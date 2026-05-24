"""
Execute UMA VEZ para autenticar o Google Calendar:

    cd backend
    python setup_google_auth.py

Pré-requisitos:
  1. Acesse https://console.cloud.google.com/
  2. Crie um projeto (ou use um existente)
  3. Ative a API "Google Calendar API"
  4. Em "Credenciais", crie OAuth 2.0 do tipo "Aplicativo de Desktop"
  5. Baixe o JSON e salve como  backend/credentials.json
  6. Execute este script — ele abrirá o navegador para autorizar
"""

import os
import sys

SCOPES = ["https://www.googleapis.com/auth/calendar"]
BASE_DIR = os.path.dirname(__file__)
CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.json")
TOKEN_FILE = os.path.join(BASE_DIR, "token.json")


def main():
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("❌ Instale primeiro: pip install google-auth-oauthlib")
        sys.exit(1)

    if not os.path.exists(CREDENTIALS_FILE):
        print("❌  credentials.json não encontrado!")
        print("    Faça o download em: https://console.cloud.google.com/ → Credenciais → OAuth 2.0")
        print(f"    Salve como: {CREDENTIALS_FILE}")
        sys.exit(1)

    print("🌐  Abrindo navegador para autorizar o Google Calendar...")
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())

    print(f"✅  Autenticação concluída! token.json salvo em: {TOKEN_FILE}")
    print("    O sistema está pronto para criar eventos no Google Meet.")


if __name__ == "__main__":
    main()
