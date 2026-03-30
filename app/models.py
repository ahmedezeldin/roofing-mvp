from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship

from .db import Base


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, index=True, nullable=False)
    source = Column(String, default="missed_call", nullable=False)

    conversation_state = Column(String, default="awaiting_job_type", nullable=False)
    status = Column(String, default="new", nullable=False)  # qualification status

    job_type = Column(String, nullable=True)
    postal_code = Column(String, nullable=True)
    customer_name = Column(String, nullable=True)
    urgency = Column(String, nullable=True)
    priority = Column(String, nullable=True)
    insurance_claim = Column(String, nullable=True)  # yes / no

    crm_status = Column(String, default="new", nullable=False)  # new/contacted/booked/closed/lost
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    messages = relationship("Message", back_populates="lead", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    direction = Column(String, nullable=False)  # inbound / outbound
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    lead = relationship("Lead", back_populates="messages")


class BusinessSetting(Base):
    __tablename__ = "business_settings"

    id = Column(Integer, primary_key=True, index=True)
    business_name = Column(String, default="ABC Roofing", nullable=False)
    first_message_template = Column(
        Text,
        default="Hey, thanks for calling {business_name}. Sorry we missed you — are you looking for a repair, replacement, or inspection?",
        nullable=False
    )