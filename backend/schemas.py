from pydantic import BaseModel
from datetime import datetime


# =========================
# LEAD
# =========================
class LeadCreate(BaseModel):
    name: str | None = None
    phone: str


class LeadResponse(BaseModel):
    id: int
    name: str | None
    phone: str
    created_at: datetime

    class Config:
        from_attributes = True


# =========================
# MESSAGE
# =========================
class MessageCreate(BaseModel):
    text: str
    lead_id: int


class MessageResponse(BaseModel):
    id: int
    text: str
    status: str
    created_at: datetime
    lead_id: int

    class Config:
        from_attributes = True


# =========================
# MESSAGE TEMPLATE
# =========================
class MessageTemplateBase(BaseModel):
    text: str

class MessageTemplateCreate(MessageTemplateBase):
    pass

class MessageTemplateUpdate(MessageTemplateBase):
    pass

class MessageTemplateResponse(MessageTemplateBase):
    id: int

    class Config:
        from_attributes = True


# =========================
# KANBAN BOARD
# =========================
class KanbanBoardResponse(BaseModel):
    id: int
    nome: str
    etapas: str   # JSON string
    created_at: datetime

    class Config:
        from_attributes = True


# =========================
# AUTH
# =========================
class LoginRequest(BaseModel):
    email: str
    senha: str


class TokenResponse(BaseModel):
    token: str
    nome: str
    email: str


class UsuarioResponse(BaseModel):
    id: int
    nome: str
    email: str
    ativo: bool
    created_at: datetime

    class Config:
        from_attributes = True


class UsuarioCreate(BaseModel):
    nome: str
    email: str
    senha: str


class SenhaUpdate(BaseModel):
    senha_atual: str
    nova_senha: str


# =========================
# LEAD OBS
# =========================
class LeadObsCreate(BaseModel):
    texto: str
    autor: str | None = None


class LeadObsResponse(BaseModel):
    id: int
    lead_id: int
    texto: str
    autor: str | None
    created_at: datetime

    class Config:
        from_attributes = True