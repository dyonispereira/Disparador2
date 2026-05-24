from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from io import StringIO
import csv
import requests
import base64
import re

import models
import schemas
from db import SessionLocal, engine

# =========================
# INIT
# =========================
models.Base.metadata.create_all(bind=engine)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Permite requisições de qualquer origem (inclusive arquivos locais)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

EVOLUTION_URL = "http://127.0.0.1:8080"
API_KEY = "ev_api_123456_mt_local"
INSTANCE = "minha_instancia"

# =========================
# DB
# =========================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# =========================
# ROOT
# =========================
@app.get("/")
def root():
    return {"status": "API rodando 🚀"}

# =========================
# LEADS
# =========================
@app.post("/leads", response_model=schemas.LeadResponse)
def create_lead(lead: schemas.LeadCreate, db: Session = Depends(get_db)):

    existing = db.query(models.Lead).filter(models.Lead.phone == lead.phone).first()
    if existing:
        raise HTTPException(status_code=400, detail="Telefone já cadastrado")

    db_lead = models.Lead(
        name=lead.name,
        phone=lead.phone,
        status="pendente"
    )

    db.add(db_lead)
    db.commit()
    db.refresh(db_lead)

    return db_lead


@app.get("/leads")
def get_leads(db: Session = Depends(get_db)):
    return db.query(models.Lead).all()

@app.delete("/leads")
def delete_all_leads(db: Session = Depends(get_db)):
    try:
        # Apaga primeiro as mensagens para evitar erro de chave estrangeira
        db.query(models.Message).delete()
        # Em seguida, apaga todos os leads
        db.query(models.Lead).delete()
        db.commit()
        return {"ok": True, "message": "Banco de dados limpo com sucesso!"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Erro ao limpar banco: {str(e)}")

# =========================
# UPLOAD CSV
# =========================
@app.post("/upload-leads-file")
async def upload_leads_file(file: UploadFile = File(...), db: Session = Depends(get_db)):
    try: # Adiciona um bloco try geral para capturar erros no upload do arquivo
        content = await file.read()
        # Garante que o conteúdo do arquivo não está vazio
        if not content:
            raise HTTPException(status_code=400, detail="O arquivo CSV está vazio.")
            
        # Tenta decodificar como UTF-8, depois UTF-16, depois ISO-8859-1
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                decoded = content.decode("utf-16")
            except UnicodeDecodeError:
                decoded = content.decode("iso-8859-1")
                
        # Padroniza quebras de linha e remove NUL bytes e BOM (Marca de UTF-8 do Excel)
        decoded = decoded.replace('\x00', '').replace('\r\n', '\n').replace('\r', '\n').replace('\ufeff', '')

        # Lê o CSV identificando automaticamente se é separado por vírgula ou ponto e vírgula
        reader = csv.reader(StringIO(decoded), delimiter=';' if ';' in decoded else ',')

        created = 0

        for row in reader:
            try:
                # Evita erro de 'IndexError' caso a linha esteja vazia ou incompleta
                if len(row) < 2:
                    continue

                name = row[0].strip()
                phone_raw = row[1].strip()
                
                # Se a planilha tiver a coluna de status, tenta usá-la, senão é pendente
                status_planilha = row[2].strip().lower() if len(row) > 2 else "pendente"
                if status_planilha not in ["pendente", "enviado", "falhou"]:
                    status_planilha = "pendente"

                # Remove tudo que não for número (espaços, traços, parênteses, etc)
                phone = re.sub(r'\D', '', phone_raw)
                
                # Pula linhas que não possuem um telefone válido (ex: cabeçalhos como "nome", "telefone")
                if not phone:
                    continue
                    
                # Se o número tiver 10 ou 11 dígitos, adiciona automaticamente o código do Brasil (55)
                if len(phone) in [10, 11]:
                    phone = f"55{phone}"

                exists = db.query(models.Lead).filter(models.Lead.phone == phone).first()
                if exists:
                    # Se o lead já existe, atualiza o status dele para sincronizar com a planilha importada
                    exists.status = status_planilha
                    continue

                lead = models.Lead(
                    name=name,
                    phone=phone,
                    status=status_planilha
                )

                db.add(lead)
                created += 1

            except Exception as e:
                print(f"Erro ao processar a linha {row}: {e}")
                continue

        db.commit()

        return {
            "ok": True,
            "created": created
        }

    except HTTPException: # Re-raise HTTPExceptions explicitly created
        raise
    except Exception as e: # Captura quaisquer outros erros inesperados
        print(f"Erro geral no upload do CSV: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao processar o arquivo CSV: {e}. Verifique o formato do arquivo.")

# =========================
# IMPORTAR CSV LOCAL DIRETO
# =========================
@app.post("/import-local-leads")
def import_local_leads(db: Session = Depends(get_db)):
    import os
    file_path = r"C:\Users\Acer\Downloads\Disparador2\dados\leads.csv"
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"Arquivo não encontrado: {file_path}")
        
    try:
        with open(file_path, "rb") as f:
            content = f.read()
            
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                decoded = content.decode("utf-16")
            except UnicodeDecodeError:
                decoded = content.decode("iso-8859-1")

        # Padroniza quebras de linha (Mac/Windows/Linux) e remove NUL bytes e BOM
        decoded = decoded.replace('\x00', '').replace('\r\n', '\n').replace('\r', '\n').replace('\ufeff', '')

        reader = csv.reader(StringIO(decoded, newline=''), delimiter=';' if ';' in decoded else ',')
        created = 0

        for row in reader:
            try:
                if len(row) < 2:
                    continue

                name = row[0].strip()
                phone_raw = row[1].strip()
                
                # Puxa o status caso exista na planilha importada
                status_planilha = row[2].strip().lower() if len(row) > 2 else "pendente"
                if status_planilha not in ["pendente", "enviado", "falhou"]:
                    status_planilha = "pendente"

                # Remove tudo que não for número
                phone = re.sub(r'\D', '', phone_raw)
                
                if not phone:
                    continue
                    
                if len(phone) in [10, 11]:
                    phone = f"55{phone}"

                exists = db.query(models.Lead).filter(models.Lead.phone == phone).first()
                if exists:
                    # Se o lead já existe, atualiza o status dele para sincronizar com a planilha
                    exists.status = status_planilha
                    continue

                lead = models.Lead(name=name, phone=phone, status=status_planilha)
                db.add(lead)
                created += 1

            except Exception as e:
                continue

        db.commit()
        return {"ok": True, "created": created}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Erro geral no CSV local: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao processar arquivo local: {e}")


# =========================
# MESSAGES
# =========================
@app.post("/messages")
def create_message(msg: schemas.MessageCreate, db: Session = Depends(get_db)):

    lead = db.query(models.Lead).filter(models.Lead.id == msg.lead_id).first()

    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    db_msg = models.Message(
        text=msg.text,
        lead_id=msg.lead_id
    )

    db.add(db_msg)
    db.commit()
    db.refresh(db_msg)

    return db_msg


@app.get("/messages")
def get_messages(db: Session = Depends(get_db)):
    return db.query(models.Message).all()

# =========================
# DISPARO (DEBUG COMPLETO)
# =========================
@app.post("/send")
async def send(
    message_text: str = Form("Fala {name}, tudo certo? 👋"),
    file: UploadFile = File(None),
    db: Session = Depends(get_db)
):

    try:
        # Seleciona apenas os leads que ainda não receberam a mensagem
        leads = db.query(models.Lead).filter(models.Lead.status != "enviado").all()

        if len(leads) == 0:
            return {
                "ok": True,
                "enviados": 0,
                "total": 0
            }

        # Prepara a imagem em base64 se ela for enviada
        b64_media = None
        mimetype = None
        if file:
            content = await file.read()
            if not content: # Caso o arquivo seja vazio
                raise HTTPException(status_code=400, detail="O arquivo de mídia está vazio.")

            b64_media = base64.b64encode(content).decode('utf-8') # Converte para base64
            mimetype = file.content_type

        enviados = 0
        total = len(leads)

        for lead in leads:
            
            # Substitui a tag {name} pelo nome real do lead na mensagem
            text = message_text.replace("{name}", lead.name)

            if b64_media:
                payload = {
                    "number": f"{lead.phone}",
                    "mediatype": "image" if file.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')) else "document", # Mais robusto para verificar tipo de imagem
                    "mimetype": mimetype,
                    "caption": text,
                    "media": b64_media
                }
                url = f"{EVOLUTION_URL}/message/sendMedia/{INSTANCE}"
            else:
                payload = {
                    "number": f"{lead.phone}",
                    "text": text
                }
                url = f"{EVOLUTION_URL}/message/sendText/{INSTANCE}"

            headers = {
                "apikey": API_KEY,
                "Content-Type": "application/json"
            }

            r = requests.post(url, json=payload, headers=headers, timeout=15)

            print("EVOLUTION RESPONSE:", r.status_code, r.text)

            if r.status_code in [200, 201]:
                lead.status = "enviado"
                enviados += 1
            else:
                lead.status = "falhou"

        db.commit()

        # ==========================================
        # ATUALIZA A PLANILHA LOCAL APÓS O DISPARO
        # ==========================================
        try:
            import os
            output_dir = r"C:\Users\Acer\Downloads\Disparador2\dados"
            if os.path.exists(output_dir):
                file_path = os.path.join(output_dir, "leads.csv")
                all_leads = db.query(models.Lead).all()
                with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.writer(f, delimiter=";")
                    writer.writerow(["Nome", "Telefone", "Status"]) # Recria o cabeçalho
                    for l in all_leads:
                        writer.writerow([l.name, l.phone, l.status])
        except Exception as e:
            print(f"Erro ao atualizar a planilha local: {e}")

        return {
            "ok": True,
            "enviados": enviados,
            "total": total
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "enviados": 0,
            "total": 0
        }

# =========================
# CONEXÃO WHATSAPP
# =========================
@app.get("/whatsapp/status")
def get_whatsapp_status():
    headers = {"apikey": API_KEY}
    try:
        r = requests.get(f"{EVOLUTION_URL}/instance/connectionState/{INSTANCE}", headers=headers)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/whatsapp/connect")
def connect_whatsapp():
    headers = {"apikey": API_KEY}
    try:
        r = requests.get(f"{EVOLUTION_URL}/instance/connect/{INSTANCE}", headers=headers)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}