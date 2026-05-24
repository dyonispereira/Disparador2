"""
Automatic WhatsApp reminders: 24h and 1h before confirmed meetings.
Called every 5 minutes by the background scheduler in main.py.
"""

import requests
from datetime import datetime


_WEEKDAYS_PT = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
_MONTHS_PT   = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]


def _fmt_date(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        return f"{_WEEKDAYS_PT[d.weekday()]}, {d.day} de {_MONTHS_PT[d.month-1]}"
    except Exception:
        return date_str


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
        print(f"[reminder] send error to {phone}: {e}")
        return False


def check_and_send(db, settings) -> int:
    """
    Scans confirmed meetings and sends reminders when due.
    Returns number of reminders sent.
    """
    from models import ScheduledMeeting

    now = datetime.now()
    sent = 0

    meetings = db.query(ScheduledMeeting).filter(
        ScheduledMeeting.status == "confirmado"
    ).all()

    for m in meetings:
        try:
            meeting_dt = datetime.strptime(f"{m.meeting_date} {m.meeting_time}", "%Y-%m-%d %H:%M")
        except Exception:
            continue

        minutes_until = (meeting_dt - now).total_seconds() / 60

        # 24h reminder — window: 23h55m to 24h05m (1435–1445 min)
        if not m.reminder_24h_sent and 1435 <= minutes_until <= 1445:
            msg = "\n".join([
                f"👋 Olá! Só um lembrete da sua reunião *amanhã*:",
                "",
                f"📅 {_fmt_date(m.meeting_date)}",
                f"⏰ {m.meeting_time}h",
                f"🎥 {m.meet_link or ''}",
                "",
                "Até lá! 😊",
            ])
            if _send(m.lead_phone, msg, settings):
                m.reminder_24h_sent = True
                sent += 1
                print(f"[reminder] 24h sent to {m.lead_phone}")

        # 1h reminder — window: 55m to 65m before
        if not m.reminder_1h_sent and 55 <= minutes_until <= 65:
            msg = "\n".join([
                "⏰ Sua reunião começa em *1 hora*!",
                "",
                f"📅 Hoje, {m.meeting_time}h",
                f"🎥 {m.meet_link or ''}",
                "",
                "Nos vemos em breve! 🚀",
            ])
            if _send(m.lead_phone, msg, settings):
                m.reminder_1h_sent = True
                sent += 1
                print(f"[reminder] 1h sent to {m.lead_phone}")

    if sent:
        db.commit()

    return sent
