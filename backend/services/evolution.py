import requests
import random

EVOLUTION_URL = "http://127.0.0.1:8080"
API_KEY = "ev_api_123456_mt_local"
INSTANCE = "minha_instancia"

def enviar_mensagem(numero, nome):

    mensagens = [
        f"Fala {nome}, tudo certo?",
        f"{nome}, posso te mostrar algo rápido?",
        f"{nome}, olha isso aqui 👀"
    ]

    msg = random.choice(mensagens)

    url = f"{EVOLUTION_URL}/message/sendText/{INSTANCE}"

    payload = {
        "chatId": f"{numero}@s.whatsapp.net",
        "text": msg
    }

    headers = {
        "apikey": API_KEY,
        "Content-Type": "application/json"
    }

    response = requests.post(url, json=payload, headers=headers)

    print("STATUS:", response.status_code)
    print("RESPOSTA:", response.text)

    return response.status_code in [200, 201]