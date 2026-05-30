from fastapi import FastAPI, BackgroundTasks, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from io import StringIO
import csv
import requests
import base64
import re
import random
import asyncio
import os as _os
from datetime import datetime

import time
import models
import schemas
import auth
from db import SessionLocal, engine

# =========================
# INIT
# =========================
models.Base.metadata.create_all(bind=engine)


def _run_migrations():
    """Add columns introduced after the initial schema creation."""
    from sqlalchemy import text
    with engine.connect() as conn:
        for sql in [
            "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS messages_json TEXT",
            "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS followup_sent BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE scheduled_meetings ADD COLUMN IF NOT EXISTS reminder_24h_sent BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE scheduled_meetings ADD COLUMN IF NOT EXISTS reminder_1h_sent  BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS etapa VARCHAR DEFAULT 'Novo Lead'",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS status_interesse VARCHAR",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS vendedor VARCHAR",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS board_id INTEGER",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS origem_lead VARCHAR",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS custo_campanha FLOAT",
            """CREATE TABLE IF NOT EXISTS lead_obs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL REFERENCES leads(id),
                texto TEXT NOT NULL,
                autor TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
            "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS perfil VARCHAR NOT NULL DEFAULT 'vendedor'",
            "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS primeiro_login BOOLEAN NOT NULL DEFAULT false",
            "UPDATE usuarios SET perfil = 'admin', primeiro_login = false WHERE email = 'admin@gestorpec.com.br'",
        ]:
            try:
                conn.execute(text(sql))
            except Exception:
                pass
        conn.commit()


_run_migrations()


def _seed_admin():
    """Cria o usuário admin padrão se não existir nenhum usuário."""
    db = SessionLocal()
    try:
        if db.query(models.Usuario).count() == 0:
            db.add(models.Usuario(
                nome="Administrador",
                email="admin@gestorpec.com.br",
                senha_hash=auth.hash_password("admin123"),
                ativo=True,
            ))
            db.commit()
            print("[auth] Usuário admin criado: admin@gestorpec.com.br / admin123")
    finally:
        db.close()


_seed_admin()


_ETAPAS_DEFAULT = [
    "Novo Lead", "Contato Iniciado", "Engajado", "Ligação Realizada",
    "Apresentação Agendada", "Apresentação Realizada", "Proposta",
    "Negociação", "Fechado", "Grupo Criado", "Aguardando Pagamento",
    "Implantação Agendada", "Em Implantação", "Entrega Técnica",
]


def _seed_default_board():
    """Cria o quadro padrão (id=1) se ainda não existir."""
    import json as _json
    db = SessionLocal()
    try:
        if not db.query(models.KanbanBoard).filter(models.KanbanBoard.id == 1).first():
            db.add(models.KanbanBoard(id=1, nome="Pipeline de Vendas",
                                      etapas=_json.dumps(_ETAPAS_DEFAULT)))
            db.commit()
    finally:
        db.close()


_seed_default_board()


async def _reminder_loop():
    from config import load_settings
    from services.reminder import check_and_send as reminders_check
    from services.followup import check_and_send as followup_check

    iteration = 0
    while True:
        await asyncio.sleep(300)  # a cada 5 minutos
        iteration += 1
        settings = load_settings()

        # Lembretes de reunião — roda toda iteração (5 min)
        try:
            db = SessionLocal()
            sent = reminders_check(db, settings)
            if sent:
                print(f"[reminders] {sent} lembrete(s) enviado(s)")
        except Exception as exc:
            print(f"[reminders] error: {exc}")
        finally:
            db.close()

        # Follow-up de leads sumidos — roda a cada 6 iterações (30 min)
        if iteration % 6 == 0:
            try:
                db = SessionLocal()
                sent = followup_check(db, settings)
                if sent:
                    print(f"[followup] {sent} follow-up(s) enviado(s)")
            except Exception as exc:
                print(f"[followup] error: {exc}")
            finally:
                db.close()


app = FastAPI()

# Rotas públicas — não exigem token
_PUBLIC = {"/", "/auth/login", "/whatsapp/qr", "/whatsapp/status", "/whatsapp/connect", "/google/status"}
_PUBLIC_PREFIX = "/webhook"


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if path in _PUBLIC or path.startswith(_PUBLIC_PREFIX):
        return await call_next(request)

    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    if not token or not auth.decode_token(token):
        return JSONResponse({"detail": "Não autenticado"}, status_code=401)

    return await call_next(request)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(_reminder_loop())


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

import os
EVOLUTION_URL = os.getenv("EVOLUTION_API_URL", "http://127.0.0.1:8080")
API_KEY       = os.getenv("EVOLUTION_API_KEY", "ev_api_123456_mt_local")
INSTANCE      = os.getenv("EVOLUTION_INSTANCE", "minha_instancia")

# In-memory QR store — populated by QRCODE_UPDATED webhook event
_qr_store: dict = {"base64": None}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# =========================
# AUTH
# =========================
@app.post("/auth/login", response_model=schemas.TokenResponse)
def login(body: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.Usuario).filter(
        models.Usuario.email == body.email,
        models.Usuario.ativo == True,
    ).first()
    if not user or not auth.verify_password(body.senha, user.senha_hash):
        raise HTTPException(status_code=401, detail="Email ou senha incorretos")
    return schemas.TokenResponse(
        token=auth.create_token(user.email),
        nome=user.nome,
        email=user.email,
        perfil=getattr(user, "perfil", "vendedor") or "vendedor",
        primeiro_login=bool(getattr(user, "primeiro_login", False)),
    )


@app.get("/auth/me", response_model=schemas.UsuarioResponse)
def me(request: Request, db: Session = Depends(get_db)):
    token = request.headers.get("Authorization", "")[7:]
    email = auth.decode_token(token)
    user = db.query(models.Usuario).filter(models.Usuario.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    return user


@app.get("/auth/usuarios", response_model=list[schemas.UsuarioResponse])
def listar_usuarios(db: Session = Depends(get_db)):
    return db.query(models.Usuario).order_by(models.Usuario.id).all()


@app.post("/auth/usuarios", response_model=schemas.UsuarioCriadoResponse)
def criar_usuario(body: schemas.UsuarioCreate, db: Session = Depends(get_db)):
    import secrets, string as _string
    if db.query(models.Usuario).filter(models.Usuario.email == body.email).first():
        raise HTTPException(status_code=400, detail="Email já cadastrado")
    senha_temp = None
    if body.senha:
        senha_hash = auth.hash_password(body.senha)
        primeiro = False
    else:
        senha_temp = ''.join(secrets.choice(_string.ascii_letters + _string.digits) for _ in range(10))
        senha_hash = auth.hash_password(senha_temp)
        primeiro = True
    user = models.Usuario(
        nome=body.nome,
        email=body.email,
        senha_hash=senha_hash,
        perfil=body.perfil or "vendedor",
        primeiro_login=primeiro,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {
        "id": user.id,
        "nome": user.nome,
        "email": user.email,
        "perfil": user.perfil,
        "ativo": user.ativo,
        "created_at": user.created_at,
        "senha_temporaria": senha_temp,
    }


@app.put("/auth/usuarios/{uid}", response_model=schemas.UsuarioResponse)
def editar_usuario(uid: int, body: schemas.UsuarioUpdate, request: Request, db: Session = Depends(get_db)):
    token = request.headers.get("Authorization", "")[7:]
    caller_email = auth.decode_token(token)
    user = db.query(models.Usuario).filter(models.Usuario.id == uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    if body.nome is not None:
        user.nome = body.nome
    if body.email is not None:
        conflict = db.query(models.Usuario).filter(
            models.Usuario.email == body.email,
            models.Usuario.id != uid
        ).first()
        if conflict:
            raise HTTPException(status_code=400, detail="Email já cadastrado")
        user.email = body.email
    if body.perfil is not None:
        user.perfil = body.perfil
    if body.ativo is not None:
        if user.email == caller_email and not body.ativo:
            raise HTTPException(status_code=400, detail="Não é possível desativar o próprio usuário")
        user.ativo = body.ativo
    db.commit()
    db.refresh(user)
    return user


@app.post("/auth/usuarios/{uid}/reset-senha")
def reset_senha_usuario(uid: int, request: Request, db: Session = Depends(get_db)):
    import secrets, string as _string
    token = request.headers.get("Authorization", "")[7:]
    caller_email = auth.decode_token(token)
    user = db.query(models.Usuario).filter(models.Usuario.id == uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    if user.email == caller_email:
        raise HTTPException(status_code=400, detail="Use /auth/trocar-senha para alterar a própria senha")
    nova_senha = ''.join(secrets.choice(_string.ascii_letters + _string.digits) for _ in range(10))
    user.senha_hash = auth.hash_password(nova_senha)
    user.primeiro_login = True
    db.commit()
    return {"ok": True, "nova_senha": nova_senha, "email": user.email, "nome": user.nome}


@app.delete("/auth/usuarios/{uid}")
def deletar_usuario(uid: int, request: Request, db: Session = Depends(get_db)):
    token = request.headers.get("Authorization", "")[7:]
    email = auth.decode_token(token)
    user = db.query(models.Usuario).filter(models.Usuario.id == uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    if user.email == email:
        raise HTTPException(status_code=400, detail="Não é possível excluir o próprio usuário")
    db.delete(user)
    db.commit()
    return {"ok": True}


@app.post("/auth/trocar-senha")
def trocar_senha(body: schemas.SenhaUpdate, request: Request, db: Session = Depends(get_db)):
    token = request.headers.get("Authorization", "")[7:]
    email = auth.decode_token(token)
    user = db.query(models.Usuario).filter(models.Usuario.email == email).first()
    if not user or not auth.verify_password(body.senha_atual, user.senha_hash):
        raise HTTPException(status_code=401, detail="Senha atual incorreta")
    user.senha_hash = auth.hash_password(body.nova_senha)
    user.primeiro_login = False
    db.commit()
    return {"ok": True}


# =========================
# DASHBOARD – Indicadores de Performance
# =========================
@app.get("/dashboard/stats")
def get_dashboard_stats(board_id: int = 1, db: Session = Depends(get_db)):
    import json as _json
    from sqlalchemy import func, or_
    from datetime import date, timedelta

    today_date = datetime.utcnow().date()
    week_ago   = datetime.utcnow() - timedelta(days=7)
    month_ago  = datetime.utcnow() - timedelta(days=30)

    # ── Totais de leads ─────────────────────────────────────────
    total       = db.query(func.count(models.Lead.id)).scalar() or 0
    leads_hoje  = db.query(func.count(models.Lead.id)).filter(
        func.date(models.Lead.created_at) == today_date
    ).scalar() or 0
    leads_semana = db.query(func.count(models.Lead.id)).filter(
        models.Lead.created_at >= week_ago
    ).scalar() or 0
    leads_mes = db.query(func.count(models.Lead.id)).filter(
        models.Lead.created_at >= month_ago
    ).scalar() or 0

    # ── Leads que responderam (têm ConversationState) ───────────
    phones_conv = db.query(models.ConversationState.phone)
    responderam = db.query(func.count(models.Lead.id)).filter(
        models.Lead.phone.in_(phones_conv)
    ).scalar() or 0
    pct_resposta = round(responderam / total * 100, 1) if total else 0

    # ── Por campanha (top 10) ───────────────────────────────────
    por_campanha_q = (
        db.query(models.Lead.campaign_name, func.count(models.Lead.id).label("total"))
        .group_by(models.Lead.campaign_name)
        .order_by(func.count(models.Lead.id).desc())
        .limit(10)
        .all()
    )
    por_campanha = [
        {"campanha": row.campaign_name or "(sem campanha)", "total": row.total}
        for row in por_campanha_q
    ]

    # ── Por etapa (board específico) ────────────────────────────
    board = db.query(models.KanbanBoard).filter(models.KanbanBoard.id == board_id).first()
    etapas = _json.loads(board.etapas) if board else []
    por_etapa: dict = {e: 0 for e in etapas}

    filtro_board = (
        or_(models.Lead.board_id == board_id, models.Lead.board_id.is_(None))
        if board_id == 1
        else models.Lead.board_id == board_id
    )
    por_etapa_q = (
        db.query(models.Lead.etapa, func.count(models.Lead.id).label("total"))
        .filter(filtro_board)
        .group_by(models.Lead.etapa)
        .all()
    )
    for row in por_etapa_q:
        key = row.etapa or etapas[0] if etapas else "Novo Lead"
        if key in por_etapa:
            por_etapa[key] = row.total

    # ── Por status de interesse ─────────────────────────────────
    por_interesse: dict = {"quente": 0, "morno": 0, "frio": 0, "sem_classificacao": 0}
    for row in db.query(models.Lead.status_interesse, func.count(models.Lead.id).label("total")) \
                 .group_by(models.Lead.status_interesse).all():
        k = row.status_interesse or "sem_classificacao"
        if k in por_interesse:
            por_interesse[k] = row.total
        else:
            por_interesse["sem_classificacao"] += row.total

    # ── Performance por vendedor ────────────────────────────────
    por_vendedor_q = (
        db.query(models.Lead.vendedor, func.count(models.Lead.id).label("leads"))
        .filter(models.Lead.vendedor.isnot(None))
        .group_by(models.Lead.vendedor)
        .all()
    )
    por_vendedor = []
    for row in por_vendedor_q:
        fechados = db.query(func.count(models.Lead.id)).filter(
            models.Lead.vendedor == row.vendedor,
            models.Lead.etapa.in_(["Fechado", "Grupo Criado", "Aguardando Pagamento",
                                   "Implantação Agendada", "Em Implantação", "Entrega Técnica"]),
        ).scalar() or 0
        reunioes = db.query(func.count(models.ScheduledMeeting.id)).filter(
            models.ScheduledMeeting.lead_phone.in_(
                db.query(models.Lead.phone).filter(models.Lead.vendedor == row.vendedor)
            )
        ).scalar() or 0
        por_vendedor.append({
            "vendedor": row.vendedor,
            "leads": row.leads,
            "reunioes": reunioes,
            "fechados": fechados,
            "pct_conversao": round(fechados / row.leads * 100, 1) if row.leads else 0,
        })
    por_vendedor.sort(key=lambda x: x["fechados"], reverse=True)

    # ── Reuniões ────────────────────────────────────────────────
    total_reunioes  = db.query(func.count(models.ScheduledMeeting.id)).scalar() or 0
    confirmadas     = db.query(func.count(models.ScheduledMeeting.id)).filter(
        models.ScheduledMeeting.status == "confirmado"
    ).scalar() or 0
    canceladas      = db.query(func.count(models.ScheduledMeeting.id)).filter(
        models.ScheduledMeeting.status == "cancelado"
    ).scalar() or 0
    taxa_comp = round(confirmadas / total_reunioes * 100, 1) if total_reunioes else 0

    # ── Funil de conversão ──────────────────────────────────────
    def _count(*etapas_list):
        return db.query(func.count(models.Lead.id)).filter(
            models.Lead.etapa.in_(etapas_list)
        ).scalar() or 0

    ETAPAS_POS_CONTATO   = ["Contato Iniciado","Engajado","Ligação Realizada","Apresentação Agendada",
                             "Apresentação Realizada","Proposta","Negociação","Fechado","Grupo Criado",
                             "Aguardando Pagamento","Implantação Agendada","Em Implantação","Entrega Técnica"]
    ETAPAS_POS_LIGACAO   = ["Ligação Realizada","Apresentação Agendada","Apresentação Realizada",
                             "Proposta","Negociação","Fechado","Grupo Criado","Aguardando Pagamento",
                             "Implantação Agendada","Em Implantação","Entrega Técnica"]
    ETAPAS_POS_APRES     = ["Apresentação Agendada","Apresentação Realizada","Proposta","Negociação",
                             "Fechado","Grupo Criado","Aguardando Pagamento","Implantação Agendada",
                             "Em Implantação","Entrega Técnica"]
    ETAPAS_POS_PROPOSTA  = ["Proposta","Negociação","Fechado","Grupo Criado","Aguardando Pagamento",
                             "Implantação Agendada","Em Implantação","Entrega Técnica"]
    ETAPAS_FECHADO       = ["Fechado","Grupo Criado","Aguardando Pagamento",
                             "Implantação Agendada","Em Implantação","Entrega Técnica"]

    n_contato   = _count(*ETAPAS_POS_CONTATO)
    n_ligacao   = _count(*ETAPAS_POS_LIGACAO)
    n_apres     = _count(*ETAPAS_POS_APRES)
    n_proposta  = _count(*ETAPAS_POS_PROPOSTA)
    n_fechados  = _count(*ETAPAS_FECHADO)

    def _pct(n): return round(n / total * 100, 1) if total else 0

    funil = [
        {"label": "Total Leads",       "valor": total,     "pct": 100},
        {"label": "Contato Iniciado",  "valor": n_contato, "pct": _pct(n_contato)},
        {"label": "Ligação Realizada", "valor": n_ligacao, "pct": _pct(n_ligacao)},
        {"label": "Apresentação",      "valor": n_apres,   "pct": _pct(n_apres)},
        {"label": "Proposta",          "valor": n_proposta,"pct": _pct(n_proposta)},
        {"label": "Fechado",           "valor": n_fechados,"pct": _pct(n_fechados)},
    ]

    # ── Follow-up / mensagens ───────────────────────────────────
    followups    = db.query(func.count(models.ConversationState.id)).filter(
        models.ConversationState.followup_sent == True
    ).scalar() or 0
    total_msgs   = db.query(func.count(models.Message.id)).scalar() or 0
    media_msgs   = round(total_msgs / total, 1) if total else 0

    return {
        "resumo": {
            "total_leads": total,
            "leads_hoje": leads_hoje,
            "leads_semana": leads_semana,
            "leads_mes": leads_mes,
            "responderam": responderam,
            "pct_resposta": pct_resposta,
            "followups_enviados": followups,
            "media_msgs_por_lead": media_msgs,
        },
        "reunioes": {
            "total": total_reunioes,
            "confirmadas": confirmadas,
            "canceladas": canceladas,
            "taxa_comparecimento": taxa_comp,
        },
        "funil": funil,
        "por_campanha": por_campanha,
        "por_etapa": por_etapa,
        "por_interesse": por_interesse,
        "por_vendedor": por_vendedor,
    }


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
    # Normaliza: remove não-dígitos e adiciona DDI 55 se necessário
    phone = re.sub(r'\D', '', lead.phone)
    if len(phone) in [10, 11]:
        phone = f"55{phone}"

    existing = db.query(models.Lead).filter(models.Lead.phone == phone).first()
    if existing:
        raise HTTPException(status_code=400, detail="Telefone já cadastrado")

    db_lead = models.Lead(
        name=lead.name,
        phone=phone,
        status="pendente",
        etapa="Novo Lead",
        board_id=1,
    )

    db.add(db_lead)
    db.commit()
    db.refresh(db_lead)

    return db_lead


@app.get("/leads")
def get_leads(source: str = None, db: Session = Depends(get_db)):
    q = db.query(models.Lead)
    if source == "csv":
        q = q.filter(models.Lead.origem_lead == "Planilha CSV")
    return q.all()

@app.delete("/leads/{lead_id}")
def delete_lead(lead_id: int, db: Session = Depends(get_db)):
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    db.query(models.LeadObs).filter(models.LeadObs.lead_id == lead_id).delete()
    db.query(models.Message).filter(models.Message.lead_id == lead_id).delete()
    db.delete(lead)
    db.commit()
    return {"ok": True}


@app.delete("/leads")
def delete_all_leads(db: Session = Depends(get_db)):
    try:
        db.query(models.Message).delete()
        db.query(models.LeadObs).delete()
        db.query(models.Lead).delete()
        db.commit()
        return {"ok": True, "message": "Banco de dados limpo com sucesso!"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Erro ao limpar banco: {str(e)}")

# =========================
# KANBAN CRM — QUADROS
# =========================
@app.get("/kanban/boards", response_model=list[schemas.KanbanBoardResponse])
def list_boards(db: Session = Depends(get_db)):
    return db.query(models.KanbanBoard).order_by(models.KanbanBoard.id).all()


@app.post("/kanban/boards", response_model=schemas.KanbanBoardResponse)
def create_board(body: dict, db: Session = Depends(get_db)):
    import json as _json
    nome   = (body.get("nome") or "").strip()
    etapas = body.get("etapas") or _ETAPAS_DEFAULT
    if not nome:
        raise HTTPException(status_code=400, detail="Nome é obrigatório")
    b = models.KanbanBoard(nome=nome, etapas=_json.dumps(etapas))
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


@app.put("/kanban/boards/{board_id}", response_model=schemas.KanbanBoardResponse)
def update_board(board_id: int, body: dict, db: Session = Depends(get_db)):
    import json as _json
    b = db.query(models.KanbanBoard).filter(models.KanbanBoard.id == board_id).first()
    if not b:
        raise HTTPException(status_code=404, detail="Quadro não encontrado")
    if "nome" in body:
        b.nome = (body["nome"] or "").strip() or b.nome
    if "etapas" in body:
        b.etapas = _json.dumps(body["etapas"])
    db.commit()
    db.refresh(b)
    return b


@app.delete("/kanban/boards/{board_id}")
def delete_board(board_id: int, db: Session = Depends(get_db)):
    if board_id == 1:
        raise HTTPException(status_code=400, detail="O quadro padrão não pode ser excluído")
    b = db.query(models.KanbanBoard).filter(models.KanbanBoard.id == board_id).first()
    if not b:
        raise HTTPException(status_code=404, detail="Quadro não encontrado")
    # Move leads do quadro deletado para o quadro padrão
    db.query(models.Lead).filter(models.Lead.board_id == board_id).update(
        {"board_id": 1, "etapa": "Novo Lead"})
    db.delete(b)
    db.commit()
    return {"ok": True}


@app.get("/leads/kanban")
def get_leads_kanban(board_id: int = 1, db: Session = Depends(get_db)):
    import json as _json
    from sqlalchemy import func
    board = db.query(models.KanbanBoard).filter(models.KanbanBoard.id == board_id).first()
    if not board:
        raise HTTPException(status_code=404, detail="Quadro não encontrado")
    etapas = _json.loads(board.etapas)
    result = {e: [] for e in etapas}
    leads  = db.query(models.Lead).filter(
        models.Lead.board_id == board_id
    ).order_by(models.Lead.created_at.desc()).all()
    obs_counts = dict(
        db.query(models.LeadObs.lead_id, func.count(models.LeadObs.id))
        .group_by(models.LeadObs.lead_id)
        .all()
    )
    for lead in leads:
        etapa = lead.etapa if lead.etapa in result else etapas[0]
        result[etapa].append({
            "id": lead.id,
            "name": lead.name or "",
            "phone": lead.phone,
            "etapa": etapa,
            "status_interesse": lead.status_interesse or "",
            "vendedor": lead.vendedor or "",
            "status": lead.status,
            "sent_at": lead.sent_at.strftime("%d/%m %H:%M") if lead.sent_at else "",
            "obs_count": obs_counts.get(lead.id, 0),
            "created_at": lead.created_at.strftime("%d/%m/%Y") if lead.created_at else "",
            "origem_lead": lead.origem_lead or "",
            "campaign_name": lead.campaign_name or "",
            "custo_campanha": lead.custo_campanha,
        })
    return {"board_id": board_id, "board_nome": board.nome, "etapas": etapas, "board": result}


@app.patch("/leads/{lead_id}/etapa")
def update_lead_etapa(lead_id: int, body: dict, db: Session = Depends(get_db)):
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    for field in ("etapa", "status_interesse", "vendedor", "board_id", "origem_lead", "campaign_name"):
        if field in body:
            setattr(lead, field, body[field] or None)
    if "custo_campanha" in body:
        lead.custo_campanha = float(body["custo_campanha"]) if body["custo_campanha"] not in (None, "", 0, "0") else None
    db.commit()
    return {"ok": True}


# =========================
# LEAD OBS — Histórico de interações
# =========================
@app.get("/leads/{lead_id}/obs", response_model=list[schemas.LeadObsResponse])
def get_lead_obs(lead_id: int, db: Session = Depends(get_db)):
    return (
        db.query(models.LeadObs)
        .filter(models.LeadObs.lead_id == lead_id)
        .order_by(models.LeadObs.created_at.desc())
        .all()
    )


@app.post("/leads/{lead_id}/obs", response_model=schemas.LeadObsResponse)
def add_lead_obs(lead_id: int, body: schemas.LeadObsCreate, db: Session = Depends(get_db)):
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    obs = models.LeadObs(lead_id=lead_id, texto=body.texto.strip(), autor=(body.autor or "").strip() or None)
    db.add(obs)
    db.commit()
    db.refresh(obs)
    return obs


@app.put("/leads/{lead_id}/obs/{obs_id}", response_model=schemas.LeadObsResponse)
def update_lead_obs(lead_id: int, obs_id: int, body: schemas.LeadObsCreate, db: Session = Depends(get_db)):
    obs = db.query(models.LeadObs).filter(
        models.LeadObs.id == obs_id,
        models.LeadObs.lead_id == lead_id,
    ).first()
    if not obs:
        raise HTTPException(status_code=404, detail="Observação não encontrada")
    obs.texto = body.texto.strip()
    db.commit()
    db.refresh(obs)
    return obs


@app.delete("/leads/{lead_id}/obs/{obs_id}")
def delete_lead_obs(lead_id: int, obs_id: int, db: Session = Depends(get_db)):
    obs = db.query(models.LeadObs).filter(
        models.LeadObs.id == obs_id,
        models.LeadObs.lead_id == lead_id,
    ).first()
    if not obs:
        raise HTTPException(status_code=404, detail="Observação não encontrada")
    db.delete(obs)
    db.commit()
    return {"ok": True}


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
                    status=status_planilha,
                    origem_lead="Planilha CSV",
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
    _backend_dir = _os.path.dirname(_os.path.abspath(__file__))
    file_path = _os.path.join(_backend_dir, "..", "dados", "leads.csv")
    
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
# DISPARO
# =========================

def _run_disparo(campaign_name: str, leads_snapshot: list, templates_text: list,
                 b64_media, mimetype, filename):
    """Runs in a background thread — sends messages with anti-ban delays."""
    from config import load_settings
    settings = load_settings()
    evo_url  = settings.get("evolution_url", EVOLUTION_URL)
    api_key  = settings.get("api_key", API_KEY)
    instance = settings.get("instance", INSTANCE)
    headers  = {"apikey": api_key, "Content-Type": "application/json"}

    is_image = bool(filename and filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')))
    media_type = "image" if is_image else "document"

    db = SessionLocal()
    try:
        for i, (lead_id, lead_name, lead_phone) in enumerate(leads_snapshot):
            lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
            if not lead or lead.status == "enviado":
                continue

            text = re.sub(r'{\s*name\s*}', lead_name or "", random.choice(templates_text), flags=re.IGNORECASE)
            final_code = 500

            try:
                if b64_media:
                    order = random.choice(['media_first', 'text_first'])
                    if order == 'media_first':
                        r = requests.post(
                            f"{evo_url}/message/sendMedia/{instance}",
                            json={"number": lead_phone, "mediatype": media_type, "mimetype": mimetype, "caption": text, "media": b64_media},
                            headers=headers, timeout=30)
                        final_code = r.status_code
                        print(f"[disparo] media_first {lead_phone}: {r.status_code}")
                    else:
                        r1 = requests.post(f"{evo_url}/message/sendText/{instance}",
                                           json={"number": lead_phone, "text": text},
                                           headers=headers, timeout=15)
                        time.sleep(random.uniform(2, 8))
                        r2 = requests.post(
                            f"{evo_url}/message/sendMedia/{instance}",
                            json={"number": lead_phone, "mediatype": media_type, "mimetype": mimetype, "media": b64_media},
                            headers=headers, timeout=30)
                        final_code = r2.status_code if r1.ok else r1.status_code
                        print(f"[disparo] text_first {lead_phone}: txt={r1.status_code} media={r2.status_code}")
                else:
                    r = requests.post(f"{evo_url}/message/sendText/{instance}",
                                      json={"number": lead_phone, "text": text},
                                      headers=headers, timeout=15)
                    final_code = r.status_code
                    print(f"[disparo] text_only {lead_phone}: {r.status_code}")
            except Exception as exc:
                print(f"[disparo] erro {lead_phone}: {exc}")
                final_code = 500

            lead.status = "enviado" if 200 <= final_code < 300 else "falhou"
            lead.campaign_name = campaign_name
            lead.sent_message = text
            lead.sent_at = datetime.utcnow()
            db.commit()

            if i < len(leads_snapshot) - 1:
                delay = random.uniform(20, 90)
                print(f"[disparo] aguardando {delay:.0f}s...")
                time.sleep(delay)

        # Atualiza planilha local se existir (path relativo ao backend)
        try:
            _backend_dir = _os.path.dirname(_os.path.abspath(__file__))
            output_dir = _os.path.join(_backend_dir, "..", "dados")
            if _os.path.exists(output_dir):
                file_path = _os.path.join(output_dir, "leads.csv")
                all_leads = db.query(models.Lead).all()
                with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.writer(f, delimiter=";", quoting=csv.QUOTE_ALL)
                    writer.writerow(["Nome", "Telefone", "Status", "Mensagem Enviada", "Data Disparo", "Campanha"])
                    for l in all_leads:
                        sent_at_str = l.sent_at.strftime("%Y-%m-%d %H:%M:%S") if l.sent_at else ""
                        writer.writerow([l.name, l.phone, l.status, l.sent_message, sent_at_str, l.campaign_name])
        except Exception as e:
            print(f"[disparo] erro ao salvar CSV: {e}")

        print(f"[disparo] campanha '{campaign_name}' finalizada.")
    finally:
        db.close()


@app.post("/send")
async def send(
    background_tasks: BackgroundTasks,
    campaign_name: str = Form(...),
    file: UploadFile = File(None),
    db: Session = Depends(get_db)
):
    templates = db.query(models.MessageTemplate).all()
    if not templates:
        raise HTTPException(status_code=400, detail="Nenhuma mensagem cadastrada para o disparo.")

    leads = db.query(models.Lead).filter(models.Lead.status != "enviado").all()
    if not leads:
        return {"ok": True, "iniciado": False, "total": 0, "message": "Nenhum lead pendente."}

    b64_media = None
    mimetype = None
    filename = None
    if file and file.filename:
        content = await file.read()
        if content:
            b64_media = base64.b64encode(content).decode('utf-8')
            mimetype = file.content_type
            filename = file.filename

    leads_snapshot = [(l.id, l.name or "", l.phone) for l in leads]
    templates_text = [t.text for t in templates]

    background_tasks.add_task(
        _run_disparo,
        campaign_name, leads_snapshot, templates_text, b64_media, mimetype, filename
    )

    return {
        "ok": True,
        "iniciado": True,
        "total": len(leads),
        "message": f"Disparo iniciado para {len(leads)} lead(s). Os envios ocorrem em background com delays anti-ban."
    }

# =========================
# CONEXÃO WHATSAPP
# =========================
@app.get("/whatsapp/status")
def get_whatsapp_status():
    headers = {"apikey": API_KEY}
    try:
        r = requests.get(f"{EVOLUTION_URL}/instance/connectionState/{INSTANCE}", headers=headers, timeout=5)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _extract_b64(data: dict):
    return (
        data.get("base64")
        or (data.get("qrcode") or {}).get("base64")
        or data.get("code")
    )


def _register_webhook_for_instance(instance_name: str, api_key: str, evolution_url: str):
    """Register the evolution webhook silently; failures are non-fatal."""
    import json as _json
    from config import load_settings
    try:
        settings = load_settings()
        # Use internal Docker URL so Evolution API (inside Docker) can reach the backend
        internal_url = os.getenv("EVOLUTION_WEBHOOK_URL") or settings.get("webhook_base_url", "http://localhost:8000")
        webhook_url = f"{internal_url}/webhook/evolution"
        body = _json.dumps({
            "webhook": {
                "url": webhook_url,
                "enabled": True,
                "webhookByEvents": False,
                "webhookBase64": False,
                "events": ["MESSAGES_UPSERT", "QRCODE_UPDATED", "CONNECTION_UPDATE"],
            }
        })
        requests.post(
            f"{evolution_url}/webhook/set/{instance_name}",
            data=body.encode(),
            headers={"apikey": api_key, "Content-Type": "application/json"},
            timeout=10,
        )
    except Exception:
        pass


@app.get("/whatsapp/connect")
def connect_whatsapp():
    headers = {"apikey": API_KEY}
    try:
        state_r = requests.get(
            f"{EVOLUTION_URL}/instance/connectionState/{INSTANCE}",
            headers=headers, timeout=5,
        )

        if state_r.status_code == 200:
            state_data = state_r.json()
            instance_state = (
                (state_data.get("instance") or {}).get("state")
                or state_data.get("state", "")
            )
            if instance_state == "open":
                return {"connected": True, "state": "open"}

            if instance_state in ("connecting", "close"):
                # If QR already stored, return it immediately
                if _qr_store.get("base64"):
                    return {"base64": _qr_store["base64"]}
                # Trigger connect so Baileys sends QR via QRCODE_UPDATED webhook
                requests.get(f"{EVOLUTION_URL}/instance/connect/{INSTANCE}", headers=headers, timeout=10)
                return {"ok": True, "waiting": True}

            # Unknown state — delete and recreate
            requests.delete(f"{EVOLUTION_URL}/instance/logout/{INSTANCE}", headers=headers, timeout=10)
            time.sleep(1)
            requests.delete(f"{EVOLUTION_URL}/instance/delete/{INSTANCE}", headers=headers, timeout=10)
            for _ in range(6):
                time.sleep(2)
                chk = requests.get(f"{EVOLUTION_URL}/instance/connectionState/{INSTANCE}", headers=headers, timeout=5)
                if chk.status_code == 404:
                    break

        # Create fresh instance (clear any stale QR from previous attempt)
        _qr_store["base64"] = None
        create_r = requests.post(
            f"{EVOLUTION_URL}/instance/create",
            json={"instanceName": INSTANCE, "qrcode": True, "integration": "WHATSAPP-BAILEYS"},
            headers=headers, timeout=15,
        )
        if create_r.status_code not in (200, 201):
            return {"ok": False, "error": f"Falha ao criar instância: {create_r.text}"}

        # Register webhook (includes QRCODE_UPDATED so QR arrives via webhook)
        _register_webhook_for_instance(INSTANCE, API_KEY, EVOLUTION_URL)

        # Trigger connect so Baileys starts QR generation
        requests.get(f"{EVOLUTION_URL}/instance/connect/{INSTANCE}", headers=headers, timeout=10)

        return {"ok": True, "waiting": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/whatsapp/qr")
def get_whatsapp_qr():
    """Frontend polls this after clicking Conectar to get the QR once it arrives via webhook."""
    b64 = _qr_store.get("base64")
    if b64:
        return {"base64": b64}
    return {"base64": None}


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
# WEBHOOK – Landing Page Captura → CRM
# =========================
@app.post("/webhook/captura-lead")
async def webhook_captura_lead(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "JSON inválido"}, status_code=400)

    phone_raw = str(body.get("telefone", "")).replace(r"\D", "")
    import re as _re
    digits = _re.sub(r"\D", "", phone_raw)
    phone = "55" + digits if not digits.startswith("55") else digits

    name = str(body.get("nome_completo", "")).strip() or None

    existing = db.query(models.Lead).filter(models.Lead.phone == phone).first()
    if existing:
        return {"ok": True, "id": existing.id, "status": "already_exists"}

    lead = models.Lead(name=name, phone=phone, status="pendente", etapa="Novo Lead", board_id=1)
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return {"ok": True, "id": lead.id, "status": "created"}


# =========================
# IMPORTAR TODOS OS LEADS DO FACEBOOK
# =========================
@app.post("/facebook/import-all-leads")
def facebook_import_all_leads(db: Session = Depends(get_db)):
    """Busca todos os leads de todos os formulários da Página e importa para o CRM."""
    from config import load_settings
    s = load_settings()
    page_token = s.get("fb_page_access_token", "").strip()
    if not page_token:
        raise HTTPException(status_code=400, detail="Page Access Token não configurado")

    criados = 0
    ignorados = 0
    erros = []

    try:
        # Tenta user token (/me/accounts) primeiro; se não tiver páginas, usa como page token direto (/me)
        accounts = requests.get("https://graph.facebook.com/v25.0/me/accounts",
                                params={"access_token": page_token, "fields": "id,name,access_token"}, timeout=15).json()
        if accounts.get("data"):
            pages = [{"id": p["id"], "access_token": p["access_token"]} for p in accounts["data"]]
        else:
            # Page Access Token: /me retorna a própria página
            me_resp = requests.get("https://graph.facebook.com/v25.0/me",
                                   params={"access_token": page_token, "fields": "id,name"}, timeout=15).json()
            if "error" in me_resp:
                raise HTTPException(status_code=400, detail=f"Token inválido: {me_resp['error'].get('message','')}")
            pages = [{"id": me_resp.get("id", "me"), "access_token": page_token}]

        for page in pages:
            page_id = page.get("id")
            page_tok = page.get("access_token", page_token)

            # 2. Busca todos os formulários da página
            forms_resp = requests.get(f"https://graph.facebook.com/v25.0/{page_id}/leadgen_forms",
                                      params={"access_token": page_tok, "fields": "id,name"}, timeout=15).json()
            forms = forms_resp.get("data", [])

            for form in forms:
                form_id = form["id"]
                form_name = form.get("name", form_id)
                next_url = f"https://graph.facebook.com/v25.0/{form_id}/leads"
                params = {"access_token": page_tok, "fields": "field_data,created_time", "limit": 100}

                while next_url:
                    resp = requests.get(next_url, params=params, timeout=15).json()
                    params = {}  # params only for first request
                    for lead_data in resp.get("data", []):
                        try:
                            fields = {f["name"]: f["values"][0] for f in lead_data.get("field_data", []) if f.get("values")}
                            phone_raw = fields.get("phone_number") or fields.get("phone") or fields.get("telefone") or fields.get("celular") or ""
                            name = fields.get("full_name") or fields.get("name") or fields.get("nome") or ""
                            phone = re.sub(r'\D', '', phone_raw)
                            if len(phone) in [10, 11]:
                                phone = f"55{phone}"
                            if not phone:
                                ignorados += 1
                                continue
                            if db.query(models.Lead).filter(models.Lead.phone == phone).first():
                                ignorados += 1
                                continue
                            lead = models.Lead(
                                name=name or phone,
                                phone=phone,
                                status="pendente",
                                etapa="Novo Lead",
                                board_id=1,
                                campaign_name=f"Facebook · {form_name}",
                                origem_lead="Facebook",
                            )
                            db.add(lead)
                            db.commit()
                            criados += 1
                        except Exception as e:
                            erros.append(str(e))
                    next_url = resp.get("paging", {}).get("next")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao buscar leads do Facebook: {e}")

    return {"ok": True, "criados": criados, "ignorados": ignorados, "erros": erros[:10]}


# =========================
# WEBHOOK – Facebook Lead Ads
# =========================
@app.get("/webhook/facebook")
async def facebook_webhook_verify(request: Request):
    """Verificação do webhook pelo Facebook (challenge handshake)."""
    from config import load_settings
    s = load_settings()
    verify_token = s.get("fb_verify_token", "")
    mode      = request.query_params.get("hub.mode")
    token     = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge", "")
    if mode == "subscribe" and verify_token and token == verify_token:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(challenge)
    return JSONResponse({"ok": False, "error": "Token inválido"}, status_code=403)


@app.post("/webhook/facebook")
async def facebook_webhook_lead(request: Request, db: Session = Depends(get_db)):
    """Recebe eventos de leads do Facebook Lead Ads e cria o lead no CRM."""
    import hashlib, hmac as _hmac, json as _json, re as _re
    from config import load_settings

    body_bytes = await request.body()
    s          = load_settings()
    app_secret = s.get("fb_app_secret", "")
    page_token = s.get("fb_page_access_token", "")

    # Verificação de assinatura (opcional — ativa quando fb_app_secret está preenchido)
    if app_secret:
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        expected   = "sha256=" + _hmac.new(app_secret.encode(), body_bytes, hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(sig_header, expected):
            return JSONResponse({"ok": False, "error": "Assinatura inválida"}, status_code=403)

    try:
        payload = _json.loads(body_bytes)
    except Exception:
        return JSONResponse({"ok": False, "error": "JSON inválido"}, status_code=400)

    if payload.get("object") != "page":
        return {"ok": True, "skipped": "not a page event"}

    results = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "leadgen":
                continue
            value      = change.get("value", {})
            leadgen_id = value.get("leadgen_id")
            if not leadgen_id:
                continue

            # Busca dados completos do lead na Graph API
            if not page_token:
                print(f"[fb] leadgen_id={leadgen_id} ignorado: page_access_token não configurado")
                continue
            try:
                r = requests.get(
                    f"https://graph.facebook.com/v21.0/{leadgen_id}",
                    params={"access_token": page_token},
                    timeout=10,
                )
                lead_data = r.json()
            except Exception as exc:
                print(f"[fb] erro ao buscar leadgen {leadgen_id}: {exc}")
                continue

            if "error" in lead_data:
                print(f"[fb] Graph API erro para {leadgen_id}: {lead_data['error']}")
                continue

            # Transforma field_data em dict {nome_campo: valor}
            fields = {
                f["name"]: f["values"][0]
                for f in lead_data.get("field_data", [])
                if f.get("values")
            }

            # Nome
            name = (
                fields.get("full_name")
                or (f"{fields.get('first_name','')} {fields.get('last_name','')}".strip() or None)
                or fields.get("nome_completo") or fields.get("nome") or fields.get("name")
            )

            # Telefone — aceita variações comuns de campo
            phone_raw = (
                fields.get("phone_number") or fields.get("phone")
                or fields.get("telefone")  or fields.get("celular")
                or fields.get("whatsapp")  or ""
            )
            digits = _re.sub(r"\D", "", phone_raw)
            if not digits:
                print(f"[fb] leadgen {leadgen_id} sem telefone — campos: {list(fields.keys())}")
                results.append({"leadgen_id": leadgen_id, "status": "sem_telefone", "fields": list(fields.keys())})
                continue
            phone = "55" + digits if not digits.startswith("55") else digits

            # Upsert: se o telefone já existe, não duplica
            existing = db.query(models.Lead).filter(models.Lead.phone == phone).first()
            if existing:
                print(f"[fb] lead {phone} já existe id={existing.id}")
                results.append({"id": existing.id, "status": "already_exists", "phone": phone})
                continue

            form_id  = value.get("form_id", "")
            campaign = f"Facebook Leads · {form_id}" if form_id else "Facebook Leads"
            lead = models.Lead(
                name=name, phone=phone, status="pendente",
                etapa="Novo Lead", board_id=1,
                campaign_name=campaign,
            )
            db.add(lead)
            db.commit()
            db.refresh(lead)
            print(f"[fb] ✓ novo lead: {name} | {phone} | id={lead.id}")
            results.append({"id": lead.id, "status": "created", "phone": phone, "name": name})

    return {"ok": True, "processed": len(results), "leads": results}


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

    # Capture QR code — Evolution API v2 sends either QRCODE_UPDATED or connection.update with qr
    if event in ("qrcode.updated", "QRCODE_UPDATED"):
        qr_data = payload.get("data", {})
        b64 = (
            (qr_data.get("qrcode") or {}).get("base64")
            or qr_data.get("base64")
        )
        if b64:
            _qr_store["base64"] = b64
            print("[webhook] QR code recebido via QRCODE_UPDATED")
        return {"ok": True}

    if event in ("connection.update", "CONNECTION_UPDATE"):
        data = payload.get("data", {})
        print(f"[webhook] connection.update: {data}")
        # Some versions embed QR inside connection.update
        b64 = (
            (data.get("qrcode") or {}).get("base64")
            or data.get("qr")
            or data.get("base64")
        )
        if b64:
            _qr_store["base64"] = b64
            print("[webhook] QR code recebido via connection.update")
        return {"ok": True}

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

        # Detecta áudio (voz ou arquivo de áudio)
        audio_data = None
        audio_mime = None
        is_audio = any(k in msg_obj for k in ("audioMessage", "pttMessage"))
        if is_audio and not text.strip():
            print(f"[webhook] audio detected from {phone}, fetching base64...")
            try:
                from config import load_settings as _ls
                _s = _ls()
                r = requests.post(
                    f"{_s['evolution_url']}/chat/getBase64FromMediaMessage/{_s['instance']}",
                    json={"message": data, "convertToMp4": False},
                    headers={"apikey": _s["api_key"], "Content-Type": "application/json"},
                    timeout=20,
                )
                if r.ok:
                    resp = r.json()
                    raw_b64 = resp.get("base64", "")
                    if "," in raw_b64:
                        raw_b64 = raw_b64.split(",", 1)[1]
                    audio_data = raw_b64
                    audio_mime = (resp.get("mimetype") or "audio/ogg").split(";")[0].strip()
                    print(f"[webhook] audio fetched mime={audio_mime} size={len(audio_data)}")
                else:
                    print(f"[webhook] audio fetch failed: {r.status_code} {r.text[:200]}")
            except Exception as e:
                print(f"[webhook] audio fetch error: {e}")

        print(f"[webhook] phone={phone} text={repr(text)} audio={bool(audio_data)}")

        if not text.strip() and not audio_data:
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
                msg_preview = text.strip() if text.strip() else ("🎤 [áudio]" if audio_data else "")
                alert = (
                    f"\U0001f6a8 *ALERTA COMERCIAL*\n\n"
                    f"Time, atenção!\n\n"
                    f"O lead *{lead_name}* acabou de responder no WhatsApp.\n\n"
                    f"\U0001f4de Telefone: {phone_display}\n"
                    f"\U0001f552 Data/Hora: {data_hora}\n"
                    f"\U0001f4ac Mensagem: \"{msg_preview}\"\n\n"
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

            handled = handle_incoming(phone=phone, raw_text=text, db=db, settings=settings,
                                       audio_data=audio_data, audio_mime=audio_mime)
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