"""
Follow-up automático: se o lead não respondeu em 24h, manda uma mensagem
reativando a conversa com o tom da GestorPec.
Chamado a cada 30 minutos pelo loop de background em main.py.
"""

import requests
from datetime import datetime, timedelta


def _send(phone: str, text: str, s: dict) -> bool:
    try:
        r = requests.post(
            f"{s['evolution_url']}/message/sendText/{s['instance']}",
            json={"number": phone, "text": text},
            headers={"apikey": s["api_key"], "Content-Type": "application/json"},
            timeout=15,
        )
        return r.ok
    except Exception as e:
        print(f"[followup] send error to {phone}: {e}")
        return False


def _build_message(lead_name: str, settings: dict) -> str:
    """Tenta gerar via Gemini. Se falhar, usa template fixo."""
    first_name = (lead_name or "").split()[0] or "você"
    api_key = settings.get("gemini_api_key", "").strip()
    persona = settings.get("bot_persona", "").strip()

    if api_key and persona:
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)

            prompt = f"""{persona}

---
O lead {first_name} demonstrou interesse mas sumiu há mais de 24h sem agendar.
Escreva UMA mensagem curta de follow-up (máximo 4 linhas) para reativar o contato.
Tom: direto, sem cobrar, com curiosidade ou dor. Termine convidando para agendar.
Responda SOMENTE o texto da mensagem, sem JSON, sem aspas extras."""

            for model in ("gemini-2.5-flash", "gemini-2.0-flash"):
                try:
                    m = genai.GenerativeModel(model)
                    resp = m.generate_content(prompt)
                    text = resp.text.strip()
                    if text:
                        print(f"[followup] AI gerou mensagem para {first_name}")
                        return text
                except Exception as e:
                    print(f"[followup] AI {model} error: {e}")
                    continue
        except Exception as e:
            print(f"[followup] AI setup error: {e}")

    # Template fixo com tom GestorPec
    return (
        f"Olá, *{first_name}*! 👋\n\n"
        f"Notei que você ainda não agendou nossa apresentação.\n\n"
        f"São só *15 minutos* — sem custo — pra você ver onde sua operação pode estar perdendo dinheiro.\n\n"
        f"Qual dia fica melhor pra você? 😊"
    )


def check_and_send(db, settings) -> int:
    """
    Busca leads que não responderam em 24h e envia follow-up.
    Retorna o número de mensagens enviadas.
    """
    from models import ConversationState, Lead

    cutoff = datetime.utcnow() - timedelta(hours=24)
    sent = 0

    # Estados que indicam que o lead está no funil mas não converteu
    estados_ativos = ("idle", "awaiting_date", "awaiting_time", "awaiting_confirmation")

    convs = db.query(ConversationState).filter(
        ConversationState.state.in_(estados_ativos),
        ConversationState.followup_sent == False,
        ConversationState.updated_at <= cutoff,
    ).all()

    for conv in convs:
        lead = db.query(Lead).filter(Lead.phone == conv.phone).first()
        if not lead:
            continue

        msg = _build_message(lead.name, settings)
        if _send(conv.phone, msg, settings):
            conv.followup_sent = True
            conv.updated_at = datetime.utcnow()
            sent += 1
            print(f"[followup] enviado para {conv.phone} ({lead.name})")

    if sent:
        db.commit()

    return sent
