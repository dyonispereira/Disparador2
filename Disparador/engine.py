"""
🚀 DISPARADOR GESTOR PEC - EVOLUTION API v2.3.7 (CORRIGIDO)
"""

import os
import time
import random
import pandas as pd
import requests
from pathlib import Path

# ================================
# CONFIGURAÇÃO
# ================================

EVOLUTION_URL = "http://127.0.0.1:8080"
EVOLUTION_API_KEY = "ev_api_123456_mt_local"
INSTANCE_NAME = "minha_instancia"

ARQUIVO_LEADS = "dados/leads.xlsx"
ARQUIVO_IMAGEM = "uploads/anexo.png"

DELAY_MIN = 5
DELAY_MAX = 12


# ================================
# API WHATSAPP
# ================================

class WhatsAppAPI:
    def __init__(self, url, api_key, instance):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.instance = instance
        self.headers = {"apikey": api_key}

    # ============================
    # NORMALIZA NÚMERO BR
    # ============================
    def formatar_numero(self, numero):
        numero = ''.join(c for c in str(numero) if c.isdigit())

        if not numero.startswith("55"):
            numero = "55" + numero

        return numero

    # ============================
    # ENVIAR IMAGEM + TEXTO
    # ============================
    def enviar_imagem_e_texto(self, numero, imagem_path, mensagem):

        numero = self.formatar_numero(numero)

        try:
            url = f"{self.url}/message/sendMedia/{self.instance}"

            with open(imagem_path, "rb") as f:
                files = {
                    "file": (Path(imagem_path).name, f, "image/png")
                }

                data = {
                    "number": numero,
                    "mediatype": "image",
                    "caption": mensagem
                }

                response = requests.post(
                    url,
                    files=files,
                    data=data,
                    headers=self.headers,
                    timeout=60
                )

            if response.status_code not in [200, 201]:
                print(f"❌ ERRO IMAGEM: {response.text}")
                return False

            print(f"✅ Imagem enviada -> {numero}")
            return True

        except Exception as e:
            print(f"❌ Erro imagem: {e}")
            return False

    # ============================
    # ENVIAR TEXTO
    # ============================
    def enviar_texto(self, numero, mensagem):

        numero = self.formatar_numero(numero)

        try:
            url = f"{self.url}/message/sendText/{self.instance}"

            payload = {
                "number": numero,
                "text": mensagem
            }

            response = requests.post(
                url,
                json=payload,
                headers=self.headers,
                timeout=60
            )

            if response.status_code not in [200, 201]:
                print(f"❌ ERRO TEXTO: {response.text}")
                return False

            print(f"✅ Texto enviado -> {numero}")
            return True

        except Exception as e:
            print(f"❌ Erro texto: {e}")
            return False


# ================================
# MAIN
# ================================

def main():

    print("\n╔════════════════════════════════════╗")
    print("║   🚀 DISPARADOR GESTOR PEC        ║")
    print("╚════════════════════════════════════╝\n")

    # =========================
    # VALIDAR ARQUIVOS
    # =========================
    if not os.path.exists(ARQUIVO_LEADS):
        print("❌ leads.xlsx não encontrado")
        return

    if not os.path.exists(ARQUIVO_IMAGEM):
        print("❌ imagem não encontrada")
        return

    # =========================
    # CARREGAR LEADS
    # =========================
    df = pd.read_excel(ARQUIVO_LEADS)

    if "nome" not in df.columns or "telefone" not in df.columns:
        print("❌ Excel precisa ter colunas: nome | telefone")
        return

    print(f"📂 Leads carregados: {len(df)}")

    # =========================
    # MODO
    # =========================
    print("\nModo:")
    print("1 - Imagem + Texto")
    print("2 - Texto apenas")

    modo = input("Escolha: ").strip()

    # =========================
    # CONFIRMAÇÃO
    # =========================
    print(f"\n⚠️ Vai enviar para {len(df)} leads")
    if input("Confirmar? (s/n): ").lower() != "s":
        print("Cancelado")
        return

    # =========================
    # API
    # =========================
    api = WhatsAppAPI(EVOLUTION_URL, EVOLUTION_API_KEY, INSTANCE_NAME)

    enviados = 0

    print("\n==============================")

    # =========================
    # LOOP
    # =========================
    for i, row in df.iterrows():

        nome = str(row["nome"])
        telefone = str(row["telefone"])

        if not nome or not telefone:
            continue

        mensagem = random.choice([
            f"Fala {nome}! 👊\n\nVocê já viu como grandes fazendas estão organizando tudo digitalmente?",
            f"{nome}, me diz uma coisa...\nVocê ainda controla tudo no papel ou Excel?"
        ])

        print(f"\n📤 {i+1}/{len(df)} - {nome}")

        sucesso = False

        if modo == "1":
            sucesso = api.enviar_imagem_e_texto(
                telefone,
                ARQUIVO_IMAGEM,
                mensagem
            )
        else:
            sucesso = api.enviar_texto(telefone, mensagem)

        if sucesso:
            enviados += 1

        # delay humano
        if i < len(df) - 1:
            delay = random.randint(DELAY_MIN, DELAY_MAX)
            print(f"⏳ aguardando {delay}s...")
            time.sleep(delay)

    # =========================
    # RESULTADO
    # =========================
    print("\n==============================")
    print(f"✅ Finalizado: {enviados}/{len(df)}")
    print(f"📊 Sucesso: {(enviados/len(df)*100):.1f}%")


if __name__ == "__main__":
    main()