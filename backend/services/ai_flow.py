"""
Gemini Flash AI integration for natural WhatsApp conversation.
Falls back to number-based flow if API key not configured or on error.
"""

import json
from datetime import datetime, timedelta

_WEEKDAYS_PT = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
_MONTHS_PT   = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]


def _next_business_days(n=5):
    days, d = [], datetime.now().date() + timedelta(days=1)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _fmt_date(d) -> str:
    return f"{_WEEKDAYS_PT[d.weekday()]}, {d.day} de {_MONTHS_PT[d.month-1]}"


def ask_gemini(conv, lead, user_message: str, settings: dict):
    """
    Calls Gemini Flash and returns structured response.

    Returns: {"message": str, "action": str, "value": any}  or  None on failure.

    Actions:
        start_flow    → show available dates (value=null)
        date_selected → lead picked a date  (value=0-based index)
        time_selected → lead picked a time  (value=0-based index)
        confirmed     → lead confirmed      (value="yes")
        cancelled     → lead wants to quit  (value="no")
        clarify       → couldn't understand (value=null)
    """
    api_key = settings.get("gemini_api_key", "").strip()
    if not api_key:
        return None

    try:
        import google.generativeai as genai
    except ImportError:
        print("[AI] Instale: pip install google-generativeai")
        return None

    genai.configure(api_key=api_key)

    days  = _next_business_days(5)
    times = settings.get("available_times", ["09:00","10:00","11:00","14:00","15:00","16:00","17:00"])
    company    = settings.get("company_name", "Nossa Empresa")
    first_name = (lead.name or "").split()[0] or "você"

    dates_list = "\n".join([f"{i}. {_fmt_date(d)} ({d.strftime('%d/%m')})" for i, d in enumerate(days)])
    times_list = "\n".join([f"{i}. {t}h" for i, t in enumerate(times)])

    system = f"""Você é um assistente comercial simpático da {company} no WhatsApp.
Seu objetivo é agendar uma reunião rápida de apresentação com {first_name}.

ESTADO ATUAL: {conv.state}
DATA ESCOLHIDA: {conv.selected_date or "nenhuma"}
HORÁRIO ESCOLHIDO: {conv.selected_time or "nenhum"}

DATAS DISPONÍVEIS (índices 0 a {len(days)-1}):
{dates_list}

HORÁRIOS DISPONÍVEIS (índices 0 a {len(times)-1}):
{times_list}

COMPORTAMENTO POR ESTADO:
- idle / confirmed / cancelled → apresente as datas disponíveis de forma amigável
- awaiting_date  → identifique qual data o lead quer ("quarta", "dia 28", "opção 3", etc.)
- awaiting_time  → identifique qual horário ("às 15h", "de tarde", "4", "manhã", etc.)
- awaiting_confirmation → identifique se confirma ou cancela

ESTILO: curto, natural, WhatsApp. Use *negrito* quando necessário. Máximo 4 linhas.

RESPONDA SOMENTE com JSON válido (sem texto fora):
{{"message": "texto para o lead", "action": "start_flow|date_selected|time_selected|confirmed|cancelled|clarify", "value": null}}"""

    # Conversation history (last 8 messages for context)
    history = []
    try:
        for m in json.loads(conv.messages_json or "[]")[-8:]:
            history.append({"role": m["role"], "parts": [m["content"]]})
    except Exception:
        pass

    for model_name in ("gemini-2.0-flash", "gemini-2.0-flash-lite"):
        try:
            model = genai.GenerativeModel(
                model_name,
                system_instruction=system,
                generation_config={"response_mime_type": "application/json", "temperature": 0.4},
            )
            response = model.start_chat(history=history).send_message(user_message)
            result = json.loads(response.text)
            if "message" in result and "action" in result:
                print(f"[AI] {model_name} → action={result['action']} value={result.get('value')}")
                return result
        except Exception as e:
            print(f"[AI] {model_name} error: {e}")
            continue

    return None
