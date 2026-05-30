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


_key_index = 0  # rotação global entre chaves


def _get_all_keys(settings: dict) -> list:
    keys = []
    primary = settings.get("gemini_api_key", "").strip()
    if primary:
        keys.append(primary)
    for k in settings.get("gemini_api_keys", []):
        k = k.strip()
        if k and k not in keys:
            keys.append(k)
    return keys


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
    global _key_index
    keys = _get_all_keys(settings)
    if not keys:
        return None

    try:
        import google.generativeai as genai
    except ImportError:
        print("[AI] Instale: pip install google-generativeai")
        return None

    # Usa a chave actual (rotação acontece no bloco de chamada abaixo)
    genai.configure(api_key=keys[_key_index % len(keys)])

    days  = _next_business_days(5)
    times = settings.get("available_times", ["09:00","10:00","11:00","14:00","15:00","16:00","17:00"])
    company    = settings.get("company_name", "Nossa Empresa")
    first_name = (lead.name or "").split()[0] or "você"

    dates_list = "\n".join([f"{i}. {_fmt_date(d)} ({d.strftime('%d/%m')})" for i, d in enumerate(days)])
    times_list = "\n".join([f"{i}. {t}h" for i, t in enumerate(times)])

    persona = settings.get("bot_persona", "").strip()
    persona_block = f"{persona}\n\n---\n\n" if persona else ""

    idx_15 = next((i for i, t in enumerate(times) if t == "15:00"), 4)

    system = f"""{persona_block}LEAD: {first_name}
EMPRESA: {company}
ESTADO DA CONVERSA: {conv.state}
DATA ESCOLHIDA: {conv.selected_date or "nenhuma"}
HORÁRIO ESCOLHIDO: {conv.selected_time or "nenhum"}

DATAS DISPONÍVEIS (índices 0 a {len(days)-1}):
{dates_list}

HORÁRIOS DISPONÍVEIS (índices 0 a {len(times)-1}):
{times_list}

COMPORTAMENTO OBRIGATÓRIO POR ESTADO:
- idle / confirmed / cancelled -> use a persona para engajar, provoque a dor, crie curiosidade e apresente as datas para agendar. action=start_flow
- awaiting_date  -> identifique qual data o lead quer ("quarta", "dia 28", "opção 3", "próxima semana"). action=date_selected
- awaiting_time  -> identifique qual horário ("às 15h", "de tarde", "4", "manhã"). action=time_selected
- awaiting_confirmation -> identifique se confirma (action=confirmed) ou cancela (action=cancelled)
- Se o lead tiver dúvidas, objeções ou perguntas -> responda usando a persona (curto, direto) e redirecione para o agendamento. action=clarify

REGRAS DO CAMPO "value" (OBRIGATÓRIO):
- date_selected: ÍNDICE INTEIRO (0 a {len(days)-1}). Ex: 3ª opção = value=2
- time_selected: ÍNDICE INTEIRO (0 a {len(times)-1}). Ex: 15:00h = value={idx_15}
- start_flow / confirmed / cancelled / clarify: value=null

FORMATO: mensagens curtas, WhatsApp, *negrito* quando necessário, máximo 4 linhas.

RESPONDA SOMENTE JSON válido. Exemplos:
{{"message": "Boa! Qual data fica melhor pra você?", "action": "start_flow", "value": null}}
{{"message": "Anotado!", "action": "date_selected", "value": 2}}
{{"message": "Perfeito!", "action": "time_selected", "value": {idx_15}}}
{{"message": "Reunião confirmada!", "action": "confirmed", "value": null}}"""

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

    for attempt in range(len(keys)):
        current_key = keys[_key_index % len(keys)]
        genai.configure(api_key=current_key)
        for model_name in ("gemini-2.0-flash", "gemini-2.5-flash"):
            try:
                model = genai.GenerativeModel(
                    model_name,
                    system_instruction=system,
                    generation_config={"response_mime_type": "application/json", "temperature": 0.3},
                )
                response = model.start_chat(history=history).send_message(content)
                result = json.loads(response.text)
                if "message" in result and "action" in result:
                    print(f"[AI] chave {_key_index % len(keys)+1}/{len(keys)} {model_name} -> action={result['action']}")
                    return result
            except Exception as e:
                err = str(e)
                if "429" in err or "quota" in err.lower() or "Resource" in err:
                    print(f"[AI] Chave {_key_index % len(keys)+1} quota esgotada, trocando...")
                    _key_index += 1
                    break
                print(f"[AI] {model_name} error: {e}")
                continue

    print("[AI] Todas as chaves esgotadas ou erro")
    return None
