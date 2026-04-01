from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
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


class BusinessSettings(Base):
    __tablename__ = "business_settings"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), unique=True, nullable=False)

    business_name = Column(String, nullable=False, default="My Roofing Company")
    first_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    workspace = relationship("Workspace", back_populates="settings")

class AppUser(Base):
    __tablename__ = "app_users"

    id = Column(Integer, primary_key=True, index=True)
    full_name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    company_name = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    owned_workspaces = relationship("Workspace", back_populates="owner")
    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")

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

    owner = relationship("AppUser", back_populates="owned_workspaces")
    settings = relationship("BusinessSettings", back_populates="workspace", uselist=False)
    leads = relationship("Lead", back_populates="workspace", cascade="all, delete-orphan")
    messages = relationship("Message", back_populates="workspace", cascade="all, delete-orphan")

workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
workspace = relationship("Workspace", back_populates="leads")

workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
workspace = relationship("Workspace", back_populates="messages")

def create_workspace_for_user(
    db: Session,
    user: models.AppUser,
    plan: str,
    company_name: str,
    business_phone: str,
    primary_service_area: str,
) -> models.Workspace:
    workspace = models.Workspace(
        company_name=company_name.strip(),
        plan=plan.strip().lower(),
        business_phone=business_phone.strip(),
        primary_service_area=primary_service_area.strip(),
        owner_user_id=user.id,
    )
    db.add(workspace)
    db.flush()

    settings = models.BusinessSettings(
        workspace_id=workspace.id,
        business_name=company_name.strip(),
        first_message="Hey, thanks for calling. We missed you — are you looking for a repair, replacement, or inspection?",
    )
    db.add(settings)
    db.commit()
    db.refresh(workspace)
    return workspace
