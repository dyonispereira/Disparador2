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


def _resolve_index(value, options: list):
    """
    Aceita índice numérico (0, 1, 2...) ou string direta ("15:00", "16:00h").
    Retorna o item da lista ou None se não encontrar.
    """
    if value is None:
        return None

    # Tenta como índice numérico
    try:
        idx = int(value)
        if 0 <= idx < len(options):
            return options[idx]
    except (ValueError, TypeError):
        pass

    # Tenta como string direta (remove "h" e espaços extras)
    val_clean = str(value).replace("h", "").strip()
    if val_clean in options:
        return val_clean

    # Tenta encontrar correspondência parcial
    for opt in options:
        if opt in str(value) or str(value) in opt:
            return opt

    return None


def ask_gemini(conv, lead, user_message: str, settings: dict,
               audio_data: str = None, audio_mime: str = None):
    """
    Calls Gemini Flash and returns structured response.

    Returns: {"message": str, "action": str, "value": any}  or  None on failure.

    Actions:
        start_flow    -> show available dates (value=null)
        date_selected -> lead picked a date  (value=0-based index integer)
        time_selected -> lead picked a time  (value=0-based index integer)
        confirmed     -> lead confirmed      (value="yes")
        cancelled     -> lead wants to quit  (value="no")
        clarify       -> couldn't understand (value=null)
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
- idle / confirmed / cancelled -> apresente as datas disponíveis de forma amigável, action=start_flow
- awaiting_date  -> identifique qual data o lead quer ("quarta", "dia 28", "opção 3"), action=date_selected
- awaiting_time  -> identifique qual horário ("às 15h", "de tarde", "4", "manhã"), action=time_selected
- awaiting_confirmation -> identifique se confirma (action=confirmed) ou cancela (action=cancelled)

REGRAS OBRIGATÓRIAS PARA O CAMPO "value":
- date_selected: SEMPRE retorne o NÚMERO DO ÍNDICE (inteiro 0 a {len(days)-1}). Ex: 3a data = value=2
- time_selected: SEMPRE retorne o NÚMERO DO ÍNDICE (inteiro 0 a {len(times)-1}). Ex: 15:00h é índice {next((i for i,t in enumerate(times) if t=="15:00"), 4)} = value={next((i for i,t in enumerate(times) if t=="15:00"), 4)}
- confirmed/cancelled/start_flow/clarify: value=null

ESTILO: curto, natural, WhatsApp. Use *negrito* quando necessário. Máximo 4 linhas.

RESPONDA SOMENTE com JSON válido (sem texto fora). Exemplos corretos:
{{"message": "Qual data prefere?", "action": "start_flow", "value": null}}
{{"message": "Ótimo!", "action": "date_selected", "value": 2}}
{{"message": "Perfeito!", "action": "time_selected", "value": 4}}
{{"message": "Confirmado!", "action": "confirmed", "value": null}}"""

    # Histórico de conversa (últimas 8 mensagens)
    history = []
    try:
        for m in json.loads(conv.messages_json or "[]")[-8:]:
            role = "model" if m["role"] == "assistant" else "user"
            history.append({"role": role, "parts": [m["content"]]})
    except Exception:
        pass

    # Monta o conteúdo: áudio + instrução ou só texto
    if audio_data:
        import base64 as _b64
        try:
            audio_bytes = _b64.b64decode(audio_data)
            content = [
                {"inline_data": {"mime_type": audio_mime or "audio/ogg", "data": audio_bytes}},
                "Transcreva o áudio acima e use o conteúdo como mensagem do lead para o sistema de agendamento.",
            ]
            print(f"[AI] audio mode mime={audio_mime} bytes={len(audio_bytes)}")
        except Exception as e:
            print(f"[AI] audio decode error: {e}")
            content = user_message or "oi"
    else:
        content = user_message or "oi"

    for model_name in ("gemini-2.5-flash", "gemini-2.0-flash"):
        try:
            model = genai.GenerativeModel(
                model_name,
                system_instruction=system,
                generation_config={"response_mime_type": "application/json", "temperature": 0.3},
            )
            response = model.start_chat(history=history).send_message(content)
            result = json.loads(response.text)
            if "message" in result and "action" in result:
                print(f"[AI] {model_name} -> action={result['action']} value={result.get('value')}")
                return result
        except Exception as e:
            print(f"[AI] {model_name} error: {e}")
            continue

    return None
