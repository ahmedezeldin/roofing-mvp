from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship

from .db import Base


class AppUser(Base):
    __tablename__ = "app_users"

    id = Column(Integer, primary_key=True, index=True)
    full_name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    company_name = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    owned_workspaces = relationship(
        "Workspace",
        back_populates="owner",
        cascade="all, delete-orphan",
    )
    sessions = relationship(
        "UserSession",
        back_populates="user",
        cascade="all, delete-orphan",
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("app_users.id"), nullable=False, index=True)
    token = Column(String, unique=True, index=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("AppUser", back_populates="sessions")


class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True, index=True)
    company_name = Column(String, nullable=False)
    plan = Column(String, nullable=False, default="pilot")
    business_phone = Column(String, nullable=True)
    primary_service_area = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    owner_user_id = Column(Integer, ForeignKey("app_users.id"), nullable=False)

    # ---- billing / status ----
    status = Column(String, nullable=False, default="pending")
    stripe_customer_id = Column(String, nullable=True)
    stripe_subscription_id = Column(String, nullable=True)

    # ---- phone setup ----
    phone_mode = Column(String, nullable=True)                 # existing / new
    pending_twilio_number = Column(String, nullable=True)      # selected but not purchased yet
    active_twilio_number = Column(String, nullable=True)       # purchased after checkout
    coverage_mode = Column(String, nullable=True)              # always / after_hours
    workday_start = Column(String, nullable=True)              # "09:00"
    workday_end = Column(String, nullable=True)                # "17:00"
    business_days = Column(String, nullable=True)              # mon_fri / mon_sat / all_week
    notification_email = Column(String, nullable=True)
    team_mobile = Column(String, nullable=True)

    owner = relationship("AppUser", back_populates="owned_workspaces")
    settings = relationship("BusinessSettings", back_populates="workspace", uselist=False)
    leads = relationship("Lead", back_populates="workspace", cascade="all, delete-orphan")
    messages = relationship("Message", back_populates="workspace", cascade="all, delete-orphan")

class BusinessSettings(Base):
    __tablename__ = "business_settings"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), unique=True, nullable=False)

    business_name = Column(String, nullable=False, default="My Roofing Company")
    first_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    workspace = relationship("Workspace", back_populates="settings")


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)

    phone_number = Column(String, index=True, nullable=False)
    source = Column(String, default="missed_call", nullable=False)

    conversation_state = Column(String, default="awaiting_job_type", nullable=False)
    status = Column(String, default="new", nullable=False)

    job_type = Column(String, nullable=True)
    postal_code = Column(String, nullable=True)
    customer_name = Column(String, nullable=True)
    urgency = Column(String, nullable=True)
    priority = Column(String, nullable=True)
    insurance_claim = Column(String, nullable=True)

    crm_status = Column(String, default="new", nullable=False)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    workspace = relationship("Workspace", back_populates="leads")
    messages = relationship("Message", back_populates="lead", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)

    direction = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    workspace = relationship("Workspace", back_populates="messages")
    lead = relationship("Lead", back_populates="messages")
