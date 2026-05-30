"""
WhatsApp scheduling conversation flow.

States per lead:
  idle               → Send date options
  awaiting_date      → Lead picks a number → send time options
  awaiting_time      → Lead picks a number → create Meet, send confirmation
  awaiting_confirmation → Lead says SIM/NÃO → confirm or restart
  confirmed          → Meeting saved, vendors notified
  cancelled          → Flow restarted from idle
"""

import json
import re
import requests
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

_WEEKDAYS_PT = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
_MONTHS_PT   = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]

_YES = {"sim", "s", "yes", "confirmar", "confirmo", "ok", "tá", "ta", "blz", "beleza", "pode"}
_NO  = {"não", "nao", "n", "no", "cancelar", "cancel", "outro", "mudar", "não quero"}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _phone_variants(phone: str) -> list:
    """Return all plausible DB variants for an incoming WhatsApp phone number.
    Handles: with/without country code 55, 8-digit vs 9-digit local numbers."""
    digits = re.sub(r'\D', '', phone)
    seen = set()
    variants = []

    def _add(p):
        if p and p not in seen:
            seen.add(p)
            variants.append(p)

    _add(digits)

    if digits.startswith("55") and len(digits) >= 12:
        no_cc = digits[2:]
        _add(no_cc)
        # 12-digit (55+DDD+8): add 9 → 13-digit variant
        if len(digits) == 12:
            _add(digits[:4] + "9" + digits[4:])
            _add(no_cc[:2] + "9" + no_cc[2:])
        # 13-digit with leading 9 (55+DDD+9+8): remove 9 → 12-digit variant
        elif len(digits) == 13 and digits[4] == "9":
            _add(digits[:4] + digits[5:])
            _add(no_cc[:2] + no_cc[3:])
    elif not digits.startswith("55") and len(digits) in [10, 11]:
        with_cc = "55" + digits
        _add(with_cc)
        if len(digits) == 10:
            _add(with_cc[:4] + "9" + with_cc[4:])
            _add(digits[:2] + "9" + digits[2:])
        elif len(digits) == 11 and digits[2] == "9":
            _add(with_cc[:4] + with_cc[5:])
            _add(digits[:2] + digits[3:])

    return variants


def _next_business_days(n: int = 5):
    days, d = [], datetime.now().date() + timedelta(days=1)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _fmt_date(d) -> str:
    return f"{_WEEKDAYS_PT[d.weekday()]}, {d.day} de {_MONTHS_PT[d.month - 1]}"


def _send(phone: str, text: str, s: dict) -> bool:
    """Fire-and-forget text message via Evolution API."""
    try:
        r = requests.post(
            f"{s['evolution_url']}/message/sendText/{s['instance']}",
            json={"number": phone, "text": text},
            headers={"apikey": s["api_key"], "Content-Type": "application/json"},
            timeout=15,
        )
        return r.ok
    except Exception as e:
        print(f"[flow] send error to {phone}: {e}")
        return False


# ─── Public entry-point ───────────────────────────────────────────────────────

def handle_incoming(phone: str, raw_text: str, db: Session, settings: dict,
                    audio_data: str = None, audio_mime: str = None) -> bool:
    """
    Called for every incoming WhatsApp message.
    Returns True if the message was consumed by the scheduling flow.
    """
    from models import ConversationState, Lead

    variants = _phone_variants(phone)
    lead = db.query(Lead).filter(Lead.phone.in_(variants)).first()
    if not lead:
        # Lead desconhecido — cria automaticamente e inicia o fluxo
        lead = Lead(
            name=phone,
            phone=phone,
            status="pendente",
            etapa="Novo Lead",
            board_id=1,
            origem_lead="WhatsApp",
        )
        db.add(lead)
        db.commit()
        db.refresh(lead)
        print(f"[flow] novo lead criado automaticamente via WhatsApp: {phone}")

    canonical = lead.phone
    conv = db.query(ConversationState).filter(ConversationState.phone == canonical).first()
    if not conv:
        conv = ConversationState(phone=canonical, lead_name=lead.name, state="idle")
        db.add(conv)
        db.commit()
        db.refresh(conv)

    # Lead voltou a interagir — zera o follow-up para poder reenviar no futuro
    if conv.followup_sent:
        conv.followup_sent = False
        db.commit()

    # Se o lead ainda não está no funil CRM, adiciona como "Novo Lead"
    if not lead.board_id:
        lead.board_id = 1
        lead.etapa = "Novo Lead"
        db.commit()

    # Rota para o fluxo AI se qualquer chave Gemini estiver configurada
    from services.ai_flow import _get_all_keys
    if _get_all_keys(settings):
        return _handle_with_ai(conv, lead, raw_text, db, settings,
                               audio_data=audio_data, audio_mime=audio_mime)

    # Fallback: fluxo numérico (sem áudio)
    if audio_data and not raw_text.strip():
        _send(lead.phone, "Recebi seu áudio! 😊 Por favor, responda com texto para eu conseguir agendar.", settings)
        return True

    text = raw_text.strip().lower()
    if conv.state in ("idle", "confirmed", "cancelled"):
        _start_flow(conv, lead, db, settings)
        return True
    if conv.state == "awaiting_date":
        return _on_date_pick(conv, lead, text, db, settings)
    if conv.state == "awaiting_time":
        return _on_time_pick(conv, lead, text, db, settings)
    if conv.state == "awaiting_confirmation":
        return _on_confirm(conv, lead, text, db, settings)
    return False


# ─── AI flow ──────────────────────────────────────────────────────────────────

def _append_history(conv, user_msg: str, bot_msg: str, db):
    try:
        msgs = json.loads(conv.messages_json or "[]")
    except Exception:
        msgs = []
    msgs.append({"role": "user",      "content": user_msg})
    msgs.append({"role": "assistant", "content": bot_msg})
    conv.messages_json = json.dumps(msgs[-20:])  # keep last 20
    conv.updated_at = datetime.utcnow()
    db.commit()


def _handle_with_ai(conv, lead, raw_text: str, db, settings,
                    audio_data: str = None, audio_mime: str = None) -> bool:
    from services.ai_flow import ask_gemini

    try:
        result = ask_gemini(conv, lead, raw_text, settings,
                            audio_data=audio_data, audio_mime=audio_mime)
    except Exception as exc:
        print(f"[AI] ask_gemini exception — falling back to numeric flow: {exc}")
        result = None

    if not result:
        # Gemini unavailable — fall back to full scheduling flow
        text = raw_text.strip().lower()
        if conv.state in ("idle", "confirmed", "cancelled"):
            _start_flow(conv, lead, db, settings)
        elif conv.state == "awaiting_date":
            # If dates were never offered (state got stuck), restart
            if not json.loads(conv.offered_dates or "[]"):
                _start_flow(conv, lead, db, settings)
            else:
                _on_date_pick(conv, lead, text, db, settings)
        elif conv.state == "awaiting_time":
            # If times were never offered (state got stuck), re-ask
            if not json.loads(conv.offered_times or "[]"):
                _ask_time(conv, lead, db, settings)
            else:
                _on_time_pick(conv, lead, text, db, settings)
        elif conv.state == "awaiting_confirmation":
            _on_confirm(conv, lead, text, db, settings)
        return True

    action  = result.get("action", "clarify")
    value   = result.get("value")
    message = result.get("message", "")

    _append_history(conv, raw_text, message, db)

    # For time_selected and confirmed the system sends its own messages after
    if message and action not in ("time_selected", "confirmed"):
        _send(lead.phone, message, settings)

    if action == "start_flow":
        days = _next_business_days(5)
        conv.offered_dates  = json.dumps([d.strftime("%Y-%m-%d") for d in days])
        conv.offered_times  = json.dumps(settings.get("available_times", ["09:00","10:00","11:00","14:00","15:00","16:00","17:00"]))
        conv.state          = "awaiting_date"
        conv.updated_at     = datetime.utcnow()
        db.commit()

    elif action == "date_selected":
        from services.ai_flow import _resolve_index
        dates = json.loads(conv.offered_dates or "[]")
        chosen = _resolve_index(value, dates)
        if chosen:
            conv.selected_date = chosen
            conv.offered_times = json.dumps(settings.get("available_times", ["09:00","10:00","11:00","14:00","15:00","16:00","17:00"]))
            conv.state         = "awaiting_time"
            conv.updated_at    = datetime.utcnow()
            db.commit()

    elif action == "time_selected":
        from services.ai_flow import _resolve_index
        times = json.loads(conv.offered_times or "[]")
        chosen = _resolve_index(value, times)
        if chosen:
            conv.selected_time = chosen
            conv.updated_at    = datetime.utcnow()
            db.commit()
            _create_meet_and_confirm(conv, lead, db, settings)
        else:
            _send(lead.phone, message or "Não entendi o horário 😅 Qual dos horários acima prefere?", settings)

    elif action == "confirmed":
        if conv.selected_date and conv.selected_time:
            _finalize(conv, lead, db, settings)
        elif conv.selected_date:
            # Horário nunca foi capturado — pede de novo
            _ask_time(conv, lead, db, settings)
        else:
            _start_flow(conv, lead, db, settings)

    elif action == "cancelled":
        conv.state          = "idle"
        conv.selected_date  = None
        conv.selected_time  = None
        conv.meet_link      = None
        conv.calendar_event_id = None
        conv.updated_at     = datetime.utcnow()
        db.commit()

    # clarify in idle/confirmed/cancelled: bot answered the question, now present dates
    elif action == "clarify" and conv.state in ("idle", "confirmed", "cancelled"):
        import time as _time
        _time.sleep(1)
        _start_flow(conv, lead, db, settings)

    return True


# ─── Flow steps ───────────────────────────────────────────────────────────────

def _start_flow(conv, lead, db, settings):
    days = _next_business_days(5)
    date_strs = [d.strftime("%Y-%m-%d") for d in days]
    name = lead.name.split()[0] if lead.name else "você"

    lines = [
        f"Olá, *{name}*! 😊 Que bom ter seu interesse!",
        "",
        "Vou agendar uma apresentação rápida da nossa solução.",
        "Qual data fica melhor pra você?\n",
    ]
    for i, (d, ds) in enumerate(zip(days, date_strs), 1):
        lines.append(f"*{i}.* {_fmt_date(d)} ({d.strftime('%d/%m')})")

    _send(lead.phone, "\n".join(lines), settings)

    conv.state = "awaiting_date"
    conv.offered_dates = json.dumps(date_strs)
    conv.updated_at = datetime.utcnow()
    db.commit()


def _on_date_pick(conv, lead, text, db, settings) -> bool:
    dates = json.loads(conv.offered_dates or "[]")
    try:
        idx = int(text.strip()) - 1
        if 0 <= idx < len(dates):
            conv.selected_date = dates[idx]
            _ask_time(conv, lead, db, settings)
            return True
    except ValueError:
        pass

    _send(lead.phone, f"Não entendi 😅 — digite só o número (1 a {len(dates)})", settings)
    return True


def _ask_time(conv, lead, db, settings):
    times = settings.get("available_times", ["09:00", "10:00", "11:00", "14:00", "15:00", "16:00", "17:00"])
    d = datetime.strptime(conv.selected_date, "%Y-%m-%d").date()

    lines = [f"📅 *{_fmt_date(d)} ({d.strftime('%d/%m')})* — ótima escolha!\n", "Agora me diga o horário:"]
    for i, t in enumerate(times, 1):
        lines.append(f"*{i}.* {t}h")

    _send(lead.phone, "\n".join(lines), settings)

    conv.state = "awaiting_time"
    conv.offered_times = json.dumps(times)
    conv.updated_at = datetime.utcnow()
    db.commit()


def _on_time_pick(conv, lead, text, db, settings) -> bool:
    times = json.loads(conv.offered_times or "[]")
    try:
        idx = int(text.strip()) - 1
        if 0 <= idx < len(times):
            conv.selected_time = times[idx]
            _create_meet_and_confirm(conv, lead, db, settings)
            return True
    except ValueError:
        pass

    _send(lead.phone, f"Não entendi 😅 — digite só o número (1 a {len(times)})", settings)
    return True


def _create_meet_and_confirm(conv, lead, db, settings):
    from services.google_meet import create_meet_event
    from models import Participante

    company = settings.get("company_name", "Nossa Empresa")
    d = datetime.strptime(conv.selected_date, "%Y-%m-%d").date()
    date_label = f"{_fmt_date(d)} ({d.strftime('%d/%m')})"

    ativos = db.query(Participante).filter(Participante.ativo == True).all()
    emails = [p.email for p in ativos]

    # ID do calendário compartilhado da empresa (cria o evento diretamente nele)
    calendar_id = settings.get("company_calendar_email", "").strip() or "primary"

    meet_link = None
    event_id = None
    try:
        result = create_meet_event(
            summary=f"Reunião {company} × {lead.name}",
            date_str=conv.selected_date,
            time_str=conv.selected_time,
            attendee_emails=emails,
            calendar_id=calendar_id,
        )
        meet_link = result.get("meet_link")
        event_id = result.get("event_id")
    except Exception as e:
        print(f"[flow] Google Meet error: {e}")

    conv.meet_link = meet_link
    conv.calendar_event_id = event_id
    conv.state = "awaiting_confirmation"
    conv.updated_at = datetime.utcnow()
    db.commit()

    lines = [
        "🗓️ *Tudo certo! Confirma a reunião abaixo?*",
        "",
        f"📅 *Data:* {date_label}",
        f"⏰ *Horário:* {conv.selected_time or '?'}h",
    ]
    if meet_link:
        lines += ["🎥 *Link Meet:*", meet_link]
    lines += ["", "Responda *SIM* para confirmar ou *NÃO* para escolher outra data."]

    _send(lead.phone, "\n".join(lines), settings)


def _on_confirm(conv, lead, text, db, settings) -> bool:
    if text in _YES:
        _finalize(conv, lead, db, settings)
    elif text in _NO:
        conv.state = "idle"
        conv.selected_date = None
        conv.selected_time = None
        conv.meet_link = None
        conv.calendar_event_id = None
        conv.updated_at = datetime.utcnow()
        db.commit()
        _send(lead.phone, "Sem problema! Vamos recomeçar 😊", settings)
        _start_flow(conv, lead, db, settings)
    else:
        _send(lead.phone, "Responda *SIM* para confirmar ou *NÃO* para escolher outro horário.", settings)
    return True


def _finalize(conv, lead, db, settings):
    from models import ScheduledMeeting

    company = settings.get("company_name", "Nossa Empresa")
    vendor_group = settings.get("vendor_group_jid", "")
    d = datetime.strptime(conv.selected_date, "%Y-%m-%d").date()
    date_label = f"{_fmt_date(d)} ({d.strftime('%d/%m/%Y')})"
    meet_link = conv.meet_link or "_(sem link)_"

    meeting = ScheduledMeeting(
        lead_name=lead.name,
        lead_phone=lead.phone,
        meeting_date=conv.selected_date,
        meeting_time=conv.selected_time,
        meet_link=meet_link,
        calendar_event_id=conv.calendar_event_id,
        status="confirmado",
        confirmed_at=datetime.utcnow(),
    )
    db.add(meeting)

    # Confirmation to lead
    _send(lead.phone, "\n".join([
        "🎉 *Reunião confirmada!*",
        "",
        f"📅 *Data:* {date_label}",
        f"⏰ *Horário:* {conv.selected_time or '?'}h",
        f"🎥 *Link Meet:* {meet_link}",
        "",
        "Até lá! Qualquer dúvida é só chamar. 😊",
    ]), settings)

    # Summary to vendor WhatsApp group
    if vendor_group:
        _send(vendor_group, "\n".join([
            "🔔 *Nova reunião confirmada!*",
            "",
            f"👤 *Lead:* {lead.name}",
            f"📱 *Tel:* {lead.phone}",
            f"📅 *Data:* {date_label}",
            f"⏰ *Horário:* {conv.selected_time}h",
            f"🎥 *Meet:* {meet_link}",
            "",
            "✅ Confirmado via WhatsApp",
        ]), settings)

    conv.state = "confirmed"
    conv.updated_at = datetime.utcnow()
    db.commit()
