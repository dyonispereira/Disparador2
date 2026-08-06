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
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS form_data TEXT",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS bot_ativo BOOLEAN NOT NULL DEFAULT TRUE",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS modo VARCHAR NOT NULL DEFAULT 'auto'",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS instancia VARCHAR",
            "ALTER TABLE kanban_boards ADD COLUMN IF NOT EXISTS ticket_medio_json TEXT",
            """CREATE TABLE IF NOT EXISTS whatsapp_instances (
                id SERIAL PRIMARY KEY,
                nome VARCHAR NOT NULL,
                instance_name VARCHAR UNIQUE NOT NULL,
                ativo BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            """CREATE TABLE IF NOT EXISTS whatsapp_messages (
                id SERIAL PRIMARY KEY,
                phone VARCHAR NOT NULL,
                content TEXT NOT NULL,
                direction VARCHAR NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            "CREATE INDEX IF NOT EXISTS idx_wa_messages_phone ON whatsapp_messages(phone)",
            "ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS tipo VARCHAR NOT NULL DEFAULT 'text'",
            "ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS url_arquivo TEXT",
            """CREATE TABLE IF NOT EXISTS disparo_leads (
                id SERIAL PRIMARY KEY,
                name VARCHAR,
                phone VARCHAR NOT NULL,
                status VARCHAR NOT NULL DEFAULT 'pendente',
                campaign_name VARCHAR,
                sent_message VARCHAR,
                sent_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            )""",
            """CREATE TABLE IF NOT EXISTS lead_obs (
                id SERIAL PRIMARY KEY,
                lead_id INTEGER NOT NULL REFERENCES leads(id),
                texto TEXT NOT NULL,
                autor TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS perfil VARCHAR NOT NULL DEFAULT 'vendedor'",
            "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS primeiro_login BOOLEAN NOT NULL DEFAULT false",
            "UPDATE usuarios SET perfil = 'admin', primeiro_login = false WHERE email = 'admin@gestorpec.com.br'",
        ]:
            try:
                conn.execute(text("SAVEPOINT m"))
                conn.execute(text(sql))
                conn.execute(text("RELEASE SAVEPOINT m"))
            except Exception:
                conn.execute(text("ROLLBACK TO SAVEPOINT m"))
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


def _facebook_sync_incremental(db, s: dict) -> int:
    """Busca leads novos do Facebook desde o último sync. Retorna quantidade criada."""
    import json as _json, time as _t
    from config import save_settings

    page_token = s.get("fb_page_access_token", "").strip()
    if not page_token:
        return 0

    app_secret = s.get("fb_app_secret", "").strip()
    last_sync  = int(s.get("fb_last_sync", 0) or 0)
    if not last_sync:
        last_sync = int(_t.time()) - 7 * 86400  # primeira vez: últimos 7 dias

    criados = 0
    erros   = []

    try:
        accounts = requests.get(
            "https://graph.facebook.com/v25.0/me/accounts",
            params=_fb_params(page_token, app_secret, {"fields": "id,name,access_token"}),
            timeout=15,
        ).json()
        if accounts.get("data"):
            pages = [{"id": p["id"], "access_token": p["access_token"], "name": p.get("name", "")} for p in accounts["data"]]
        else:
            me = requests.get(
                "https://graph.facebook.com/v25.0/me",
                params=_fb_params(page_token, app_secret, {"fields": "id,name"}),
                timeout=15,
            ).json()
            if "error" in me:
                raise Exception(f"Token inválido: {me['error'].get('message', '')}")
            pages = [{"id": me.get("id", "me"), "access_token": page_token, "name": me.get("name", "")}]

        filtering = _json.dumps([{"field": "time_created", "operator": "GREATER_THAN", "value": last_sync}])

        for page in pages:
            page_tok = page["access_token"]
            forms_resp = requests.get(
                f"https://graph.facebook.com/v25.0/{page['id']}/leadgen_forms",
                params=_fb_params(page_tok, app_secret, {"fields": "id,name"}),
                timeout=15,
            ).json()
            if "error" in forms_resp:
                erros.append(f"Página {page.get('name', page['id'])}: {forms_resp['error'].get('message', '')}")
                continue
            forms = forms_resp.get("data", [])

            for form in forms:
                form_id   = form["id"]
                form_name = form.get("name", form_id)
                next_url  = f"https://graph.facebook.com/v25.0/{form_id}/leads"
                p = _fb_params(page_tok, app_secret, {
                    "fields": "field_data,created_time",
                    "filtering": filtering,
                    "limit": 100,
                })

                while next_url:
                    resp = requests.get(next_url, params=p, timeout=15).json()
                    p = {}
                    if "error" in resp:
                        erros.append(f"form {form_name}: {resp['error'].get('message', '')}")
                        break
                    for lead_data in resp.get("data", []):
                        try:
                            flds = {f["name"]: f["values"][0] for f in lead_data.get("field_data", []) if f.get("values")}
                            phone_raw = (
                                flds.get("phone_number") or flds.get("phone")
                                or flds.get("telefone")  or flds.get("celular")
                                or flds.get("whatsapp")  or ""
                            )
                            name = (
                                flds.get("full_name") or flds.get("nome_completo")
                                or flds.get("nome")   or flds.get("name") or ""
                            )
                            phone = _normalizar_telefone(phone_raw)
                            if not phone:
                                continue

                            fb_time_str = lead_data.get("created_time", "")
                            fb_created  = None
                            if fb_time_str:
                                try:
                                    fb_created = datetime.strptime(fb_time_str[:19], "%Y-%m-%dT%H:%M:%S")
                                except Exception:
                                    pass

                            existing = db.query(models.Lead).filter(models.Lead.phone == phone).first()
                            if existing:
                                changed = False
                                if not existing.board_id:
                                    existing.board_id = 1
                                    existing.etapa = "Novo Lead"
                                    changed = True
                                if name and not existing.name:
                                    existing.name = name
                                    changed = True
                                if changed:
                                    db.commit()
                                continue

                            db.add(models.Lead(
                                name=name or phone, phone=phone, status="pendente",
                                etapa="Novo Lead", board_id=1,
                                campaign_name=f"Facebook · {form_name}",
                                origem_lead="Facebook",
                                created_at=fb_created,
                                form_data=_json.dumps(flds, ensure_ascii=False),
                            ))
                            db.commit()
                            criados += 1
                        except Exception:
                            pass
                    next_url = resp.get("paging", {}).get("next")

    except Exception as exc:
        erros.append(str(exc))
        print(f"[fb-sync] erro: {exc}")

    # Salva timestamp e resultado do sync
    now = int(_t.time())
    s_fresh = s.copy()
    s_fresh["fb_last_sync"]         = now
    s_fresh["fb_last_sync_at"]      = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    s_fresh["fb_last_sync_criados"] = criados
    s_fresh["fb_last_sync_erro"]    = erros[0] if erros else ""
    save_settings(s_fresh)

    if criados > 0:
        print(f"[fb-sync] ✓ {criados} novo(s) lead(s) importado(s)")
    if erros:
        print(f"[fb-sync] erros: {erros}")
    return criados


async def _facebook_sync_loop():
    """Polling automático do Facebook a cada 5 minutos."""
    from config import load_settings
    while True:
        await asyncio.sleep(300)
        s = load_settings()
        if not s.get("fb_page_access_token", "").strip():
            continue
        db = SessionLocal()
        try:
            _facebook_sync_incremental(db, s)
        except Exception as exc:
            print(f"[fb-sync-loop] erro inesperado: {exc}")
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
    asyncio.create_task(_facebook_sync_loop())


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


def _normalizar_telefone(raw: str) -> str | None:
    """
    Normaliza telefone para formato brasileiro com DDI 55.
    Rejeita notação científica do Excel (ex: 5,57E+12) pois perde precisão.
    Retorna None se o número for inválido.
    """
    raw = (raw or "").strip()
    # Rejeita notação científica — número original foi perdido pelo Excel
    raw_upper = raw.upper().replace(",", ".").replace(" ", "")
    if "E+" in raw_upper or "E-" in raw_upper:
        return None
    phone = re.sub(r'\D', '', raw)
    if not phone:
        return None
    if len(phone) in [10, 11]:
        phone = "55" + phone
    # Número deve ter 12 ou 13 dígitos (55 + DDD + número)
    if len(phone) < 12 or len(phone) > 13:
        return None
    return phone

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
def get_dashboard_stats(board_id: int = 1, mes: int = None, ano: int = None,
                        data_inicio: str = None, data_fim: str = None,
                        db: Session = Depends(get_db)):
    import json as _json
    from sqlalchemy import func, or_
    from datetime import date, timedelta

    today_date = datetime.utcnow().date()
    week_ago   = datetime.utcnow() - timedelta(days=7)
    month_ago  = datetime.utcnow() - timedelta(days=30)

    # ── Filtro por período ──────────────────────────────────────
    period_start = period_end = None
    if data_inicio and data_fim:
        try:
            period_start = datetime.strptime(data_inicio, "%Y-%m-%d")
            period_end   = datetime.strptime(data_fim, "%Y-%m-%d") + timedelta(days=1)
        except ValueError:
            pass
    elif mes and ano:
        next_m = mes % 12 + 1
        next_y = ano + (1 if mes == 12 else 0)
        period_start = datetime(ano, mes, 1)
        period_end   = datetime(next_y, next_m, 1)

    def _leads_q():
        q = db.query(func.count(models.Lead.id))
        if period_start:
            q = q.filter(models.Lead.created_at >= period_start, models.Lead.created_at < period_end)
        return q

    def _meetings_q():
        q = db.query(func.count(models.ScheduledMeeting.id))
        if period_start:
            q = q.filter(models.ScheduledMeeting.created_at >= period_start,
                         models.ScheduledMeeting.created_at < period_end)
        return q

    # ── Totais de leads ─────────────────────────────────────────
    total        = _leads_q().scalar() or 0
    leads_hoje   = _leads_q().filter(func.date(models.Lead.created_at) == today_date).scalar() or 0
    leads_semana = _leads_q().filter(models.Lead.created_at >= week_ago).scalar() or 0
    leads_mes    = _leads_q().filter(models.Lead.created_at >= month_ago).scalar() or 0

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
        # Mesma regra do /leads/kanban: etapa ausente ou que não existe mais
        # no quadro atual cai no primeiro card, em vez de ser descartada.
        key = row.etapa if row.etapa in por_etapa else (etapas[0] if etapas else None)
        if key in por_etapa:
            por_etapa[key] += row.total

    # ── Indicador de Ticket Médio por etapa ─────────────────────
    ticket_map = _json.loads(board.ticket_medio_json) if (board and board.ticket_medio_json) else {}
    total_board_leads = sum(por_etapa.values())
    indicador_ticket = []
    for e in etapas:
        qtd = por_etapa.get(e, 0)
        ticket_medio = ticket_map.get(e, 0) or 0
        indicador_ticket.append({
            "etapa": e,
            "qtd_leads": qtd,
            "ticket_medio": ticket_medio,
            "valor_total": round(qtd * ticket_medio, 2),
            "percentual": round(qtd / total_board_leads * 100, 1) if total_board_leads else 0,
        })

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
    total_reunioes  = _meetings_q().scalar() or 0
    confirmadas     = _meetings_q().filter(models.ScheduledMeeting.status == "confirmado").scalar() or 0
    canceladas      = _meetings_q().filter(models.ScheduledMeeting.status == "cancelado").scalar() or 0
    taxa_comp = round(confirmadas / total_reunioes * 100, 1) if total_reunioes else 0

    # ── Funil de conversão ──────────────────────────────────────
    def _count(*etapas_list):
        q = db.query(func.count(models.Lead.id)).filter(models.Lead.etapa.in_(etapas_list))
        if period_start:
            q = q.filter(models.Lead.created_at >= period_start, models.Lead.created_at < period_end)
        return q.scalar() or 0

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
        "indicador_ticket": indicador_ticket,
    }


@app.put("/kanban/boards/{board_id}/ticket-medio")
def set_ticket_medio(board_id: int, body: dict, db: Session = Depends(get_db)):
    """Atualiza o Ticket Médio de uma etapa. Body: {"etapa": "...", "valor": 123.45}"""
    import json as _json
    board = db.query(models.KanbanBoard).filter(models.KanbanBoard.id == board_id).first()
    if not board:
        raise HTTPException(status_code=404, detail="Quadro não encontrado")

    etapa = (body.get("etapa") or "").strip()
    if not etapa:
        raise HTTPException(status_code=400, detail="Campo 'etapa' é obrigatório")
    try:
        valor = float(body.get("valor", 0) or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Campo 'valor' inválido")

    ticket_map = _json.loads(board.ticket_medio_json) if board.ticket_medio_json else {}
    ticket_map[etapa] = valor
    board.ticket_medio_json = _json.dumps(ticket_map, ensure_ascii=False)
    db.commit()
    return {"ok": True, "etapa": etapa, "valor": valor}


@app.put("/kanban/boards/{board_id}/ticket-medio/aplicar-todos")
def aplicar_ticket_medio_todos(board_id: int, body: dict, db: Session = Depends(get_db)):
    """Aplica o mesmo Ticket Médio a todas as etapas do quadro. Body: {"valor": 123.45}"""
    import json as _json
    board = db.query(models.KanbanBoard).filter(models.KanbanBoard.id == board_id).first()
    if not board:
        raise HTTPException(status_code=404, detail="Quadro não encontrado")
    try:
        valor = float(body.get("valor", 0) or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Campo 'valor' inválido")

    etapas = _json.loads(board.etapas)
    ticket_map = {e: valor for e in etapas}
    board.ticket_medio_json = _json.dumps(ticket_map, ensure_ascii=False)
    db.commit()
    return {"ok": True, "valor": valor, "etapas_atualizadas": len(etapas)}


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
    if source == "csv":
        return db.query(models.DisparoLead).order_by(models.DisparoLead.id.desc()).all()
    q = db.query(models.Lead)
    return q.all()


@app.get("/leads/export-csv")
def export_leads_csv(db: Session = Depends(get_db)):
    from fastapi.responses import StreamingResponse
    from datetime import timezone, timedelta
    import io as _io
    BR_TZ = timezone(timedelta(hours=-3))
    leads = db.query(models.DisparoLead).all()
    out = _io.StringIO()
    w = csv.writer(out, delimiter=";", quoting=csv.QUOTE_ALL)
    w.writerow(["Nome", "Telefone", "Status", "Mensagem Enviada", "Data Disparo (Brasília)", "Campanha"])
    for l in leads:
        if l.status == "enviado":   disp = "Sim"
        elif l.status == "falhou":  disp = "Falhou"
        else:                       disp = "Não"
        if l.sent_at:
            sent_br = l.sent_at.replace(tzinfo=timezone.utc).astimezone(BR_TZ)
            sent_str = sent_br.strftime("%d/%m/%Y %H:%M")
        else:
            sent_str = ""
        # Prefixo = para forçar Excel a tratar como texto
        phone_txt = f'="{l.phone}"' if l.phone else ""
        w.writerow([l.name or "", phone_txt, disp, l.sent_message or "", sent_str, l.campaign_name or ""])
    content = "﻿" + out.getvalue()
    return StreamingResponse(
        _io.BytesIO(content.encode("utf-8")),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="leads_disparo.csv"'}
    )

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
def delete_all_leads(source: str = None, db: Session = Depends(get_db)):
    try:
        if source == "csv":
            # Limpa apenas a tabela de disparo — Funil CRM intacto
            db.query(models.DisparoLead).delete()
        else:
            db.query(models.Message).delete()
            db.query(models.LeadObs).delete()
            db.query(models.Lead).delete()
        db.commit()
        return {"ok": True}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Erro ao limpar: {str(e)}")

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
    from datetime import timezone as _tz, timedelta as _td
    BR_TZ = _tz(_td(hours=-3))  # Brasília UTC-3
    for lead in leads:
        etapa = lead.etapa if lead.etapa in result else etapas[0]
        if lead.created_at:
            created_br = lead.created_at.replace(tzinfo=_tz.utc).astimezone(BR_TZ)
            created_str = created_br.strftime("%d/%m/%Y")
        else:
            created_str = ""
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
            "created_at": created_str,
            "created_at_raw": lead.created_at.isoformat() if lead.created_at else None,
            "origem_lead": lead.origem_lead or "",
            "campaign_name": lead.campaign_name or "",
            "custo_campanha": lead.custo_campanha,
            "form_data": lead.form_data or "",
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


@app.patch("/leads/bulk-etapa")
def bulk_update_etapa(body: dict, db: Session = Depends(get_db)):
    """Move vários leads de uma vez para outra etapa. Body: {"lead_ids": [1,2,3], "etapa": "..."}"""
    lead_ids = body.get("lead_ids") or []
    etapa = (body.get("etapa") or "").strip()
    if not lead_ids:
        raise HTTPException(status_code=400, detail="lead_ids é obrigatório")
    if not etapa:
        raise HTTPException(status_code=400, detail="etapa é obrigatória")
    atualizados = (
        db.query(models.Lead)
        .filter(models.Lead.id.in_(lead_ids))
        .update({"etapa": etapa}, synchronize_session=False)
    )
    db.commit()
    return {"ok": True, "atualizados": atualizados}


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
# CHAT WHATSAPP
# =========================

@app.get("/leads/{lead_id}/chat")
def get_lead_chat(lead_id: int, db: Session = Depends(get_db)):
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    msgs = (
        db.query(models.WhatsAppMessage)
        .filter(models.WhatsAppMessage.phone == lead.phone)
        .order_by(models.WhatsAppMessage.created_at.asc())
        .all()
    )
    return {
        "bot_ativo": getattr(lead, "bot_ativo", True),
        "modo": getattr(lead, "modo", "auto"),
        "messages": [
            {
                "id": m.id,
                "content": m.content,
                "direction": m.direction,
                "created_at": m.created_at.isoformat(),
                "tipo": getattr(m, "tipo", "text") or "text",
                "url_arquivo": getattr(m, "url_arquivo", None),
            }
            for m in msgs
        ],
    }


@app.post("/leads/{lead_id}/send")
def send_lead_message(lead_id: int, body: dict, db: Session = Depends(get_db)):
    from config import load_settings
    from services.scheduling_flow import _send

    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Mensagem vazia")

    settings = load_settings()
    _send(lead.phone, text, settings)

    msg = models.WhatsAppMessage(phone=lead.phone, content=text, direction="out", tipo="text")
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return {
        "id": msg.id,
        "content": msg.content,
        "direction": msg.direction,
        "created_at": msg.created_at.isoformat(),
        "tipo": "text",
        "url_arquivo": None,
    }


@app.patch("/leads/{lead_id}/bot")
def toggle_lead_bot(lead_id: int, body: dict, db: Session = Depends(get_db)):
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    if "bot_ativo" in body:
        lead.bot_ativo = bool(body["bot_ativo"])
    if "modo" in body:
        lead.modo = str(body["modo"])
    db.commit()
    return {"bot_ativo": lead.bot_ativo, "modo": lead.modo}


@app.post("/leads/{lead_id}/send-audio")
def send_lead_audio(lead_id: int, body: dict, db: Session = Depends(get_db)):
    from config import load_settings

    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    audio_b64 = body.get("audio", "")
    mime = body.get("mime", "audio/ogg")
    if not audio_b64:
        raise HTTPException(status_code=400, detail="Áudio vazio")

    settings = load_settings()
    try:
        requests.post(
            f"{settings['evolution_url']}/message/sendAudio/{settings['instance']}",
            json={"number": lead.phone, "audio": audio_b64, "encoding": True},
            headers={"apikey": settings["api_key"], "Content-Type": "application/json"},
            timeout=30,
        )
    except Exception as e:
        print(f"[send-audio] error: {e}")

    data_uri = f"data:{mime};base64,{audio_b64}"
    msg = models.WhatsAppMessage(
        phone=lead.phone, content="[áudio]", direction="out",
        tipo="audio", url_arquivo=data_uri,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return {
        "id": msg.id, "content": msg.content, "direction": msg.direction,
        "created_at": msg.created_at.isoformat(), "tipo": msg.tipo, "url_arquivo": msg.url_arquivo,
    }


@app.post("/leads/{lead_id}/send-media")
def send_lead_media(lead_id: int, body: dict, db: Session = Depends(get_db)):
    from config import load_settings

    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    media_b64 = body.get("media", "")
    mime = body.get("mime", "image/jpeg")
    filename = body.get("filename", "arquivo")
    if not media_b64:
        raise HTTPException(status_code=400, detail="Arquivo vazio")

    tipo = "imagem" if mime.startswith("image/") else "arquivo"
    ev_mediatype = "image" if mime.startswith("image/") else ("video" if mime.startswith("video/") else "document")

    settings = load_settings()
    try:
        requests.post(
            f"{settings['evolution_url']}/message/sendMedia/{settings['instance']}",
            json={
                "number": lead.phone,
                "mediatype": ev_mediatype,
                "mimetype": mime,
                "media": media_b64,
                "caption": "",
                "fileName": filename,
            },
            headers={"apikey": settings["api_key"], "Content-Type": "application/json"},
            timeout=30,
        )
    except Exception as e:
        print(f"[send-media] error: {e}")

    data_uri = f"data:{mime};base64,{media_b64}"
    msg = models.WhatsAppMessage(
        phone=lead.phone, content=filename, direction="out",
        tipo=tipo, url_arquivo=data_uri,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return {
        "id": msg.id, "content": msg.content, "direction": msg.direction,
        "created_at": msg.created_at.isoformat(), "tipo": msg.tipo, "url_arquivo": msg.url_arquivo,
    }


# =========================
# BOT GLOBAL
# =========================

@app.get("/bot-global")
def get_bot_global():
    from config import load_settings
    s = load_settings()
    return {"bot_global": s.get("bot_global", True)}


@app.patch("/bot-global")
def set_bot_global(body: dict):
    from config import load_settings, save_settings
    s = load_settings()
    s["bot_global"] = bool(body.get("bot_global", True))
    save_settings(s)
    return {"bot_global": s["bot_global"]}


# =========================
# CONEXÕES WHATSAPP (múltiplas instâncias)
# =========================

@app.get("/whatsapp-instances")
def list_whatsapp_instances(db: Session = Depends(get_db)):
    insts = db.query(models.WhatsAppInstance).order_by(models.WhatsAppInstance.id).all()
    return [{"id": i.id, "nome": i.nome, "instance_name": i.instance_name, "ativo": i.ativo} for i in insts]


@app.post("/whatsapp-instances")
def create_whatsapp_instance(body: dict, db: Session = Depends(get_db)):
    nome = (body.get("nome") or "").strip()
    instance_name = (body.get("instance_name") or "").strip()
    if not nome or not instance_name:
        raise HTTPException(status_code=400, detail="Nome e instance_name são obrigatórios")
    inst = models.WhatsAppInstance(nome=nome, instance_name=instance_name)
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return {"id": inst.id, "nome": inst.nome, "instance_name": inst.instance_name, "ativo": inst.ativo}


@app.delete("/whatsapp-instances/{inst_id}")
def delete_whatsapp_instance(inst_id: int, db: Session = Depends(get_db)):
    inst = db.query(models.WhatsAppInstance).filter(models.WhatsAppInstance.id == inst_id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Instância não encontrada")
    db.delete(inst)
    db.commit()
    return {"ok": True}


@app.get("/whatsapp-instances/{inst_id}/status")
def get_instance_status(inst_id: int, db: Session = Depends(get_db)):
    from config import load_settings
    inst = db.query(models.WhatsAppInstance).filter(models.WhatsAppInstance.id == inst_id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Instância não encontrada")
    settings = load_settings()
    try:
        r = requests.get(
            f"{settings['evolution_url']}/instance/connectionState/{inst.instance_name}",
            headers={"apikey": settings["api_key"]},
            timeout=5,
        )
        data = r.json() if r.ok else {}
        state = (data.get("instance") or {}).get("state") or data.get("state") or "unknown"
        online = state in ("open", "connected", "CONNECTED")
        return {"instance_name": inst.instance_name, "state": state, "online": online}
    except Exception:
        return {"instance_name": inst.instance_name, "state": "offline", "online": False}


@app.post("/whatsapp-instances/{inst_id}/connect")
def connect_whatsapp_instance(inst_id: int, db: Session = Depends(get_db)):
    from config import load_settings
    inst = db.query(models.WhatsAppInstance).filter(models.WhatsAppInstance.id == inst_id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Instância não encontrada")

    settings = load_settings()
    headers = {"apikey": settings["api_key"]}
    ev_url = settings["evolution_url"]
    inst_name = inst.instance_name

    def _try_get_qr(timeout=5):
        """Tenta buscar QR com timeout curto — retorna None se demorar."""
        try:
            r = requests.get(f"{ev_url}/instance/connect/{inst_name}", headers=headers, timeout=timeout)
            if r.ok:
                data = r.json()
                return _extract_b64(data) or _extract_b64(data.get("qrcode") or {})
        except Exception:
            pass
        return None

    def _do_connect_bg():
        """Executa o fluxo completo em background para não bloquear a resposta."""
        try:
            state_r = requests.get(f"{ev_url}/instance/connectionState/{inst_name}", headers=headers, timeout=5)
            if state_r.status_code != 200:
                # Instância não existe — cria
                requests.post(
                    f"{ev_url}/instance/create",
                    json={"instanceName": inst_name, "qrcode": True, "integration": "WHATSAPP-BAILEYS"},
                    headers=headers, timeout=15,
                )
                _register_webhook_for_instance(inst_name, settings["api_key"], ev_url)
                time.sleep(2)
            else:
                state = (state_r.json().get("instance") or {}).get("state") or state_r.json().get("state", "")
                if state == "open":
                    _qr_store[inst_name] = "CONNECTED"
                    return
                if state not in ("connecting", "close", ""):
                    # Estado inválido — recria
                    requests.delete(f"{ev_url}/instance/logout/{inst_name}", headers=headers, timeout=5)
                    requests.delete(f"{ev_url}/instance/delete/{inst_name}", headers=headers, timeout=5)
                    time.sleep(2)
                    requests.post(
                        f"{ev_url}/instance/create",
                        json={"instanceName": inst_name, "qrcode": True, "integration": "WHATSAPP-BAILEYS"},
                        headers=headers, timeout=15,
                    )
                    _register_webhook_for_instance(inst_name, settings["api_key"], ev_url)
                    time.sleep(2)
                else:
                    # close/connecting — garante webhook registrado para receber QRCODE_UPDATED
                    _register_webhook_for_instance(inst_name, settings["api_key"], ev_url)

            # Tenta buscar QR até 90s (Evolution API leva ~2s de delay interno no primeiro request)
            for _ in range(15):
                qr = _try_get_qr(timeout=8)
                if qr:
                    _qr_store[inst_name] = qr
                    return
                time.sleep(4)

            # Esgotou tentativas sem QR — sinaliza erro para o frontend
            if not _qr_store.get(inst_name):
                _qr_store[inst_name] = "ERROR"
                print(f"[connect-bg] {inst_name}: QR não chegou após 90s")
        except Exception as exc:
            _qr_store[inst_name] = "ERROR"
            print(f"[connect-bg] {inst_name}: {exc}")

    import threading
    try:
        # Se já tem QR em memória e instância está conectando, retorna imediatamente
        _cached = _qr_store.get(inst_name)
        if _cached and _cached not in ("CONNECTED", "ERROR"):
            return {"base64": _cached}
        # Limpa estado de erro anterior para nova tentativa
        if _cached == "ERROR":
            _qr_store[inst_name] = None

        # Verifica se já está conectado
        state_r = requests.get(f"{ev_url}/instance/connectionState/{inst_name}", headers=headers, timeout=4)
        if state_r.status_code == 200:
            state = (state_r.json().get("instance") or {}).get("state") or state_r.json().get("state", "")
            if state == "open":
                return {"connected": True, "state": "open"}

        # Tenta QR rápido (3s) — se vier, ótimo; senão inicia background e retorna waiting
        _qr_store[inst_name] = None
        qr = _try_get_qr(timeout=3)
        if qr:
            _qr_store[inst_name] = qr
            return {"base64": qr}

        # Inicia processo em background e retorna imediatamente para o frontend
        threading.Thread(target=_do_connect_bg, daemon=True).start()
        return {"ok": True, "waiting": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/whatsapp-instances/{inst_id}/qr")
def get_instance_qr(inst_id: int, db: Session = Depends(get_db)):
    inst = db.query(models.WhatsAppInstance).filter(models.WhatsAppInstance.id == inst_id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Instância não encontrada")
    val = _qr_store.get(inst.instance_name)
    if val == "CONNECTED":
        return {"connected": True, "base64": None}
    if val == "ERROR":
        return {"error": True, "base64": None}
    return {"base64": val}


@app.post("/leads/{lead_id}/schedule-meet")
def schedule_meet_manual(lead_id: int, body: dict, db: Session = Depends(get_db)):
    """Cria reunião no Google Calendar + Meet a partir do chat do CRM."""
    from services.google_meet import create_meet_event, is_authenticated
    from services.scheduling_flow import _send
    from config import load_settings
    import json as _json

    if not is_authenticated():
        raise HTTPException(status_code=400, detail="Google Calendar não autenticado. Configure em Configurações → Google.")

    data_str = (body.get("data") or "").strip()   # YYYY-MM-DD
    hora_str = (body.get("hora") or "").strip()   # HH:MM
    if not data_str or not hora_str:
        raise HTTPException(status_code=400, detail="Data e hora são obrigatórios")

    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    settings = load_settings()
    company   = settings.get("company_name", "Empresa")
    cal_id    = settings.get("company_calendar_email", "") or "primary"

    # Convidados: padrão das configs + participantes ativos
    emails = list(settings.get("default_meet_emails", []) or [])
    for p in db.query(models.Participante).filter(models.Participante.ativo == True).all():
        if p.email and p.email not in emails:
            emails.append(p.email)
    emails = [e for e in emails if e and "@" in e]

    try:
        result = create_meet_event(
            summary=f"Reunião {company} × {lead.name or lead.phone}",
            date_str=data_str,
            time_str=hora_str,
            attendee_emails=emails or None,
            calendar_id=cal_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erro ao criar evento no Google: {exc}")

    meet_link = result.get("meet_link") or result.get("html_link", "")
    event_id  = result.get("event_id", "")

    # Salva reunião no banco
    meeting = models.ScheduledMeeting(
        lead_name=lead.name or lead.phone,
        lead_phone=lead.phone,
        meeting_date=data_str,
        meeting_time=hora_str,
        meet_link=meet_link,
        calendar_event_id=event_id,
        status="confirmado",
        confirmed_at=datetime.utcnow(),
    )
    db.add(meeting)

    # Pausa o bot e marca como agendado
    lead.bot_ativo = False
    lead.status_interesse = "agendado"
    db.add(models.LeadObs(
        lead_id=lead.id,
        texto=f"Reunião agendada para {data_str} às {hora_str}. Meet: {meet_link}",
        autor="Sistema",
    ))
    db.commit()
    db.refresh(meeting)

    # Mensagem para o lead
    _send(lead.phone, f"Reunião confirmada! Segue o link: {meet_link}", settings)

    # Alerta no grupo comercial
    vendor_group = settings.get("vendor_group_jid", "")
    if vendor_group:
        try:
            from datetime import datetime as _dt
            date_label = _dt.strptime(data_str, "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            date_label = data_str
        _send(vendor_group, "\n".join([
            "🚨 *Nova reunião agendada*",
            "",
            f"👤 Nome: {lead.name or lead.phone}",
            f"📅 Data: {date_label}",
            f"⏰ Hora: {hora_str}",
            f"🎥 Link: {meet_link}",
        ]), settings)

    return {"ok": True, "meet_link": meet_link, "event_id": event_id, "meeting_id": meeting.id}


@app.patch("/leads/{lead_id}/transfer")
def transfer_lead_instance(lead_id: int, body: dict, db: Session = Depends(get_db)):
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    nova_instancia = (body.get("instancia") or "").strip()
    autor = (body.get("autor") or "Sistema").strip()

    inst = db.query(models.WhatsAppInstance).filter(
        models.WhatsAppInstance.instance_name == nova_instancia
    ).first()
    nome_instancia = inst.nome if inst else nova_instancia

    antiga = lead.instancia or "principal"
    lead.instancia = nova_instancia
    lead.bot_ativo = False
    lead.modo = "manual"

    db.add(models.LeadObs(
        lead_id=lead.id,
        texto=f"Atendimento transferido para {nome_instancia} (antes: {antiga})",
        autor=autor,
    ))
    db.commit()
    return {"ok": True, "instancia": nova_instancia, "nome": nome_instancia}


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
        ignorados = 0
        ja_existentes = 0

        for row in reader:
            try:
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

                phone = _normalizar_telefone(phone_raw)
                if not phone:
                    # Só conta como ignorado se havia dígitos (telefone malformado)
                    # Linhas sem dígitos são cabeçalhos ou linhas em branco
                    if re.search(r'\d', phone_raw):
                        ignorados += 1
                    continue

                # Tabela disparo_leads — independente do Funil CRM
                exists = db.query(models.DisparoLead).filter(models.DisparoLead.phone == phone).first()
                if exists:
                    exists.name = name or exists.name
                    exists.status = status_planilha
                    ja_existentes += 1
                    continue

                db.add(models.DisparoLead(name=name, phone=phone, status=status_planilha))
                created += 1

            except Exception as e:
                print(f"Erro ao processar a linha {row}: {e}")
                continue

        db.commit()

        return {
            "ok": True,
            "created": created,
            "ignorados": ignorados,
            "ja_existentes": ja_existentes,
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

                exists = db.query(models.DisparoLead).filter(models.DisparoLead.phone == phone).first()
                if exists:
                    exists.name = name or exists.name
                    exists.status = status_planilha
                    continue

                db.add(models.DisparoLead(name=name, phone=phone, status=status_planilha))
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
                 b64_media, mimetype, filename,
                 batch_size: int = 10, batch_pause_min: int = 300, batch_pause_max: int = 600,
                 instance_names: list = None):
    """Runs in a background thread — sends messages with anti-ban delays."""
    from config import load_settings
    settings = load_settings()
    evo_url  = settings.get("evolution_url", EVOLUTION_URL)
    api_key  = settings.get("api_key", API_KEY)
    headers  = {"apikey": api_key, "Content-Type": "application/json"}

    # Instâncias para rateio: usa as fornecidas ou cai na instância padrão
    if not instance_names:
        instance_names = [settings.get("instance", INSTANCE)]
    instance_cycle = instance_names[:]  # cópia para rotação
    _inst_index = 0

    is_image = bool(filename and filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')))
    media_type = "image" if is_image else "document"

    sent_in_batch = 0

    db = SessionLocal()
    try:
        for i, (lead_id, lead_name, lead_phone) in enumerate(leads_snapshot):
            lead = db.query(models.DisparoLead).filter(models.DisparoLead.id == lead_id).first()
            if not lead or lead.status == "enviado":
                continue

            # Pausa longa a cada batch_size mensagens enviadas
            if sent_in_batch > 0 and sent_in_batch % batch_size == 0:
                pause = random.uniform(batch_pause_min, batch_pause_max)
                print(f"[disparo] pausa de lote após {sent_in_batch} msgs: {pause:.0f}s (~{pause/60:.1f}min)")
                time.sleep(pause)

            # Round-robin entre instâncias
            instance = instance_cycle[_inst_index % len(instance_cycle)]
            _inst_index += 1

            text = re.sub(r'{\s*name\s*}', lead_name or "", random.choice(templates_text), flags=re.IGNORECASE)
            final_code = 500

            # Simula digitação antes de enviar (3-7 segundos)
            typing_delay = random.randint(3000, 7000)
            options = {"delay": typing_delay, "presence": "composing"}

            print(f"[disparo] instancia={instance} lead={lead_phone}")
            try:
                if b64_media:
                    order = random.choice(['media_first', 'text_first'])
                    if order == 'media_first':
                        r = requests.post(
                            f"{evo_url}/message/sendMedia/{instance}",
                            json={"number": lead_phone, "mediatype": media_type, "mimetype": mimetype,
                                  "caption": text, "media": b64_media, "options": options},
                            headers=headers, timeout=30)
                        final_code = r.status_code
                        print(f"[disparo] media_first {lead_phone}: {r.status_code}")
                    else:
                        r1 = requests.post(f"{evo_url}/message/sendText/{instance}",
                                           json={"number": lead_phone, "text": text, "options": options},
                                           headers=headers, timeout=15)
                        time.sleep(random.uniform(4, 10))
                        r2 = requests.post(
                            f"{evo_url}/message/sendMedia/{instance}",
                            json={"number": lead_phone, "mediatype": media_type, "mimetype": mimetype,
                                  "media": b64_media, "options": {"delay": 2000, "presence": "composing"}},
                            headers=headers, timeout=30)
                        final_code = r2.status_code if r1.ok else r1.status_code
                        print(f"[disparo] text_first {lead_phone}: txt={r1.status_code} media={r2.status_code}")
                else:
                    r = requests.post(f"{evo_url}/message/sendText/{instance}",
                                      json={"number": lead_phone, "text": text, "options": options},
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

            if lead.status == "enviado":
                sent_in_batch += 1

            if i < len(leads_snapshot) - 1:
                delay = random.uniform(45, 120)
                print(f"[disparo] aguardando {delay:.0f}s...")
                time.sleep(delay)

        # Atualiza planilha local se existir (path relativo ao backend)
        try:
            _backend_dir = _os.path.dirname(_os.path.abspath(__file__))
            output_dir = _os.path.join(_backend_dir, "..", "dados")
            if _os.path.exists(output_dir):
                file_path = _os.path.join(output_dir, "leads.csv")
                all_leads = db.query(models.DisparoLead).all()
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
    lead_ids: str = Form(None),
    batch_size: int = Form(10),
    batch_pause_min: int = Form(300),
    batch_pause_max: int = Form(600),
    instance_ids: str = Form(None),  # IDs separados por vírgula; vazio = todas ativas
    db: Session = Depends(get_db)
):
    templates = db.query(models.MessageTemplate).all()
    if not templates:
        raise HTTPException(status_code=400, detail="Nenhuma mensagem cadastrada para o disparo.")

    # Disparo usa tabela própria — independente do Funil CRM
    if lead_ids:
        ids = [int(i) for i in lead_ids.split(',') if i.strip().isdigit()]
        q = db.query(models.DisparoLead).filter(models.DisparoLead.id.in_(ids))
    else:
        q = db.query(models.DisparoLead).filter(models.DisparoLead.status == "pendente")
    leads = q.all()
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

    # Resolve instâncias para rateio
    if instance_ids:
        ids_list = [int(x) for x in instance_ids.split(',') if x.strip().isdigit()]
        insts = db.query(models.WhatsAppInstance).filter(
            models.WhatsAppInstance.id.in_(ids_list),
            models.WhatsAppInstance.ativo == True
        ).all()
    else:
        insts = db.query(models.WhatsAppInstance).filter(models.WhatsAppInstance.ativo == True).all()

    instance_names = [i.instance_name for i in insts] if insts else None

    leads_snapshot = [(l.id, l.name or "", l.phone) for l in leads]
    templates_text = [t.text for t in templates]

    background_tasks.add_task(
        _run_disparo,
        campaign_name, leads_snapshot, templates_text, b64_media, mimetype, filename,
        batch_size, batch_pause_min, batch_pause_max, instance_names
    )

    num_insts = len(instance_names) if instance_names else 1
    tempo_estimado_min = (len(leads) * 45 + (len(leads) // batch_size) * batch_pause_min) // num_insts
    tempo_estimado_max = (len(leads) * 120 + (len(leads) // batch_size) * batch_pause_max) // num_insts
    inst_label = f"{num_insts} número(s)" if num_insts > 1 else "1 número"
    return {
        "ok": True,
        "iniciado": True,
        "total": len(leads),
        "instancias": num_insts,
        "message": (
            f"Disparo iniciado para {len(leads)} lead(s) via {inst_label}. "
            f"Pausa a cada {batch_size} msgs ({batch_pause_min//60}-{batch_pause_max//60} min). "
            f"Tempo estimado: {tempo_estimado_min//60}-{tempo_estimado_max//60} min."
        )
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
    """Extrai somente imagens base64 válidas — ignora strings raw de QR code."""
    candidates = [
        data.get("base64"),
        (data.get("qrcode") or {}).get("base64"),
    ]
    for c in candidates:
        if c and isinstance(c, str) and (c.startswith("data:") or len(c) > 100):
            return c
    return None


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
                "webhookBase64": True,
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
                # Re-registra webhook e dispara connect para gerar QR
                _qr_store["base64"] = None
                _register_webhook_for_instance(INSTANCE, API_KEY, EVOLUTION_URL)
                r_conn = requests.get(f"{EVOLUTION_URL}/instance/connect/{INSTANCE}", headers=headers, timeout=15)
                if r_conn.ok:
                    qr = _extract_b64(r_conn.json())
                    if qr:
                        _qr_store["base64"] = qr
                        return {"base64": qr}
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
# WEBHOOK – Email pós-captura
# =========================
@app.post("/webhook/send-email")
async def webhook_send_email(request: Request):
    import smtplib, ssl, sys, io
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from config import load_settings
    if hasattr(sys.stdout, 'reconfigure'):
        try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception: pass

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "JSON inválido"}, status_code=400)

    nome  = str(body.get("nome_completo", body.get("nome", ""))).split()[0]
    email = str(body.get("email", "")).strip()

    if not email:
        return JSONResponse({"ok": False, "error": "email ausente"}, status_code=400)

    cfg = load_settings()
    smtp_email = cfg.get("smtp_email", "")
    smtp_pass  = cfg.get("smtp_password", "")

    if not smtp_email or not smtp_pass:
        return JSONResponse({"ok": False, "error": "SMTP não configurado"}, status_code=500)

    assunto = "Seu acesso foi liberado"
    corpo_html = f"""
<div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;color:#1a1a1a">
  <p>Olá <strong>{nome}</strong>,</p>
  <p>Conforme prometido, aqui está o acesso ao material:</p>
  <p style="margin:24px 0">
    <a href="https://drive.google.com/drive/folders/1_veNPyBGrrI1VtiUNElDtBnm1BY8hq-4"
       style="background:#18b745;color:#fff;padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:bold">
      👉 Acessar material agora
    </a>
  </p>
  <p>Esse conteúdo vai te ajudar a enxergar melhor os números da sua operação.</p>
  <p>Depois me conta o que achou.</p>
  <p>Abraço,<br><strong>Carlos</strong><br>GestorPec</p>
</div>
"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = assunto
        msg["From"]    = f"{cfg.get('smtp_nome_remetente', 'GestorPec')} <{smtp_email}>"
        msg["To"]      = email
        msg.attach(MIMEText(corpo_html, "html", "utf-8"))
    except Exception as err:
        return JSONResponse({"ok": False, "stage": "mime", "error": repr(err)}, status_code=500)

    try:
        ctx = ssl.create_default_context()
    except Exception as err:
        return JSONResponse({"ok": False, "stage": "ssl", "error": repr(err)}, status_code=500)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
            s.login(smtp_email, smtp_pass)
            s.send_message(msg)
        return {"ok": True}
    except Exception as err:
        return JSONResponse({"ok": False, "stage": "smtp", "error": repr(err)}, status_code=500)


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

    import json as _json
    _KNOWN = {"telefone", "nome_completo"}
    extra = {k: v for k, v in body.items() if k not in _KNOWN and v not in (None, "")}
    lead = models.Lead(
        name=name, phone=phone, status="pendente", etapa="Novo Lead", board_id=1,
        origem_lead="Landing Page",
        form_data=_json.dumps(extra, ensure_ascii=False) if extra else None,
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return {"ok": True, "id": lead.id, "status": "created"}


# =========================
# IMPORTAR TODOS OS LEADS DO FACEBOOK
# =========================

def _fb_params(token: str, app_secret: str, extra: dict = None) -> dict:
    """Monta params com appsecret_proof quando app_secret está configurado."""
    import hmac, hashlib
    p = {"access_token": token}
    if app_secret:
        proof = hmac.new(app_secret.encode(), token.encode(), hashlib.sha256).hexdigest()
        p["appsecret_proof"] = proof
    if extra:
        p.update(extra)
    return p


@app.get("/facebook/sync-status")
def facebook_sync_status():
    from config import load_settings
    s = load_settings()
    return {
        "ativo": bool(s.get("fb_page_access_token", "").strip()),
        "last_sync_at": s.get("fb_last_sync_at"),
        "last_sync_criados": s.get("fb_last_sync_criados", 0),
        "last_sync_erro": s.get("fb_last_sync_erro", ""),
        "intervalo_min": 5,
    }


@app.post("/facebook/sync-now")
def facebook_sync_now(db: Session = Depends(get_db)):
    from config import load_settings
    s = load_settings()
    if not s.get("fb_page_access_token", "").strip():
        raise HTTPException(status_code=400, detail="Page Access Token não configurado")
    criados = _facebook_sync_incremental(db, s)
    return {"ok": True, "criados": criados}


@app.post("/facebook/extend-token")
def facebook_extend_token():
    """Converte token curto em token de longa duração (60 dias)."""
    from config import load_settings, save_settings
    s = load_settings()
    token      = s.get("fb_page_access_token", "").strip()
    app_id     = s.get("fb_app_id", "").strip()
    app_secret = s.get("fb_app_secret", "").strip()
    if not all([token, app_id, app_secret]):
        raise HTTPException(status_code=400, detail="Configure Page Access Token, App ID e App Secret antes de estender.")
    # Tenta via GET (padrão) e via POST como fallback
    resp = requests.get("https://graph.facebook.com/oauth/access_token", params={
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": token,
    }, timeout=15).json()
    if "error" in resp:
        err = resp["error"]
        detail = f"{err.get('message','')} (code={err.get('code','')}, type={err.get('type','')})"
        raise HTTPException(status_code=400, detail=detail)
    new_token = resp.get("access_token", "")
    expires_in = resp.get("expires_in", 0)
    s["fb_page_access_token"] = new_token
    save_settings(s)
    days = expires_in // 86400 if expires_in else 60
    return {"ok": True, "expires_in_days": days, "message": f"Token estendido com sucesso! Válido por ~{days} dias."}


@app.post("/facebook/import-all-leads")
def facebook_import_all_leads(data_inicio: str = None, data_fim: str = None, db: Session = Depends(get_db)):
    from config import load_settings
    import json as _json
    from datetime import timedelta as _td
    s = load_settings()
    page_token = s.get("fb_page_access_token", "").strip()
    app_secret = s.get("fb_app_secret", "").strip()
    if not page_token:
        raise HTTPException(status_code=400, detail="Page Access Token não configurado")

    # Filtro de período (opcional) — restringe pelo campo time_created da Graph API
    fb_filtering = None
    if data_inicio or data_fim:
        conditions = []
        if data_inicio:
            try:
                ts_ini = int(datetime.strptime(data_inicio, "%Y-%m-%d").timestamp())
                conditions.append({"field": "time_created", "operator": "GREATER_THAN", "value": ts_ini})
            except ValueError:
                raise HTTPException(status_code=400, detail="data_inicio inválida (use YYYY-MM-DD)")
        if data_fim:
            try:
                ts_fim = int((datetime.strptime(data_fim, "%Y-%m-%d") + _td(days=1)).timestamp())
                conditions.append({"field": "time_created", "operator": "LESS_THAN", "value": ts_fim})
            except ValueError:
                raise HTTPException(status_code=400, detail="data_fim inválida (use YYYY-MM-DD)")
        fb_filtering = _json.dumps(conditions)

    criados = 0
    ignorados = 0
    erros = []
    debug = []
    if fb_filtering:
        debug.append(f"Filtro de período aplicado: {data_inicio or '(sem início)'} até {data_fim or '(sem fim)'}")

    try:
        accounts = requests.get("https://graph.facebook.com/v25.0/me/accounts",
                                params=_fb_params(page_token, app_secret, {"fields": "id,name,access_token"}),
                                timeout=15).json()
        if accounts.get("data"):
            pages = [{"id": p["id"], "access_token": p["access_token"], "name": p.get("name","")} for p in accounts["data"]]
            debug.append(f"Páginas encontradas: {[p['name'] for p in pages]}")
        else:
            me_resp = requests.get("https://graph.facebook.com/v25.0/me",
                                   params=_fb_params(page_token, app_secret, {"fields": "id,name"}),
                                   timeout=15).json()
            if "error" in me_resp:
                raise HTTPException(status_code=400, detail=f"Token inválido: {me_resp['error'].get('message','')}")
            pages = [{"id": me_resp.get("id", "me"), "access_token": page_token, "name": me_resp.get("name","")}]
            debug.append(f"Usando token como page token direto: {me_resp.get('name','')} ({me_resp.get('id','')})")

        for page in pages:
            page_id  = page.get("id")
            page_tok = page.get("access_token", page_token)

            forms_resp = requests.get(
                f"https://graph.facebook.com/v25.0/{page_id}/leadgen_forms",
                params=_fb_params(page_tok, app_secret, {"fields": "id,name,leads_count"}),
                timeout=15).json()
            forms = forms_resp.get("data", [])
            if not forms:
                if "error" in forms_resp:
                    erros.append(f"Página {page.get('name',page_id)}: {forms_resp['error'].get('message','')}")
                else:
                    debug.append(f"Página '{page.get('name',page_id)}': nenhum formulário encontrado")
                continue
            debug.append(f"Página '{page.get('name',page_id)}': {len(forms)} formulário(s): {[f.get('name','?') for f in forms]}")

            for form in forms:
                form_id   = form["id"]
                form_name = form.get("name", form_id)
                leads_count = form.get("leads_count", "?")
                debug.append(f"  Formulário '{form_name}': {leads_count} lead(s) no Facebook")
                next_url  = f"https://graph.facebook.com/v25.0/{form_id}/leads"
                p_extra = {"fields": "field_data,created_time", "limit": 100}
                if fb_filtering:
                    p_extra["filtering"] = fb_filtering
                p = _fb_params(page_tok, app_secret, p_extra)

                while next_url:
                    resp = requests.get(next_url, params=p, timeout=15).json()
                    p = {}
                    if "error" in resp:
                        erros.append(f"Formulário {form_name}: {resp['error'].get('message','')}")
                        break
                    for lead_data in resp.get("data", []):
                        try:
                            flds = {f["name"]: f["values"][0] for f in lead_data.get("field_data", []) if f.get("values")}
                            phone_raw = flds.get("phone_number") or flds.get("phone") or flds.get("telefone") or flds.get("celular") or ""
                            name      = flds.get("full_name") or flds.get("name") or flds.get("nome") or ""
                            if not phone_raw:
                                debug.append(f"    Lead sem telefone. Campos: {list(flds.keys())}")
                                ignorados += 1
                                continue
                            phone = _normalizar_telefone(phone_raw)
                            if not phone:
                                debug.append(f"    Telefone inválido: {phone_raw}")
                                ignorados += 1
                                continue
                            # Data do formulário (antes do check de existência)
                            fb_time_str = lead_data.get("created_time", "")
                            fb_created = None
                            if fb_time_str:
                                try:
                                    fb_created = datetime.strptime(fb_time_str[:19], "%Y-%m-%dT%H:%M:%S")
                                except Exception:
                                    pass
                            existing = db.query(models.Lead).filter(models.Lead.phone == phone).first()
                            if existing:
                                import json as _json
                                changed = False
                                if fb_created and existing.origem_lead == "Facebook":
                                    existing.created_at = fb_created
                                    changed = True
                                if not existing.form_data and flds:
                                    existing.form_data = _json.dumps(flds, ensure_ascii=False)
                                    changed = True
                                if name and (not existing.name or existing.name == existing.phone or existing.name == phone_raw):
                                    existing.name = name
                                    changed = True
                                # Garante que o lead está no Funil CRM
                                if not existing.board_id:
                                    existing.board_id = 1
                                    existing.etapa = "Novo Lead"
                                    existing.origem_lead = existing.origem_lead or "Facebook"
                                    changed = True
                                if changed:
                                    db.commit()
                                ignorados += 1
                                continue
                            import json as _json
                            db.add(models.Lead(
                                name=name or phone, phone=phone, status="pendente",
                                etapa="Novo Lead", board_id=1,
                                campaign_name=f"Facebook · {form_name}",
                                origem_lead="Facebook",
                                created_at=fb_created,
                                form_data=_json.dumps(flds, ensure_ascii=False),
                            ))
                            db.commit()
                            criados += 1
                        except Exception as e:
                            erros.append(str(e))
                    next_url = resp.get("paging", {}).get("next")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao buscar leads do Facebook: {e}")

    return {"ok": True, "criados": criados, "ignorados": ignorados, "erros": erros[:10], "debug": debug}


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
                    f"https://graph.facebook.com/v25.0/{leadgen_id}",
                    params=_fb_params(page_token, app_secret, {"fields": "field_data,created_time,form_id"}),
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
            phone = _normalizar_telefone(phone_raw)
            if not phone:
                print(f"[fb] leadgen {leadgen_id} telefone inválido '{phone_raw}' — campos: {list(fields.keys())}")
                results.append({"leadgen_id": leadgen_id, "status": "telefone_invalido"})
                continue

            # Data do formulário
            fb_time_str = lead_data.get("created_time", "")
            fb_created = None
            if fb_time_str:
                try:
                    fb_created = datetime.strptime(fb_time_str[:19], "%Y-%m-%dT%H:%M:%S")
                except Exception:
                    pass

            # Upsert: se o telefone já existe, atualiza form_data/nome mas não duplica
            existing = db.query(models.Lead).filter(models.Lead.phone == phone).first()
            if existing:
                import json as _json
                changed = False
                if not existing.form_data and fields:
                    existing.form_data = _json.dumps(fields, ensure_ascii=False)
                    changed = True
                # Atualiza nome se estava em branco ou igual ao telefone
                if name and (not existing.name or existing.name == existing.phone or existing.name == phone_raw):
                    existing.name = name
                    changed = True
                if fb_created and not existing.created_at:
                    existing.created_at = fb_created
                    changed = True
                # Garante que o lead está no Funil CRM
                if not existing.board_id:
                    existing.board_id = 1
                    existing.etapa = "Novo Lead"
                    existing.origem_lead = existing.origem_lead or "Facebook"
                    changed = True
                if changed:
                    db.commit()
                print(f"[fb] lead {phone} já existe id={existing.id} updated={changed}")
                results.append({"id": existing.id, "status": "updated" if changed else "already_exists", "phone": phone})
                continue

            form_id  = value.get("form_id", "") or lead_data.get("form_id", "")
            campaign = f"Facebook · {form_id}" if form_id else "Facebook Leads"
            import json as _json
            lead = models.Lead(
                name=name, phone=phone, status="pendente",
                etapa="Novo Lead", board_id=1,
                campaign_name=campaign,
                origem_lead="Facebook",
                created_at=fb_created,
                form_data=_json.dumps(fields, ensure_ascii=False),
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

    # Instância que efetivamente recebeu a mensagem — pode divergir da
    # instância padrão em settings.json se houver múltiplos números.
    instance_name = payload.get("instance", "")

    event = payload.get("event", "")

    # Capture QR code — Evolution API v2 sends either QRCODE_UPDATED or connection.update with qr
    if event in ("qrcode.updated", "QRCODE_UPDATED"):
        inst_name = payload.get("instance", "")
        qr_data = payload.get("data", {})
        b64 = (
            (qr_data.get("qrcode") or {}).get("base64")
            or qr_data.get("base64")
        )
        if b64:
            _qr_store["base64"] = b64         # legado — instância principal
            if inst_name:
                _qr_store[inst_name] = b64    # por instância
            print(f"[webhook] QR code recebido via QRCODE_UPDATED instance={inst_name}")
        return {"ok": True}

    if event in ("connection.update", "CONNECTION_UPDATE"):
        inst_name = payload.get("instance", "")
        data = payload.get("data", {})
        print(f"[webhook] connection.update: {data}")
        b64 = (
            (data.get("qrcode") or {}).get("base64")
            or data.get("qr")
            or data.get("base64")
        )
        if b64:
            _qr_store["base64"] = b64
            if inst_name:
                _qr_store[inst_name] = b64
            print(f"[webhook] QR code recebido via connection.update instance={inst_name}")
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
                    f"{_s['evolution_url']}/chat/getBase64FromMediaMessage/{instance_name or _s['instance']}",
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

        # Garante que todo contato novo entra no Funil CRM, mesmo sem texto
        db = SessionLocal()
        try:
            def _variants(p):
                vs = [p]
                if p.startswith("55") and len(p) == 12:
                    vs.append(p[:4] + "9" + p[4:])
                elif p.startswith("55") and len(p) == 13 and p[4] == "9":
                    vs.append(p[:4] + p[5:])
                return vs
            lead = db.query(models.Lead).filter(models.Lead.phone.in_(_variants(phone))).first()
            if not lead:
                lead = models.Lead(
                    name=phone, phone=phone, status="pendente",
                    etapa="Novo Lead", board_id=1, origem_lead="WhatsApp",
                )
                db.add(lead)
                db.commit()
                db.refresh(lead)
                print(f"[webhook] novo lead criado via WhatsApp: {phone}")
            elif not lead.board_id:
                lead.board_id = 1
                lead.etapa = "Novo Lead"
                db.commit()
            db.close()
        except Exception as _e:
            print(f"[webhook] erro ao criar lead {phone}: {_e}")
            db.close()

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
            alert_instance = instance_name or settings.get("instance", "")
            if not vendor_jid:
                print("[alerta-comercial] pulado: vendor_group_jid não configurado")
            else:
                phone_display = phone[2:] if phone.startswith("55") else phone
                from datetime import timezone as _tz, timedelta as _td
                CBA_TZ = _tz(_td(hours=-4))  # Cuiabá/MT UTC-4
                data_hora = datetime.now(_tz.utc).astimezone(CBA_TZ).strftime("%d/%m/%Y %H:%M")
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
                print(f"[alerta-comercial] enviando pra {vendor_jid} via instância '{alert_instance}'")
                try:
                    headers = {"apikey": settings["api_key"], "Content-Type": "application/json"}
                    alert_resp = requests.post(
                        f"{settings['evolution_url']}/message/sendText/{alert_instance}",
                        json={"number": vendor_jid, "text": alert},
                        headers=headers,
                        timeout=10,
                    )
                    print(f"[alerta-comercial] resposta: {alert_resp.status_code} {alert_resp.text[:300]}")
                except Exception as _alert_exc:
                    print(f"[alerta-comercial] erro ao enviar: {_alert_exc}")

            # Salva a mensagem recebida no histórico de chat
            msg_content = text.strip() if text.strip() else ("[áudio]" if audio_data else "")
            if msg_content:
                db.add(models.WhatsAppMessage(phone=phone, content=msg_content, direction="in"))
                db.commit()

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
            "webhookBase64": True,
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