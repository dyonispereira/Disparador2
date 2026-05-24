from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

from db import Base


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    name = Column(String)
    phone = Column(String, unique=True)
    status = Column(String, default="pendente")
    created_at = Column(DateTime, default=datetime.utcnow)
    campaign_name = Column(String, nullable=True)
    sent_message = Column(String, nullable=True)
    sent_at = Column(DateTime, nullable=True)

    messages = relationship("Message", back_populates="lead")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    text = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    lead_id = Column(Integer, ForeignKey("leads.id"))

    lead = relationship("Lead", back_populates="messages")


class MessageTemplate(Base):
    __tablename__ = "message_templates"

    id = Column(Integer, primary_key=True)
    text = Column(String, nullable=False)


class ConversationState(Base):
    """Tracks where each lead is in the scheduling conversation flow."""
    __tablename__ = "conversation_states"

    id = Column(Integer, primary_key=True)
    phone = Column(String, unique=True, nullable=False)
    lead_name = Column(String, nullable=True)
    # idle | awaiting_date | awaiting_time | awaiting_confirmation | confirmed | cancelled
    state = Column(String, default="idle")
    selected_date = Column(String, nullable=True)       # YYYY-MM-DD
    selected_time = Column(String, nullable=True)       # HH:MM
    offered_dates = Column(String, nullable=True)       # JSON list of YYYY-MM-DD strings
    offered_times = Column(String, nullable=True)       # JSON list of HH:MM strings
    meet_link = Column(String, nullable=True)
    calendar_event_id = Column(String, nullable=True)
    messages_json = Column(String, nullable=True)   # JSON array [{role, content}]
    followup_sent = Column(Boolean, default=False, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)


class ScheduledMeeting(Base):
    """Confirmed or pending meetings created through the WhatsApp scheduling flow."""
    __tablename__ = "scheduled_meetings"

    id = Column(Integer, primary_key=True)
    lead_name = Column(String, nullable=False)
    lead_phone = Column(String, nullable=False)
    meeting_date = Column(String, nullable=False)       # YYYY-MM-DD
    meeting_time = Column(String, nullable=False)       # HH:MM
    meet_link = Column(String, nullable=True)
    calendar_event_id = Column(String, nullable=True)
    # pendente | confirmado | cancelado
    status = Column(String, default="pendente")
    created_at = Column(DateTime, default=datetime.utcnow)
    confirmed_at = Column(DateTime, nullable=True)
    reminder_24h_sent = Column(Boolean, default=False, nullable=False)
    reminder_1h_sent  = Column(Boolean, default=False, nullable=False)


class Participante(Base):
    """Membros do time comercial convidados automaticamente para as reuniões."""
    __tablename__ = "participantes"

    id = Column(Integer, primary_key=True)
    nome = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    ativo = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)