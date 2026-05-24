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
import requests
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

_WEEKDAYS_PT = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
_MONTHS_PT   = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]

_YES = {"sim", "s", "yes", "confirmar", "confirmo", "ok", "tá", "ta", "blz", "beleza", "pode"}
_NO  = {"não", "nao", "n", "no", "cancelar", "cancel", "outro", "mudar", "não quero"}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _phone_variants(phone: str) -> list:
    """Brazilian numbers: WhatsApp may send 8-digit (556791879095) while DB stores
    9-digit (5567991879095) or vice versa. Return both variants to search."""
    variants = [phone]
    if phone.startswith("55") and len(phone) == 12:
        # add 9 after area code: 5567XXXXXXXX → 55679XXXXXXXX
        variants.append(phone[:4] + "9" + phone[4:])
    elif phone.startswith("55") and len(phone) == 13 and phone[4] == "9":
        # remove leading 9 from number: 55679XXXXXXXX → 5567XXXXXXXX
        variants.append(phone[:4] + phone[5:])
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

def handle_incoming(phone: str, raw_text: str, db: Session, settings: dict) -> bool:
    """
    Called for every incoming WhatsApp message.
    Returns True if the message was consumed by the scheduling flow.
    """
    from models import ConversationState, Lead

    variants = _phone_variants(phone)
    lead = db.query(Lead).filter(Lead.phone.in_(variants)).first()
    if not lead:
        return False   # Unknown sender → ignore

    # ConversationState is keyed by the canonical phone (as stored in Lead)
    canonical = lead.phone
    conv = db.query(ConversationState).filter(ConversationState.phone == canonical).first()
    if not conv:
        conv = ConversationState(phone=canonical, lead_name=lead.name, state="idle")
        db.add(conv)
        db.commit()
        db.refresh(conv)

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

    meet_link = None
    event_id = None
    try:
        result = create_meet_event(
            summary=f"Reunião {company} × {lead.name}",
            date_str=conv.selected_date,
            time_str=conv.selected_time,
            attendee_emails=emails,
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
        f"⏰ *Horário:* {conv.selected_time}h",
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
        f"⏰ *Horário:* {conv.selected_time}h",
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
