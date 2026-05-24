import requests

EVOLUTION_URL = "http://127.0.0.1:8080"

INSTANCE = "minha_instancia"

def get_qr():
    try:
        r = requests.get(f"{EVOLUTION_URL}/instance/connect/{INSTANCE}")
        return r.json()
    except:
        return {"error": "API offline"}

def get_status():
    try:
        r = requests.get(f"{EVOLUTION_URL}/instance/status/{INSTANCE}")
        return r.json()
    except:
        return {"state": "offline"}

def send_message(number, message):
    payload = {
        "number": number,
        "text": message
    }

    r = requests.post(
        f"{EVOLUTION_URL}/message/sendText/{INSTANCE}",
        json=payload
    )
    return r.json()