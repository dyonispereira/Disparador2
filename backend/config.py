import json
import os

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "settings.json")

DEFAULT_SETTINGS = {
    "evolution_url": os.getenv("EVOLUTION_API_URL", "http://127.0.0.1:8080"),
    "api_key": os.getenv("EVOLUTION_API_KEY", "ev_api_123456_mt_local"),
    "instance": os.getenv("EVOLUTION_INSTANCE", "minha_instancia"),
    "vendor_group_jid": "",
    "available_times": ["09:00", "10:00", "11:00", "14:00", "15:00", "16:00", "17:00"],
    "webhook_base_url": os.getenv("WEBHOOK_BASE_URL", "http://localhost:8000"),
    "company_name": "Nossa Empresa",
    "seller_name": "Equipe de Vendas",
    "company_calendar_email": "",
    "gemini_api_key": "",
    "bot_persona": "",
    # Facebook Lead Ads
    "fb_verify_token": "",
    "fb_page_access_token": "",
    "fb_app_secret": "",
}


def load_settings() -> dict:
    merged = DEFAULT_SETTINGS.copy()
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged.update(data)
    # Env vars always override the saved file
    for env_var, key in [
        ("EVOLUTION_API_URL", "evolution_url"),
        ("EVOLUTION_API_KEY", "api_key"),
        ("EVOLUTION_INSTANCE", "instance"),
        ("WEBHOOK_BASE_URL", "webhook_base_url"),
    ]:
        val = os.getenv(env_var)
        if val:
            merged[key] = val
    return merged


def save_settings(new_settings: dict):
    current = load_settings()
    current.update(new_settings)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
