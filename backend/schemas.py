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