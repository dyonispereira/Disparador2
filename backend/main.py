from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from io import StringIO
import csv
import requests
import base64
import re
import random
import asyncio
from datetime import datetime

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
# MESSAGE TEMPLATES (CRUD)
# =========================
@app.post("/message-templates", response_model=schemas.MessageTemplateResponse)
def create_message_template(template: schemas.MessageTemplateCreate, db: Session = Depends(get_db)):
    db_template = models.MessageTemplate(**template.dict())
    db.add(db_template)
    db.commit()
    db.refresh(db_template)
    return db_template

@app.get("/message-templates", response_model=list[schemas.MessageTemplateResponse])
def get_message_templates(db: Session = Depends(get_db)):
    return db.query(models.MessageTemplate).all()

@app.put("/message-templates/{template_id}", response_model=schemas.MessageTemplateResponse)
def update_message_template(template_id: int, template: schemas.MessageTemplateUpdate, db: Session = Depends(get_db)):
    db_template = db.query(models.MessageTemplate).filter(models.MessageTemplate.id == template_id).first()
    if not db_template:
        raise HTTPException(status_code=404, detail="Template não encontrado")
    db_template.text = template.text
    db.commit()
    db.refresh(db_template)
    return db_template

@app.delete("/message-templates/{template_id}")
def delete_message_template(template_id: int, db: Session = Depends(get_db)):
    db_template = db.query(models.MessageTemplate).filter(models.MessageTemplate.id == template_id).first()
    if not db_template:
        raise HTTPException(status_code=404, detail="Template não encontrado")
    db.delete(db_template)
    db.commit()
    return {"ok": True, "message": "Template deletado com sucesso"}

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
        # Detecta o delimitador (ponto e vírgula, tabulação ou vírgula)
        if ';' in decoded:
            delimiter = ';'
        elif '\t' in decoded:
            delimiter = '\t'
        else:
            delimiter = ','

        reader = csv.reader(StringIO(decoded, newline=''), delimiter=delimiter)
        created = 0

        for row in reader:
            try:
                # Evita erro de 'IndexError' caso a linha esteja vazia ou incompleta
                if len(row) < 2:
                    continue

                name = row[0].strip()
                phone_raw = row[1].strip()

                # Heurística: Se o campo "nome" parece um template de mensagem, ignora-o.
                if "{" in name and "}" in name and len(name) > 30:
                    name = ""
                
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

        # Detecta o delimitador (ponto e vírgula, tabulação ou vírgula)
        if ';' in decoded:
            delimiter = ';'
        elif '\t' in decoded:
            delimiter = '\t'
        else:
            delimiter = ','

        reader = csv.reader(StringIO(decoded, newline=''), delimiter=delimiter)
        created = 0

        for row in reader:
            try:
                if len(row) < 2:
                    continue

                name = row[0].strip()
                phone_raw = row[1].strip()

                # Heurística: Se o campo "nome" parece um template de mensagem, ignora-o.
                if "{" in name and "}" in name and len(name) > 30:
                    name = ""
                
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
    campaign_name: str = Form(...),
    file: UploadFile = File(None),
    db: Session = Depends(get_db)
):

    try:
        templates = db.query(models.MessageTemplate).all()
        if not templates:
            raise HTTPException(status_code=400, detail="Nenhuma mensagem cadastrada para o disparo.")

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
        headers = {"apikey": API_KEY, "Content-Type": "application/json"}

        for i, lead in enumerate(leads):
            
            # 1. Sorteia uma mensagem aleatória
            chosen_template = random.choice(templates)
            text = chosen_template.text.replace("{name}", lead.name or "")
            
            final_status_code = 500  # Assume falha por padrão

            # 2. Lógica de envio com ordem e delays aleatórios
            if b64_media:
                order = random.choice(['media_first', 'text_first'])
                delay_between_parts = random.uniform(2, 8)

                if order == 'media_first':
                    payload_media = {"number": f"{lead.phone}", "mediatype": "image" if file.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')) else "document", "mimetype": mimetype, "caption": text, "media": b64_media}
                    url_media = f"{EVOLUTION_URL}/message/sendMedia/{INSTANCE}"
                    r = requests.post(url_media, json=payload_media, headers=headers, timeout=30)
                    final_status_code = r.status_code
                    print(f"EVOLUTION (Media First) to {lead.phone}:", r.status_code, r.text)
                else:  # text_first
                    payload_text = {"number": f"{lead.phone}", "text": text}
                    url_text = f"{EVOLUTION_URL}/message/sendText/{INSTANCE}"
                    r_text = requests.post(url_text, json=payload_text, headers=headers, timeout=15)
                    print(f"EVOLUTION (Text First - Part 1) to {lead.phone}:", r_text.status_code, r_text.text)
                    
                    await asyncio.sleep(delay_between_parts)

                    payload_media = {"number": f"{lead.phone}", "mediatype": "image" if file.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')) else "document", "mimetype": mimetype, "media": b64_media}
                    url_media = f"{EVOLUTION_URL}/message/sendMedia/{INSTANCE}"
                    r_media = requests.post(url_media, json=payload_media, headers=headers, timeout=30)
                    print(f"EVOLUTION (Text First - Part 2) to {lead.phone}:", r_media.status_code, r_media.text)
                    
                    final_status_code = r_media.status_code if r_text.ok else r_text.status_code

            else:  # Apenas texto
                payload_text = {"number": f"{lead.phone}", "text": text}
                url_text = f"{EVOLUTION_URL}/message/sendText/{INSTANCE}"
                r = requests.post(url_text, json=payload_text, headers=headers, timeout=15)
                final_status_code = r.status_code
                print(f"EVOLUTION (Text Only) to {lead.phone}:", r.status_code, r.text)

            # 3. Atualiza o status no banco
            if 200 <= final_status_code < 300:
                lead.status = "enviado"
                enviados += 1
            else:
                lead.status = "falhou"
            
            lead.campaign_name = campaign_name
            lead.sent_message = text
            lead.sent_at = datetime.utcnow()

            db.commit() # Salva o status de cada lead imediatamente

            # 4. Delay entre leads (não aplica no último)
            if i < len(leads) - 1:
                delay_between_leads = random.uniform(20, 90)
                print(f"--- Aguardando {delay_between_leads:.2f}s para o próximo lead ---")
                await asyncio.sleep(delay_between_leads)

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
                    # Usa ponto e vírgula (padrão Excel BR) e força aspas em todos os campos para máxima compatibilidade.
                    writer = csv.writer(f, delimiter=";", quoting=csv.QUOTE_ALL)
                    writer.writerow(["Nome", "Telefone", "Status", "Mensagem Enviada", "Data Disparo", "Campanha"]) # Recria o cabeçalho
                    for l in all_leads:
                        sent_at_str = l.sent_at.strftime("%Y-%m-%d %H:%M:%S") if l.sent_at else ""
                        writer.writerow([l.name, l.phone, l.status, l.sent_message, sent_at_str, l.campaign_name])
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


# =========================
# CONFIGURAÇÕES
# =========================
@app.get("/settings")
def get_settings():
    from config import load_settings
    return load_settings()


@app.put("/settings")
def update_settings(body: dict, db: Session = Depends(get_db)):
    from config import load_settings, save_settings
    save_settings(body)
    return {"ok": True, "settings": load_settings()}


# =========================
# WEBHOOK – Evolution API → Backend
# =========================
@app.get("/webhook/evolution")
def webhook_evolution_verify():
    return {"ok": True, "status": "webhook ativo"}


@app.post("/webhook/evolution")
async def webhook_evolution(request: Request):
    from config import load_settings
    from services.scheduling_flow import handle_incoming

    try:
        payload = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}

    print(f"[webhook] raw payload keys: {list(payload.keys())}")

    event = payload.get("event", "")
    if event not in ("messages.upsert", "MESSAGES_UPSERT"):
        print(f"[webhook] skipped event: {event}")
        return {"ok": True, "skipped": f"event={event}"}

    # Evolution API pode mandar data como objeto ou como lista de objetos
    raw_data = payload.get("data", {})
    messages = raw_data if isinstance(raw_data, list) else [raw_data]

    results = []
    for data in messages:
        if not isinstance(data, dict):
            continue

        key = data.get("key", {})
        print(f"[webhook] key: {key}")

        if key.get("fromMe"):
            print("[webhook] skipped: fromMe")
            continue

        remote_jid = key.get("remoteJid", "")
        if remote_jid.endswith("@g.us"):
            print(f"[webhook] skipped: group {remote_jid}")
            continue

        phone = remote_jid.replace("@s.whatsapp.net", "").replace("@c.us", "")
        if not phone:
            continue

        msg_obj = data.get("message", {})
        text = (
            msg_obj.get("conversation")
            or (msg_obj.get("extendedTextMessage") or {}).get("text")
            or (msg_obj.get("imageMessage") or {}).get("caption")
            or ""
        )

        print(f"[webhook] phone={phone} text={repr(text)}")

        if not text.strip():
            continue

        db = SessionLocal()
        try:
            settings = load_settings()

            # Busca nome do lead (tenta 8 e 9 dígitos brasileiros)
            def _variants(p):
                vs = [p]
                if p.startswith("55") and len(p) == 12:
                    vs.append(p[:4] + "9" + p[4:])
                elif p.startswith("55") and len(p) == 13 and p[4] == "9":
                    vs.append(p[:4] + p[5:])
                return vs
            lead = db.query(models.Lead).filter(models.Lead.phone.in_(_variants(phone))).first()
            lead_name = (lead.name or "Lead") if lead else "Lead"

            # Alerta comercial para o grupo
            vendor_jid = settings.get("vendor_group_jid", "")
            if vendor_jid:
                phone_display = phone[2:] if phone.startswith("55") else phone
                data_hora = datetime.now().strftime("%d/%m/%Y %H:%M")
                alert = (
                    f"\U0001f6a8 *ALERTA COMERCIAL*\n\n"
                    f"Time, atenção!\n\n"
                    f"O lead *{lead_name}* acabou de responder no WhatsApp.\n\n"
                    f"\U0001f4de Telefone: {phone_display}\n"
                    f"\U0001f552 Data/Hora: {data_hora}\n"
                    f"\U0001f4ac Mensagem: \"{text.strip()}\"\n\n"
                    f"⚡ *URGENTE:* entrar em contato o quanto antes!"
                )
                try:
                    headers = {"apikey": settings["api_key"], "Content-Type": "application/json"}
                    requests.post(
                        f"{settings['evolution_url']}/message/sendText/{settings['instance']}",
                        json={"number": vendor_jid, "text": alert},
                        headers=headers,
                        timeout=10,
                    )
                except Exception:
                    pass

            handled = handle_incoming(phone=phone, raw_text=text, db=db, settings=settings)
            print(f"[webhook] handled={handled} phone={phone}")
            results.append({"phone": phone, "handled": handled})
        except Exception as exc:
            print(f"[webhook] ERROR processing {phone}: {exc}")
            import traceback; traceback.print_exc()
        finally:
            db.close()

    return {"ok": True, "processed": len(results), "results": results}


@app.post("/webhook/configure")
def configure_webhook():
    import json as _json
    from config import load_settings
    settings = load_settings()
    base_url = settings.get("webhook_base_url", "http://localhost:8000")
    webhook_url = f"{base_url}/webhook/evolution"

    # Monta o JSON manualmente para evitar que o linter altere os valores
    _ev = "MESSAGES_UPSERT"
    body = _json.dumps({
        "webhook": {
            "url": webhook_url,
            "enabled": True,
            "webhookByEvents": False,
            "webhookBase64": False,
            "events": [_ev],
        }
    })

    headers = {"apikey": settings["api_key"], "Content-Type": "application/json"}
    try:
        r = requests.post(
            f"{settings['evolution_url']}/webhook/set/{settings['instance']}",
            data=body.encode(), headers=headers, timeout=10,
        )
        return {"ok": r.ok, "status": r.status_code, "webhook_url": webhook_url, "response": r.text}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# =========================
# REUNIÕES AGENDADAS
# =========================
@app.get("/meetings")
def get_meetings(db: Session = Depends(get_db)):
    return db.query(models.ScheduledMeeting).order_by(
        models.ScheduledMeeting.created_at.desc()
    ).all()


@app.delete("/meetings/{meeting_id}")
def delete_meeting(meeting_id: int, db: Session = Depends(get_db)):
    m = db.query(models.ScheduledMeeting).filter(models.ScheduledMeeting.id == meeting_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Reunião não encontrada")
    db.delete(m)
    db.commit()
    return {"ok": True}


# =========================
# ESTADOS DE CONVERSA
# =========================
@app.get("/conversations")
def get_conversations(db: Session = Depends(get_db)):
    return db.query(models.ConversationState).order_by(
        models.ConversationState.updated_at.desc()
    ).all()


@app.delete("/conversations/{phone}")
def reset_conversation(phone: str, db: Session = Depends(get_db)):
    conv = db.query(models.ConversationState).filter(models.ConversationState.phone == phone).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversa não encontrada")
    conv.state = "idle"
    conv.selected_date = None
    conv.selected_time = None
    conv.meet_link = None
    conv.calendar_event_id = None
    conv.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


# =========================
# STATUS GOOGLE
# =========================
@app.get("/google/status")
def google_status():
    from services.google_meet import is_configured, is_authenticated
    return {
        "credentials_file": is_configured(),
        "authenticated": is_authenticated(),
    }


# =========================
# TESTE – simula resposta de lead sem precisar do WhatsApp
# Uso: POST /testar?phone=5567991879095&texto=2
# =========================
@app.post("/testar")
def testar_fluxo(phone: str, texto: str = "oi", db: Session = Depends(get_db)):
    from config import load_settings
    from services.scheduling_flow import handle_incoming
    settings = load_settings()
    lead = db.query(models.Lead).filter(models.Lead.phone == phone).first()
    if not lead:
        todos = [l.phone for l in db.query(models.Lead).limit(5).all()]
        return {"erro": f"Lead {phone} nao encontrado", "phones": todos}
    try:
        handled = handle_incoming(phone=phone, raw_text=texto, db=db, settings=settings)
        conv = db.query(models.ConversationState).filter(models.ConversationState.phone == phone).first()
        return {"ok": True, "handled": handled, "lead": lead.name, "estado": conv.state if conv else None}
    except Exception as exc:
        import traceback
        return {"ok": False, "erro": str(exc), "detalhe": traceback.format_exc()}


# =========================
# PARTICIPANTES (TIME COMERCIAL)
# =========================
@app.get("/participantes")
def get_participantes(db: Session = Depends(get_db)):
    return db.query(models.Participante).order_by(models.Participante.id).all()


@app.post("/participantes")
def create_participante(body: dict, db: Session = Depends(get_db)):
    nome = (body.get("nome") or "").strip()
    email = (body.get("email") or "").strip().lower()
    if not nome or not email:
        raise HTTPException(status_code=400, detail="Nome e email são obrigatórios")
    if db.query(models.Participante).filter(models.Participante.email == email).first():
        raise HTTPException(status_code=400, detail="Email já cadastrado")
    p = models.Participante(nome=nome, email=email)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@app.put("/participantes/{pid}")
def update_participante(pid: int, body: dict, db: Session = Depends(get_db)):
    p = db.query(models.Participante).filter(models.Participante.id == pid).first()
    if not p:
        raise HTTPException(status_code=404, detail="Participante não encontrado")
    nome = (body.get("nome") or "").strip()
    email = (body.get("email") or "").strip().lower()
    if not nome or not email:
        raise HTTPException(status_code=400, detail="Nome e email são obrigatórios")
    conflito = db.query(models.Participante).filter(
        models.Participante.email == email, models.Participante.id != pid
    ).first()
    if conflito:
        raise HTTPException(status_code=400, detail="Email já cadastrado por outro participante")
    p.nome = nome
    p.email = email
    db.commit()
    db.refresh(p)
    return p


@app.patch("/participantes/{pid}/toggle")
def toggle_participante(pid: int, db: Session = Depends(get_db)):
    p = db.query(models.Participante).filter(models.Participante.id == pid).first()
    if not p:
        raise HTTPException(status_code=404, detail="Participante não encontrado")
    p.ativo = not p.ativo
    db.commit()
    db.refresh(p)
    return p


@app.delete("/participantes/{pid}")
def delete_participante(pid: int, db: Session = Depends(get_db)):
    p = db.query(models.Participante).filter(models.Participante.id == pid).first()
    if not p:
        raise HTTPException(status_code=404, detail="Participante não encontrado")
    db.delete(p)
    db.commit()
    return {"ok": True}