import os
import json
import secrets
import hashlib
import smtplib
from urllib.parse import quote_plus
from pathlib import Path
from urllib.parse import quote_plus
from typing import Optional
from datetime import datetime, timedelta
from email.message import EmailMessage

import stripe
from passlib.context import CryptContext
from fastapi import FastAPI, Request, Depends, Form, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from twilio.twiml.messaging_response import MessagingResponse

from .db import Base, engine, get_db
from . import models, schemas, logic
import re


# --------------------------------------------------
# APP SETUP
# --------------------------------------------------

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Roofing Front Desk")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --------------------------------------------------
# ENV / CONFIG
# --------------------------------------------------

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "").strip()

APP_BASE_URL = os.getenv("APP_BASE_URL", "https://www.roofingfrontdesk.com").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

STRIPE_PRICE_PILOT = os.getenv("STRIPE_PRICE_PILOT", "").strip()
STRIPE_PRICE_PILOT_SETUP = os.getenv("STRIPE_PRICE_PILOT_SETUP", "").strip()

STRIPE_PRICE_GROWTH = os.getenv("STRIPE_PRICE_GROWTH", "").strip()
STRIPE_PRICE_GROWTH_SETUP = os.getenv("STRIPE_PRICE_GROWTH_SETUP", "").strip()


# --------------------------------------------------
# AUTH CONFIG
# --------------------------------------------------

pwd_context = CryptContext(
    schemes=["argon2"],
    deprecated="auto",
)

AUTH_COOKIE_NAME = "rfd_session"
AUTH_COOKIE_SAMESITE = "lax"
AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes"}

SHORT_SESSION_DAYS = 1
REMEMBER_ME_DAYS = 30
PASSWORD_RESET_CODE_MINUTES = 10
PASSWORD_RESET_VERIFY_MINUTES = 15
EMAIL_CHANGE_CODE_MINUTES = 10

# --------------------------------------------------
# TEMPLATE HELPERS
# --------------------------------------------------

def format_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    return dt.strftime("%Y-%m-%d %I:%M %p")


templates.env.globals["format_dt"] = format_dt


def note_preview(raw_notes: Optional[str]) -> str:
    raw = (raw_notes or "").strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and parsed:
            first = parsed[0]
            if isinstance(first, dict):
                return str(first.get("text") or "").strip()
    except json.JSONDecodeError:
        pass
    return raw


templates.env.globals["note_preview"] = note_preview

PILOT_GROWTH_UPGRADE_COPY = {
    "pipeline_reporting": "Pipeline and reporting are available on Growth. Upgrade to Growth to unlock this view.",
    "advanced_stage_management": "Advanced pipeline stage management is available on Growth. Upgrade to Growth to move leads beyond Qualified.",
    "lead_limit": "Monthly recovered lead limit reached on your current plan. Upgrade to continue capturing new leads.",
}
PILOT_BLOCKED_STAGE_UPDATES = {"contacted", "booked", "closed", "lost"}


# --------------------------------------------------
# AUTH HELPERS
# --------------------------------------------------

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, password_hash: str) -> bool:
    return pwd_context.verify(plain_password, password_hash)


def create_user_session(db: Session, user_id: int, remember_me: bool) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(48)
    expires_at = datetime.utcnow() + timedelta(
        days=REMEMBER_ME_DAYS if remember_me else SHORT_SESSION_DAYS
    )

    session = models.UserSession(
        user_id=user_id,
        token=token,
        expires_at=expires_at,
    )
    db.add(session)
    db.commit()

    return token, expires_at


def set_auth_cookie(response: Response, token: str, expires_at: datetime) -> None:
    max_age = int((expires_at - datetime.utcnow()).total_seconds())

    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=max_age,
        expires=max_age,
        httponly=True,
        secure=AUTH_COOKIE_SECURE,
        samesite=AUTH_COOKIE_SAMESITE,
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        path="/",
        samesite=AUTH_COOKIE_SAMESITE,
    )


def get_current_user_from_cookie(request: Request, db: Session) -> Optional[models.AppUser]:
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        return None

    session = (
        db.query(models.UserSession)
        .filter(models.UserSession.token == token)
        .first()
    )

    if not session:
        return None

    if session.expires_at < datetime.utcnow():
        db.delete(session)
        db.commit()
        return None

    return session.user


def get_current_session_from_cookie(request: Request, db: Session) -> Optional[models.UserSession]:
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        return None

    session = (
        db.query(models.UserSession)
        .filter(models.UserSession.token == token)
        .first()
    )
    if not session:
        return None
    if session.expires_at < datetime.utcnow():
        db.delete(session)
        db.commit()
        return None
    return session

def refresh_user_session(
    request: Request,
    response: Response,
    db: Session,
    remember_me: bool = True,
) -> Optional[models.AppUser]:
    session = get_current_session_from_cookie(request, db)
    if not session:
        return None

    new_expiry = datetime.utcnow() + timedelta(
        days=REMEMBER_ME_DAYS if remember_me else SHORT_SESSION_DAYS
    )
    session.expires_at = new_expiry
    db.commit()

    set_auth_cookie(response, session.token, new_expiry)
    return session.user


def generate_reset_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_reset_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def send_password_reset_code(email: str, code: str) -> bool:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    smtp_from = os.getenv("SMTP_FROM", "").strip() or smtp_user
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_tls = os.getenv("SMTP_USE_TLS", "1").strip().lower() in {"1", "true", "yes"}
    smtp_timeout_seconds = int(os.getenv("SMTP_TIMEOUT_SECONDS", "60"))

    if not smtp_host or not smtp_from:
        print(f"[PASSWORD RESET] No SMTP configured. Email={email}, code={code}")
        return False

    message = EmailMessage()
    message["Subject"] = "Your Roofing Front Desk password reset code"
    message["From"] = smtp_from
    message["To"] = email
    message.set_content(
        "Use this one-time code to reset your password:\n\n"
        f"{code}\n\n"
        f"This code expires in {PASSWORD_RESET_CODE_MINUTES} minutes."
    )

    for attempt in range(1, 3):
        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=smtp_timeout_seconds) as smtp:
                smtp.ehlo()
                if smtp_tls:
                    smtp.starttls()
                    smtp.ehlo()
                if smtp_user:
                    smtp.login(smtp_user, smtp_password)
                smtp.send_message(message)
            return True
        except Exception as exc:
            if attempt == 2:
                print(f"[PASSWORD RESET ERROR] Failed to send email to {email}: {exc}")
                return False
            print(f"[PASSWORD RESET WARN] Attempt {attempt} failed for {email}: {exc}. Retrying...")


def send_email_change_code(email: str, code: str) -> bool:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    smtp_from = os.getenv("SMTP_FROM", "").strip() or smtp_user
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_tls = os.getenv("SMTP_USE_TLS", "1").strip().lower() in {"1", "true", "yes"}
    smtp_timeout_seconds = int(os.getenv("SMTP_TIMEOUT_SECONDS", "60"))

    if not smtp_host or not smtp_from:
        print(f"[EMAIL CHANGE OTP] No SMTP configured. Email={email}, code={code}")
        return False

    message = EmailMessage()
    message["Subject"] = "Your Roofing Front Desk email change code"
    message["From"] = smtp_from
    message["To"] = email
    message.set_content(
        "Use this one-time code to confirm your new email:\n\n"
        f"{code}\n\n"
        f"This code expires in {EMAIL_CHANGE_CODE_MINUTES} minutes."
    )

    for attempt in range(1, 3):
        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=smtp_timeout_seconds) as smtp:
                smtp.ehlo()
                if smtp_tls:
                    smtp.starttls()
                    smtp.ehlo()
                if smtp_user:
                    smtp.login(smtp_user, smtp_password)
                smtp.send_message(message)
            return True
        except smtplib.SMTPException as exc:
            if attempt == 2:
                print(f"[EMAIL CHANGE OTP ERROR] Failed to send email to {email}: {exc}")
                return False
            print(f"[EMAIL CHANGE OTP WARN] Attempt {attempt} failed for {email}: {exc}. Retrying...")
        except Exception as exc:
            print(f"[EMAIL CHANGE OTP ERROR] Unexpected error sending to {email}: {exc}")
            return False

    return False


def send_billing_receipt_email(
    recipient_email: str,
    amount_paid_cents: Optional[int] = None,
    currency: Optional[str] = None,
    invoice_number: Optional[str] = None,
    hosted_invoice_url: Optional[str] = None,
    invoice_pdf_url: Optional[str] = None,
) -> bool:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    smtp_from = os.getenv("SMTP_FROM", "").strip() or smtp_user
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_tls = os.getenv("SMTP_USE_TLS", "1").strip().lower() in {"1", "true", "yes"}
    smtp_timeout_seconds = int(os.getenv("SMTP_TIMEOUT_SECONDS", "60"))

    normalized_email = (recipient_email or "").strip().lower()
    if not normalized_email:
        return False

    if not smtp_host or not smtp_from:
        print(f"[BILLING RECEIPT] No SMTP configured. Email={normalized_email}")
        return False

    display_amount = "your recent payment"
    if amount_paid_cents is not None:
        amount = amount_paid_cents / 100
        display_currency = (currency or "usd").upper()
        display_amount = f"{display_currency} {amount:,.2f}"

    body_lines = [
        "Thanks for choosing Roofing Front Desk.",
        "",
        f"We received {display_amount}.",
    ]

    if invoice_number:
        body_lines.append(f"Invoice number: {invoice_number}")

    if hosted_invoice_url:
        body_lines.extend(["", f"View your receipt: {hosted_invoice_url}"])
    elif invoice_pdf_url:
        body_lines.extend(["", f"Download your receipt PDF: {invoice_pdf_url}"])

    body_lines.extend(["", "If you need anything, reply to this email and our team will help."])

    message = EmailMessage()
    message["Subject"] = "Your Roofing Front Desk receipt"
    message["From"] = smtp_from
    message["To"] = normalized_email
    message.set_content("\n".join(body_lines))

    for attempt in range(1, 3):
        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=smtp_timeout_seconds) as smtp:
                smtp.ehlo()
                if smtp_tls:
                    smtp.starttls()
                    smtp.ehlo()
                if smtp_user:
                    smtp.login(smtp_user, smtp_password)
                smtp.send_message(message)
            return True
        except Exception as exc:
            if attempt == 2:
                print(f"[BILLING RECEIPT ERROR] Failed to send email to {normalized_email}: {exc}")
                return False
            print(f"[BILLING RECEIPT WARN] Attempt {attempt} failed for {normalized_email}: {exc}. Retrying...")
    
# --------------------------------------------------
# GENERIC HELPERS
# --------------------------------------------------

def stripe_attr(obj, name: str, default=None):
    try:
        value = getattr(obj, name)
        return default if value is None else value
    except Exception:
        pass

    try:
        value = obj[name]
        return default if value is None else value
    except Exception:
        return default


def get_response_sla_label(priority: Optional[str]) -> str:
    priority = (priority or "").lower()
    if priority == "high":
        return "Under 5 min"
    if priority == "medium":
        return "Same day"
    return "Business hours"


def get_checkout_prices(plan: str) -> tuple[str, list[dict]]:
    normalized = (plan or "").strip().lower()

    if normalized == "pilot":
        if not STRIPE_PRICE_PILOT:
            raise HTTPException(status_code=500, detail="Missing STRIPE_PRICE_PILOT")

        line_items = [{"price": STRIPE_PRICE_PILOT, "quantity": 1}]
        if STRIPE_PRICE_PILOT_SETUP:
            line_items.append({"price": STRIPE_PRICE_PILOT_SETUP, "quantity": 1})

        return "Roofing Front Desk Pilot", line_items

    if normalized == "growth":
        if not STRIPE_PRICE_GROWTH:
            raise HTTPException(status_code=500, detail="Missing STRIPE_PRICE_GROWTH")

        line_items = [{"price": STRIPE_PRICE_GROWTH, "quantity": 1}]
        if STRIPE_PRICE_GROWTH_SETUP:
            line_items.append({"price": STRIPE_PRICE_GROWTH_SETUP, "quantity": 1})

        return "Roofing Front Desk Growth", line_items

    raise HTTPException(status_code=400, detail="Invalid plan")


# --------------------------------------------------
# WORKSPACE HELPERS
# --------------------------------------------------

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
        plan=(plan or "pilot").strip().lower(),
        business_phone=(business_phone or "").strip(),
        primary_service_area=(primary_service_area or "").strip(),
        owner_user_id=user.id,
        status="pending",
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


def get_current_workspace(request: Request, db: Session) -> Optional[models.Workspace]:
    user = get_current_user_from_cookie(request, db)
    if not user:
        return None

    return (
        db.query(models.Workspace)
        .filter(models.Workspace.owner_user_id == user.id)
        .first()
    )


def is_pilot_workspace(workspace: Optional[models.Workspace]) -> bool:
    if not workspace:
        return False
    return (workspace.plan or "").strip().lower() == "pilot"


def pilot_upgrade_message(feature_key: str) -> Optional[str]:
    normalized_feature_key = (feature_key or "").strip().lower()
    if not normalized_feature_key:
        return None
    return PILOT_GROWTH_UPGRADE_COPY.get(normalized_feature_key)


def get_workspace_settings(db: Session, workspace_id: int) -> models.BusinessSettings:
    settings = (
        db.query(models.BusinessSettings)
        .filter(models.BusinessSettings.workspace_id == workspace_id)
        .first()
    )

    if settings:
        return settings

    workspace = (
        db.query(models.Workspace)
        .filter(models.Workspace.id == workspace_id)
        .first()
    )

    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")

    settings = models.BusinessSettings(
        workspace_id=workspace.id,
        business_name=workspace.company_name or "Roofing Front Desk",
        first_message="Hey, thanks for calling. We missed you — are you looking for a repair, replacement, or inspection?",
    )
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


def get_plan_display_name(plan: Optional[str]) -> str:
    normalized = (plan or "").strip().lower()
    if normalized == "growth":
        return "Growth"
    return "Pilot"


def get_subscription_snapshot(workspace: Optional[models.Workspace]) -> dict:
    snapshot = {
        "plan_label": get_plan_display_name(workspace.plan if workspace else "pilot"),
        "status_label": (workspace.status or "pending").replace("_", " ").title() if workspace else "Pending",
        "has_subscription": bool(workspace and workspace.stripe_subscription_id),
        "cancel_at_period_end": False,
        "current_period_end": None,
    }

    if not workspace or not stripe.api_key or not workspace.stripe_subscription_id:
        return snapshot

    try:
        subscription = stripe.Subscription.retrieve(workspace.stripe_subscription_id)
        snapshot["cancel_at_period_end"] = bool(stripe_attr(subscription, "cancel_at_period_end", False))
        period_end = stripe_attr(subscription, "current_period_end")
        if period_end:
            snapshot["current_period_end"] = datetime.utcfromtimestamp(int(period_end))
        status = stripe_attr(subscription, "status")
        if status:
            snapshot["status_label"] = str(status).replace("_", " ").title()
    except Exception:
        pass

    return snapshot


# --------------------------------------------------
# DEMO DATA HELPERS
# --------------------------------------------------

def get_dashboard_stats(db: Session) -> dict:
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    week_start = now - timedelta(days=7)
    month_start = now - timedelta(days=30)

    new_today = db.query(models.Lead).filter(models.Lead.created_at >= today_start).count()

    hot_leads = (
        db.query(models.Lead)
        .filter(models.Lead.status == "qualified")
        .filter(models.Lead.priority == "high")
        .count()
    )

    contacted_count = (
        db.query(models.Lead)
        .filter(models.Lead.crm_status == "contacted")
        .filter(models.Lead.updated_at >= today_start)
        .count()
    )

    booked_week = (
        db.query(models.Lead)
        .filter(models.Lead.crm_status == "booked")
        .filter(models.Lead.updated_at >= week_start)
        .count()
    )

    won_month = (
        db.query(models.Lead)
        .filter(models.Lead.crm_status == "closed")
        .filter(models.Lead.updated_at >= month_start)
        .count()
    )

    emergency_overdue = (
        db.query(models.Lead)
        .filter(models.Lead.status == "qualified")
        .filter(models.Lead.priority == "high")
        .filter(models.Lead.crm_status.in_(["new", "contacted"]))
        .count()
    )

    recent_hot_leads = (
        db.query(models.Lead)
        .filter(models.Lead.status == "qualified")
        .order_by(models.Lead.updated_at.desc())
        .limit(8)
        .all()
    )
    pipeline_leads = db.query(models.Lead).all()

    return {
        "new_today": new_today,
        "hot_leads": hot_leads,
        "contacted_count": contacted_count,
        "booked_week": booked_week,
        "won_month": won_month,
        "emergency_overdue": emergency_overdue,
        "recent_hot_leads": recent_hot_leads,
        "response_sla_label": get_response_sla_label("high"),
        "estimated_pipeline_value": calculate_pipeline_value(pipeline_leads),
    }


def get_inbox_leads(
    db: Session,
    crm_status_filter: str,
    search: str,
    priority_filter: str,
    insurance_filter: str,
):
    query = db.query(models.Lead)

    if crm_status_filter != "all":
        if crm_status_filter == "qualified":
            query = query.filter(models.Lead.status == "qualified")
        else:
            query = query.filter(models.Lead.crm_status == crm_status_filter)

    if priority_filter != "all":
        query = query.filter(models.Lead.priority == priority_filter)

    if insurance_filter != "all":
        query = query.filter(models.Lead.insurance_claim == insurance_filter)

    if search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(
            (models.Lead.customer_name.ilike(term))
            | (models.Lead.phone_number.ilike(term))
            | (models.Lead.postal_code.ilike(term))
        )

    return query.order_by(models.Lead.updated_at.desc()).all()


# --------------------------------------------------
# REAL APP HELPERS
# --------------------------------------------------

def get_dashboard_stats_for_workspace(db: Session, workspace_id: int) -> dict:
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    week_start = now - timedelta(days=7)
    month_start = now - timedelta(days=30)

    new_today = (
        db.query(models.Lead)
        .filter(models.Lead.workspace_id == workspace_id)
        .filter(models.Lead.created_at >= today_start)
        .count()
    )

    hot_leads = (
        db.query(models.Lead)
        .filter(models.Lead.workspace_id == workspace_id)
        .filter(models.Lead.status == "qualified")
        .filter(models.Lead.priority == "high")
        .count()
    )

    contacted_count = (
        db.query(models.Lead)
        .filter(models.Lead.workspace_id == workspace_id)
        .filter(models.Lead.crm_status == "contacted")
        .filter(models.Lead.updated_at >= today_start)
        .count()
    )

    booked_week = (
        db.query(models.Lead)
        .filter(models.Lead.workspace_id == workspace_id)
        .filter(models.Lead.crm_status == "booked")
        .filter(models.Lead.updated_at >= week_start)
        .count()
    )

    won_month = (
        db.query(models.Lead)
        .filter(models.Lead.workspace_id == workspace_id)
        .filter(models.Lead.crm_status == "closed")
        .filter(models.Lead.updated_at >= month_start)
        .count()
    )

    emergency_overdue = (
        db.query(models.Lead)
        .filter(models.Lead.workspace_id == workspace_id)
        .filter(models.Lead.status == "qualified")
        .filter(models.Lead.priority == "high")
        .filter(models.Lead.crm_status.in_(["new", "contacted"]))
        .count()
    )

    recent_hot_leads = (
        db.query(models.Lead)
        .filter(models.Lead.workspace_id == workspace_id)
        .filter(models.Lead.status == "qualified")
        .order_by(models.Lead.updated_at.desc())
        .limit(8)
        .all()
    )
    pipeline_leads = (
        db.query(models.Lead)
        .filter(models.Lead.workspace_id == workspace_id)
        .all()
    )

    return {
        "new_today": new_today,
        "hot_leads": hot_leads,
        "contacted_count": contacted_count,
        "booked_week": booked_week,
        "won_month": won_month,
        "emergency_overdue": emergency_overdue,
        "recent_hot_leads": recent_hot_leads,
        "response_sla_label": get_response_sla_label("high"),
        "estimated_pipeline_value": calculate_pipeline_value(pipeline_leads),
    }


def get_inbox_leads_for_workspace(
    db: Session,
    workspace_id: int,
    crm_status_filter: str,
    search: str,
    priority_filter: str,
    insurance_filter: str,
):
    query = db.query(models.Lead).filter(models.Lead.workspace_id == workspace_id)

    if crm_status_filter != "all":
        if crm_status_filter == "qualified":
            query = query.filter(models.Lead.status == "qualified")
        else:
            query = query.filter(models.Lead.crm_status == crm_status_filter)

    if priority_filter != "all":
        query = query.filter(models.Lead.priority == priority_filter)

    if insurance_filter != "all":
        query = query.filter(models.Lead.insurance_claim == insurance_filter)

    if search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(
            (models.Lead.customer_name.ilike(term))
            | (models.Lead.phone_number.ilike(term))
            | (models.Lead.postal_code.ilike(term))
        )

    return query.order_by(models.Lead.updated_at.desc()).all()


# --------------------------------------------------
# SHARED VIEW HELPERS
# --------------------------------------------------

def build_pipeline_columns(leads):
    columns = {
        "new": [],
        "qualified": [],
        "contacted": [],
        "booked": [],
        "closed": [],
        "lost": [],
    }

    for lead in leads:
        if lead.crm_status == "new":
            if lead.status == "qualified":
                columns["qualified"].append(lead)
            else:
                columns["new"].append(lead)
        elif lead.crm_status == "contacted":
            columns["contacted"].append(lead)
        elif lead.crm_status == "booked":
            columns["booked"].append(lead)
        elif lead.crm_status == "closed":
            columns["closed"].append(lead)
        elif lead.crm_status == "lost":
            columns["lost"].append(lead)
        else:
            columns["new"].append(lead)

    return columns


def estimate_lead_value(lead: models.Lead) -> int:
    if lead.job_type == "replacement":
        return 18000
    if lead.job_type == "repair":
        return 3500
    if lead.job_type == "inspection":
        return 750
    return 2500


def calculate_pipeline_value(leads: list[models.Lead]) -> int:
    return sum(estimate_lead_value(lead) for lead in leads)


def build_activity_log(lead: models.Lead) -> list[dict]:
    activity_log: list[dict] = []

    if lead.created_at:
        activity_log.append(
            {
                "time": lead.created_at,
                "title": "Lead created",
                "detail": f"Lead entered from source: {lead.source or 'unknown'}",
                "type": "system",
            }
        )

    if lead.messages:
        first_msg = lead.messages[0]
        activity_log.append(
            {
                "time": first_msg.created_at,
                "title": "Conversation started",
                "detail": f"First {first_msg.direction} message recorded",
                "type": "message",
            }
        )

        if lead.job_type and lead.postal_code and lead.urgency:
            activity_log.append(
                {
                    "time": lead.updated_at or lead.created_at,
                    "title": "Qualification completed",
                    "detail": "Core intake fields captured (job type, postal code, urgency)",
                    "type": "qualified",
                }
            )

    if lead.insurance_claim == "yes":
        activity_log.append(
            {
                "time": lead.updated_at or lead.created_at,
                "title": "Insurance claim detected",
                "detail": "Lead indicated insurance involvement",
                "type": "insurance",
            }
        )

    if lead.priority == "high":
        activity_log.append(
            {
                "time": lead.updated_at or lead.created_at,
                "title": "Marked high priority",
                "detail": "Recommended rapid callback due to urgency",
                "type": "priority",
            }
        )

    if lead.crm_status and lead.crm_status != "new":
        activity_log.append(
            {
                "time": lead.updated_at or lead.created_at,
                "title": "Pipeline updated",
                "detail": f"Lead moved to {lead.crm_status.title()}",
                "type": "pipeline",
            }
        )

    if lead.notes and lead.notes.strip():
        activity_log.append(
            {
                "time": lead.updated_at or lead.created_at,
                "title": "Internal notes added",
                "detail": "Operator added internal context for follow-up",
                "type": "notes",
            }
        )

    return sorted(activity_log, key=lambda x: x["time"] or lead.created_at, reverse=True)
# --------------------------------------------------
# PUBLIC PAGES
# --------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def landing_page(request: Request):
    return templates.TemplateResponse(
        request,
        "landing.html",
        {"page_title": "Roofing Front Desk"},
    )


@app.get("/pricing", response_class=HTMLResponse)
def pricing_page(request: Request):
    return templates.TemplateResponse(
        request,
        "pricing.html",
        {"page_title": "Pricing"},
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "page_title": "Login",
            "error_message": None,
            "form_data": {"email": ""},
        },
    )


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    return templates.TemplateResponse(
        request,
        "forgot_password.html",
        {
            "page_title": "Forgot Password",
            "error_message": None,
            "info_message": None,
            "form_data": {"email": ""},
        },
    )


@app.post("/forgot-password", response_class=HTMLResponse)
def forgot_password_submit(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    normalized_email = email.strip().lower()
    user = (
        db.query(models.AppUser)
        .filter(models.AppUser.email == normalized_email)
        .first()
    )

    if not user:
        return templates.TemplateResponse(
            request,
            "forgot_password.html",
            {
                "page_title": "Forgot Password",
                "error_message": "No account found for that email. Please sign up first.",
                "info_message": None,
                "form_data": {"email": normalized_email},
            },
            status_code=404,
        )

    db.query(models.PasswordResetCode).filter(
        models.PasswordResetCode.user_id == user.id,
        models.PasswordResetCode.used_at.is_(None),
    ).update({models.PasswordResetCode.used_at: datetime.utcnow()})

    code = generate_reset_code()
    reset_record = models.PasswordResetCode(
        user_id=user.id,
        email=normalized_email,
        code_hash=hash_reset_code(code),
        expires_at=datetime.utcnow() + timedelta(minutes=PASSWORD_RESET_CODE_MINUTES),
    )
    db.add(reset_record)
    db.commit()

    sent_to_email = send_password_reset_code(normalized_email, code)
    preview_code = None if sent_to_email else code

    return templates.TemplateResponse(
        request,
        "forgot_password_verify.html",
        {
            "page_title": "Verify Reset Code",
            "error_message": None if sent_to_email else "Email delivery is not configured yet. Use the dev code below.",
            "info_message": "We sent a one-time code to your email." if sent_to_email else "Use the temporary code below to continue.",
            "form_data": {"email": normalized_email, "code": ""},
            "dev_code": preview_code,
            "email_sent": sent_to_email,
        },
    )


@app.post("/forgot-password/verify", response_class=HTMLResponse)
def forgot_password_verify_submit(
    request: Request,
    email: str = Form(...),
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    normalized_email = email.strip().lower()
    normalized_code = code.strip()
    record = (
        db.query(models.PasswordResetCode)
        .filter(models.PasswordResetCode.email == normalized_email)
        .filter(models.PasswordResetCode.used_at.is_(None))
        .order_by(models.PasswordResetCode.created_at.desc())
        .first()
    )

    if (
        not record
        or record.expires_at < datetime.utcnow()
        or record.code_hash != hash_reset_code(normalized_code)
    ):
        return templates.TemplateResponse(
            request,
            "forgot_password_verify.html",
            {
                "page_title": "Verify Reset Code",
                "error_message": "Invalid or expired code. Please request a new one.",
                "info_message": None,
                "form_data": {"email": normalized_email, "code": normalized_code},
                "dev_code": None,
                "email_sent": True,
            },
            status_code=400,
        )

    verify_token = secrets.token_urlsafe(32)
    record.verification_token = verify_token
    record.verification_expires_at = datetime.utcnow() + timedelta(minutes=PASSWORD_RESET_VERIFY_MINUTES)
    db.commit()

    return RedirectResponse(url=f"/reset-password?token={verify_token}", status_code=303)


@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str = Query(...), db: Session = Depends(get_db)):
    record = (
        db.query(models.PasswordResetCode)
        .filter(models.PasswordResetCode.verification_token == token)
        .filter(models.PasswordResetCode.used_at.is_(None))
        .first()
    )
    if not record or not record.verification_expires_at or record.verification_expires_at < datetime.utcnow():
        return RedirectResponse(url="/forgot-password", status_code=303)

    return templates.TemplateResponse(
        request,
        "reset_password.html",
        {
            "page_title": "Reset Password",
            "error_message": None,
            "token": token,
        },
    )


@app.post("/reset-password", response_class=HTMLResponse)
def reset_password_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    record = (
        db.query(models.PasswordResetCode)
        .filter(models.PasswordResetCode.verification_token == token)
        .filter(models.PasswordResetCode.used_at.is_(None))
        .first()
    )
    if not record or not record.verification_expires_at or record.verification_expires_at < datetime.utcnow():
        return RedirectResponse(url="/forgot-password", status_code=303)

    if password != confirm_password:
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {"page_title": "Reset Password", "error_message": "Passwords do not match.", "token": token},
            status_code=400,
        )

    password_error = validate_password_rules(password)
    if password_error:
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {"page_title": "Reset Password", "error_message": password_error, "token": token},
            status_code=400,
        )

    user = db.query(models.AppUser).filter(models.AppUser.id == record.user_id).first()
    if not user:
        return RedirectResponse(url="/forgot-password", status_code=303)

    user.password_hash = hash_password(password)
    record.used_at = datetime.utcnow()
    record.verification_token = None
    record.verification_expires_at = None
    db.commit()

    return RedirectResponse(url="/reset-password/success", status_code=303)


@app.get("/reset-password/success", response_class=HTMLResponse)
def reset_password_success_page(request: Request):
    return templates.TemplateResponse(
        request,
        "reset_password_success.html",
        {"page_title": "Password Updated"},
    )


@app.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    remember_me: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    normalized_email = email.strip().lower()
    user = (
        db.query(models.AppUser)
        .filter(models.AppUser.email == normalized_email)
        .first()
    )

    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "page_title": "Login",
                "error_message": "Invalid email or password.",
                "form_data": {"email": normalized_email},
            },
            status_code=401,
        )

    remember = remember_me == "1"
    token, expires_at = create_user_session(db, user.id, remember)

    workspace = (
        db.query(models.Workspace)
        .filter(models.Workspace.owner_user_id == user.id)
        .first()
    )

    destination = "/app/dashboard" if workspace else "/signup"

    response = RedirectResponse(url=destination, status_code=303)
    set_auth_cookie(response, token, expires_at)
    return response


@app.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(AUTH_COOKIE_NAME)

    if token:
        session = (
            db.query(models.UserSession)
            .filter(models.UserSession.token == token)
            .first()
        )
        if session:
            db.delete(session)
            db.commit()

    response = RedirectResponse(url="/login", status_code=303)
    clear_auth_cookie(response)
    return response

@app.get("/signup", response_class=HTMLResponse)
def signup_page(
    request: Request,
    plan: str = Query("pilot"),
):
    return templates.TemplateResponse(
        request,
        "signup.html",
        {
            "page_title": "Start Setup",
            "plan": plan.lower(),
            "error_message": None,
            "form_data": {
                "company_name": "",
                "first_name": "",
                "last_name": "",
                "email": "",
                "city": "",
                "province": "",
            },
        },
    )


@app.post("/signup", response_class=HTMLResponse)
def signup_submit(
    request: Request,
    company_name: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    city: str = Form(...),
    province: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    plan: str = Form("pilot"),
    agree_terms: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    normalized_email = email.strip().lower()
    normalized_plan = (plan or "pilot").strip().lower()

    form_data = {
        "company_name": company_name.strip(),
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "email": normalized_email,
        "city": city.strip(),
        "province": province.strip(),
    }

    if not company_name.strip():
        return templates.TemplateResponse(
            request,
            "signup.html",
            {
                "page_title": "Start Setup",
                "plan": normalized_plan,
                "error_message": "Business name is required.",
                "form_data": form_data,
            },
            status_code=400,
        )

    if not province.strip():
        return templates.TemplateResponse(
            request,
            "signup.html",
            {
                "page_title": "Start Setup",
                "plan": normalized_plan,
                "error_message": "Please select a province.",
                "form_data": form_data,
            },
            status_code=400,
        )

    if not agree_terms:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {
                "page_title": "Start Setup",
                "plan": normalized_plan,
                "error_message": "Please agree to the Terms of Service before continuing.",
                "form_data": form_data,
            },
            status_code=400,
        )

    if not first_name.strip():
        return templates.TemplateResponse(
            request,
            "signup.html",
            {
                "page_title": "Start Setup",
                "plan": normalized_plan,
                "error_message": "First name is required.",
                "form_data": form_data,
            },
            status_code=400,
        )

    if not last_name.strip():
        return templates.TemplateResponse(
            request,
            "signup.html",
            {
                "page_title": "Start Setup",
                "plan": normalized_plan,
                "error_message": "Last name is required.",
                "form_data": form_data,
            },
            status_code=400,
        )

    if not city.strip():
        return templates.TemplateResponse(
            request,
            "signup.html",
            {
                "page_title": "Start Setup",
                "plan": normalized_plan,
                "error_message": "City is required.",
                "form_data": form_data,
            },
            status_code=400,
        )

    valid_provinces = {
        "AB", "BC", "MB", "NB", "NL", "NS", "NT",
        "NU", "ON", "PE", "QC", "SK", "YT"
    }

    if province not in valid_provinces:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {
                "page_title": "Start Setup",
                "plan": normalized_plan,
                "error_message": "Please choose a valid province.",
                "form_data": form_data,
            },
            status_code=400,
        )

    existing_user = (
        db.query(models.AppUser)
        .filter(models.AppUser.email == normalized_email)
        .first()
    )
    if existing_user:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {
                "page_title": "Start Setup",
                "plan": normalized_plan,
                "error_message": "An account with this email already exists. Try logging in instead.",
                "form_data": form_data,
            },
            status_code=400,
        )

    if password != confirm_password:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {
                "page_title": "Start Setup",
                "plan": normalized_plan,
                "error_message": "Passwords do not match.",
                "form_data": form_data,
            },
            status_code=400,
        )

    password_error = validate_password_rules(password)
    if password_error:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {
                "page_title": "Start Setup",
                "plan": normalized_plan,
                "error_message": password_error,
                "form_data": form_data,
            },
            status_code=400,
        )

    full_name = f"{first_name.strip()} {last_name.strip()}".strip()

    user = models.AppUser(
        full_name=full_name,
        email=normalized_email,
        company_name=company_name.strip(),
        password_hash=hash_password(password),
    )
    db.add(user)
    db.flush()

    create_workspace_for_user(
        db=db,
        user=user,
        plan=normalized_plan,
        company_name=company_name.strip(),
        business_phone="",
        primary_service_area=f"{city.strip()}, {province.strip()}",
    )

    token, expires_at = create_user_session(db, user.id, remember_me=True)

    save_business_step(
        db,
        user.id,
        {
            "company_name": company_name.strip(),
            "first_name": first_name.strip(),
            "last_name": last_name.strip(),
            "email": normalized_email,
            "city": city.strip(),
            "province": province.strip(),
        },
    )

    response = RedirectResponse(
        url=f"/onboarding/workflow?plan={normalized_plan}",
        status_code=303,
    )
    set_auth_cookie(response, token, expires_at)
    return response


@app.get("/onboarding/company")
def onboarding_company_page(plan: str = Query("pilot")):
    return RedirectResponse(url=f"/onboarding/business?plan={plan}", status_code=303)


@app.get("/onboarding/twilio", response_class=HTMLResponse)
def onboarding_twilio_page(request: Request):
    return templates.TemplateResponse(
        request,
        "onboarding/twilio.html",
        {"page_title": "Phone Setup"},
    )


@app.get("/onboarding/complete", response_class=HTMLResponse)
def onboarding_complete_page(request: Request):
    return templates.TemplateResponse(
        request,
        "onboarding/complete.html",
        {"page_title": "Setup Complete"},
    )


@app.get("/billing", response_class=HTMLResponse)
def billing_page(
    request: Request,
    plan: str = Query("pilot"),
    canceled: int = Query(0),
):
    selected_plan_slug = (plan or "pilot").lower()

    if selected_plan_slug == "growth":
        selected_plan = "Growth"
        monthly_price = "$999"
        due_today = "$999"
    else:
        selected_plan = "Pilot"
        monthly_price = "$499"
        due_today = "$499"

    return templates.TemplateResponse(
        request,
        "billing.html",
        {
            "page_title": "Billing",
            "selected_plan": selected_plan,
            "selected_plan_slug": selected_plan_slug,
            "monthly_price": monthly_price,
            "due_today": due_today,
            "canceled": bool(canceled),
        },
    )


@app.get("/billing/success", response_class=HTMLResponse)
def billing_success_page(
    request: Request,
    session_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    session_status = "complete"
    customer_email = None
    subscription_id = None
    receipt_url = None
    selected_plan = "Pilot"
    business_name = None
    phone_number = None

    current_user = get_current_user_from_cookie(request, db)
    user_to_auth = current_user

    if session_id and stripe.api_key:
        try:
            session_data = stripe.checkout.Session.retrieve(
                session_id,
                expand=["subscription.latest_invoice"]
            )

            session_status = getattr(session_data, "status", None) or "complete"

            customer_details = getattr(session_data, "customer_details", None)
            if customer_details:
                customer_email = getattr(customer_details, "email", None)

            subscription = getattr(session_data, "subscription", None)
            if subscription:
                subscription_id = getattr(subscription, "id", None) or subscription

                latest_invoice = getattr(subscription, "latest_invoice", None)
                if latest_invoice:
                    receipt_url = getattr(latest_invoice, "hosted_invoice_url", None)
                    if not receipt_url:
                        receipt_url = getattr(latest_invoice, "invoice_pdf", None)

            metadata = stripe_attr(session_data, "metadata", {}) or {}
            plan_key = stripe_attr(metadata, "plan")
            selected_plan = "Growth" if plan_key == "growth" else "Pilot"

            workspace_id = stripe_attr(metadata, "workspace_id")
            if workspace_id:
                workspace = (
                    db.query(models.Workspace)
                    .filter(models.Workspace.id == int(workspace_id))
                    .first()
                )
                if workspace:
                    business_name = workspace.company_name
                    phone_number = (
                        workspace.business_phone
                        or workspace.active_twilio_number
                        or workspace.pending_twilio_number
                    )

            if not user_to_auth and customer_email:
                user_to_auth = (
                    db.query(models.AppUser)
                    .filter(models.AppUser.email == customer_email.strip().lower())
                    .first()
                )

        except Exception:
            pass

    response = templates.TemplateResponse(
        request,
        "billing_success.html",
        {
            "page_title": "Welcome to Roofing Front Desk",
            "session_status": session_status,
            "customer_email": customer_email,
            "subscription_id": subscription_id,
            "receipt_url": receipt_url,
            "selected_plan": selected_plan,
            "business_name": business_name,
            "phone_number": phone_number,
        },
    )

    if user_to_auth and not current_user:
        token, expires_at = create_user_session(db, user_to_auth.id, remember_me=True)
        set_auth_cookie(response, token, expires_at)

    return response
    
# --------------------------------------------------
# STRIPE CHECKOUT
# --------------------------------------------------

@app.post("/stripe/create-checkout-session")
def create_checkout_session(
    request: Request,
    plan: str = Form(...),
    db: Session = Depends(get_db),
):
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Missing STRIPE_SECRET_KEY")

    workspace = get_current_workspace(request, db)
    current_user = get_current_user_from_cookie(request, db)
    plan_name, line_items = get_checkout_prices(plan)

    metadata = {
        "plan": plan.lower(),
        "plan_name": plan_name,
        "source": "roofing_front_desk_billing",
    }

    if workspace:
        metadata["workspace_id"] = str(workspace.id)

    try:
        checkout_payload = {
            "mode": "subscription",
            "line_items": line_items,
            "success_url": f"{APP_BASE_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{APP_BASE_URL}/billing?plan={plan}&canceled=1",
            "allow_promotion_codes": True,
            "metadata": metadata,
        }
        if current_user and current_user.email:
            checkout_payload["customer_email"] = current_user.email.strip().lower()

        checkout_session = stripe.checkout.Session.create(
            **checkout_payload,
        )
        return RedirectResponse(url=checkout_session.url, status_code=303)

    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Stripe checkout error: {exc}")


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Missing STRIPE_WEBHOOK_SECRET")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing Stripe signature")

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        session_id = stripe_attr(obj, "id")
        customer_id = stripe_attr(obj, "customer")
        subscription_id = stripe_attr(obj, "subscription")
        metadata = stripe_attr(obj, "metadata", {}) or {}
        workspace_id = stripe_attr(metadata, "workspace_id")
        if workspace_id:
            workspace = (
                db.query(models.Workspace)
                .filter(models.Workspace.id == int(workspace_id))
                .first()
            )
            if workspace:
                workspace.status = "active"
                workspace.stripe_customer_id = customer_id
                workspace.stripe_subscription_id = subscription_id

                if workspace.phone_mode == "new" and workspace.pending_twilio_number and not workspace.active_twilio_number:
                    try:
                        purchased_number = provision_twilio_number(workspace.pending_twilio_number)
                        workspace.active_twilio_number = getattr(
                            purchased_number,
                            "phone_number",
                            workspace.pending_twilio_number,
                        )
                        workspace.business_phone = workspace.active_twilio_number
                        workspace.pending_twilio_number = None
                    except Exception as exc:
                        print(f"twilio.provision.failed workspace={workspace.id} error={exc}")
                db.commit()

        print("checkout.session.completed", session_id)

    elif event_type == "customer.subscription.created":
        subscription_id = stripe_attr(obj, "id")
        status = stripe_attr(obj, "status")
        customer_id = stripe_attr(obj, "customer")

        print("customer.subscription.created", subscription_id)
        print("subscription_status", status)
        print("customer_id", customer_id)

    elif event_type == "customer.subscription.updated":
        subscription_id = stripe_attr(obj, "id")
        status = stripe_attr(obj, "status")
        customer_id = stripe_attr(obj, "customer")

        print("customer.subscription.updated", subscription_id)
        print("subscription_status", status)
        print("customer_id", customer_id)

    elif event_type == "customer.subscription.deleted":
        subscription_id = stripe_attr(obj, "id")
        status = stripe_attr(obj, "status")
        customer_id = stripe_attr(obj, "customer")

        print("customer.subscription.deleted", subscription_id)
        print("subscription_status", status)
        print("customer_id", customer_id)

    elif event_type == "invoice.paid":
        invoice_id = stripe_attr(obj, "id")
        customer_id = stripe_attr(obj, "customer")
        subscription_id = stripe_attr(obj, "subscription")
        invoice_number = stripe_attr(obj, "number")
        amount_paid = stripe_attr(obj, "amount_paid")
        currency = stripe_attr(obj, "currency")
        hosted_invoice_url = stripe_attr(obj, "hosted_invoice_url")
        invoice_pdf = stripe_attr(obj, "invoice_pdf")
        customer_email = stripe_attr(obj, "customer_email")

        if not customer_email and customer_id:
            try:
                customer_data = stripe.Customer.retrieve(customer_id)
                customer_email = stripe_attr(customer_data, "email")
            except Exception:
                customer_email = None

        if customer_email:
            send_billing_receipt_email(
                recipient_email=customer_email,
                amount_paid_cents=amount_paid,
                currency=currency,
                invoice_number=invoice_number,
                hosted_invoice_url=hosted_invoice_url,
                invoice_pdf_url=invoice_pdf,
            )

        print("invoice.paid", invoice_id)
        print("customer_id", customer_id)
        print("subscription_id", subscription_id)

    elif event_type == "invoice.payment_failed":
        invoice_id = stripe_attr(obj, "id")
        customer_id = stripe_attr(obj, "customer")
        subscription_id = stripe_attr(obj, "subscription")

        print("invoice.payment_failed", invoice_id)
        print("customer_id", customer_id)
        print("subscription_id", subscription_id)

    return JSONResponse({"received": True})
# --------------------------------------------------
# DEMO INDEX
# --------------------------------------------------

@app.get("/demo", response_class=HTMLResponse)
def demo_index(request: Request):
    return templates.TemplateResponse(
        request,
        "demo/index.html",
        {"page_title": "Product Demo"},
    )


# --------------------------------------------------
# DEMO APP PAGES
# --------------------------------------------------

@app.get("/demo/dashboard", response_class=HTMLResponse)
def demo_dashboard(request: Request, db: Session = Depends(get_db)):
    settings = logic.get_or_create_business_settings(db)
    stats = get_dashboard_stats(db)

    return templates.TemplateResponse(
        request,
        "demo/dashboard.html",
        {
            "settings": settings,
            "twilio_live": logic.twilio_enabled(),
            "active_page": "dashboard",
            "page_title": "Dashboard",
            "page_subtitle": "Your lead desk overview for today.",
            **stats,
        },
    )


@app.get("/demo/inbox", response_class=HTMLResponse)
def demo_inbox(
    request: Request,
    lead_id: Optional[int] = Query(None),
    crm_status_filter: str = Query("all"),
    search: str = Query(""),
    priority_filter: str = Query("all"),
    insurance_filter: str = Query("all"),
    db: Session = Depends(get_db),
):
    settings = logic.get_or_create_business_settings(db)

    leads = get_inbox_leads(
        db=db,
        crm_status_filter=crm_status_filter,
        search=search,
        priority_filter=priority_filter,
        insurance_filter=insurance_filter,
    )

    selected_lead = None
    if lead_id:
        selected_lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    elif leads:
        selected_lead = leads[0]

    high_priority_count = db.query(models.Lead).filter(models.Lead.priority == "high").count()
    qualified_count = db.query(models.Lead).filter(models.Lead.status == "qualified").count()
    booked_count = db.query(models.Lead).filter(models.Lead.crm_status == "booked").count()

    return templates.TemplateResponse(
        request,
        "demo/inbox.html",
        {
            "settings": settings,
            "leads": leads,
            "selected_lead": selected_lead,
            "twilio_live": logic.twilio_enabled(),
            "recommended_response_time": logic.recommended_response_time,
            "crm_status_filter": crm_status_filter,
            "priority_filter": priority_filter,
            "insurance_filter": insurance_filter,
            "search": search,
            "high_priority_count": high_priority_count,
            "qualified_count": qualified_count,
            "booked_count": booked_count,
            "selected_notes": parse_lead_notes(selected_lead) if selected_lead else [],
            "active_page": "inbox",
            "page_title": "Inbox",
            "page_subtitle": "All lead conversations in one workspace.",
        },
    )


@app.get("/demo/pipeline", response_class=HTMLResponse)
def demo_pipeline(request: Request, db: Session = Depends(get_db)):
    settings = logic.get_or_create_business_settings(db)
    leads = db.query(models.Lead).order_by(models.Lead.updated_at.desc()).all()
    columns = build_pipeline_columns(leads)
    estimated_pipeline_value = calculate_pipeline_value(leads)

    return templates.TemplateResponse(
        request,
        "demo/pipeline.html",
        {
            "settings": settings,
            "columns": columns,
            "estimated_pipeline_value": estimated_pipeline_value,
            "twilio_live": logic.twilio_enabled(),
            "active_page": "pipeline",
            "page_title": "Pipeline",
            "page_subtitle": "Track leads from qualification to outcome.",
        },
    )


@app.get("/demo/settings", response_class=HTMLResponse)
def demo_settings(request: Request, db: Session = Depends(get_db)):
    settings = logic.get_or_create_business_settings(db)
    current_user = get_current_user_from_cookie(request, db)
    first_name, last_name = split_full_name(current_user.full_name) if current_user else ("", "")
    workflow_steps = get_settings_workflow_steps(db, current_user, settings.business_name)

    return templates.TemplateResponse(
        request,
        "demo/settings.html",
        {
            "settings": settings,
            "workspace": None,
            "current_user": current_user,
            "profile_first_name": first_name,
            "profile_last_name": last_name,
            "workflow_steps": workflow_steps,
            "twilio_live": logic.twilio_enabled(),
            "active_page": "settings",
            "page_title": "Settings",
            "page_subtitle": "Manage business identity and workspace basics.",
        },
    )


@app.get("/demo/lead/{lead_id}", response_class=HTMLResponse)
def demo_lead_detail(request: Request, lead_id: int, db: Session = Depends(get_db)):
    settings = logic.get_or_create_business_settings(db)
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()

    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    activity_log = build_activity_log(lead)
    stage_options = [
        ("new", "New"),
        ("qualified", "Qualified"),
        ("contacted", "Contacted"),
        ("booked", "Estimate Scheduled"),
        ("closed", "Won"),
        ("lost", "Lost"),
    ]

    return templates.TemplateResponse(
        request,
        "lead_detail.html",
        {
            "settings": settings,
            "lead": lead,
            "activity_log": activity_log,
            "stage_options": stage_options,
            "twilio_live": logic.twilio_enabled(),
            "recommended_response_time": logic.recommended_response_time,
            "active_page": "pipeline",
            "page_title": "Lead Record",
            "page_subtitle": "Review the full lead lifecycle and update next actions.",
        },
    )


# --------------------------------------------------
# REAL APP PAGES
# --------------------------------------------------

@app.get("/app/dashboard", response_class=HTMLResponse)
def app_dashboard(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user_from_cookie(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    workspace = get_current_workspace(request, db)
    if not workspace:
        return RedirectResponse(url="/signup", status_code=303)

    settings = get_workspace_settings(db, workspace.id)
    stats = get_dashboard_stats_for_workspace(db, workspace.id)

    return templates.TemplateResponse(
        request,
        "demo/dashboard.html",
        {
            "settings": settings,
            "workspace": workspace,
            "current_user": current_user,
            "twilio_live": logic.twilio_enabled(),
            "active_page": "dashboard",
            "page_title": "Dashboard",
            "page_subtitle": "Your lead desk overview for today.",
            **stats,
        },
    )


@app.get("/app/inbox", response_class=HTMLResponse)
def app_inbox(
    request: Request,
    lead_id: Optional[int] = Query(None),
    crm_status_filter: str = Query("all"),
    search: str = Query(""),
    priority_filter: str = Query("all"),
    insurance_filter: str = Query("all"),
    upgrade_required: str = Query(""),
    limit_message: str = Query(""),
    db: Session = Depends(get_db),
):
    current_user = get_current_user_from_cookie(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    workspace = get_current_workspace(request, db)
    if not workspace:
        return RedirectResponse(url="/signup", status_code=303)

    settings = get_workspace_settings(db, workspace.id)

    leads = get_inbox_leads_for_workspace(
        db=db,
        workspace_id=workspace.id,
        crm_status_filter=crm_status_filter,
        search=search,
        priority_filter=priority_filter,
        insurance_filter=insurance_filter,
    )

    selected_lead = None
    if lead_id:
        selected_lead = (db.query(models.Lead)
            .filter(models.Lead.workspace_id == workspace.id)
            .filter(models.Lead.id == lead_id)
            .first()
        )
    elif leads:
        selected_lead = leads[0]

    high_priority_count = (
        db.query(models.Lead)
        .filter(models.Lead.workspace_id == workspace.id)
        .filter(models.Lead.priority == "high")
        .count()
    )
    qualified_count = (
        db.query(models.Lead)
        .filter(models.Lead.workspace_id == workspace.id)
        .filter(models.Lead.status == "qualified")
        .count()
    )
    booked_count = (
        db.query(models.Lead)
        .filter(models.Lead.workspace_id == workspace.id)
        .filter(models.Lead.crm_status == "booked")
        .count()
    )

    return templates.TemplateResponse(
        request,
        "demo/inbox.html",
        {
            "settings": settings,
            "workspace": workspace,
            "current_user": current_user,
            "leads": leads,
            "selected_lead": selected_lead,
            "twilio_live": logic.twilio_enabled(),
            "recommended_response_time": logic.recommended_response_time,
            "crm_status_filter": crm_status_filter,
            "priority_filter": priority_filter,
            "insurance_filter": insurance_filter,
            "search": search,
            "upgrade_message": (limit_message or pilot_upgrade_message(upgrade_required)),
            "high_priority_count": high_priority_count,
            "qualified_count": qualified_count,
            "booked_count": booked_count,
            "selected_notes": parse_lead_notes(selected_lead) if selected_lead else [],
            "active_page": "inbox",
            "page_title": "Inbox",
            "page_subtitle": "All lead conversations in your workspace.",
        },
    )


@app.get("/app/pipeline", response_class=HTMLResponse)
def app_pipeline(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user_from_cookie(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    workspace = get_current_workspace(request, db)
    if not workspace:
        return RedirectResponse(url="/signup", status_code=303)
    if is_pilot_workspace(workspace):
        return RedirectResponse(
            url="/app/inbox?upgrade_required=pipeline_reporting",
            status_code=303,
        )

    settings = get_workspace_settings(db, workspace.id)

    leads = (
        db.query(models.Lead)
        .filter(models.Lead.workspace_id == workspace.id)
        .order_by(models.Lead.updated_at.desc())
        .all()
    )
    columns = build_pipeline_columns(leads)
    estimated_pipeline_value = calculate_pipeline_value(leads)

    return templates.TemplateResponse(
        request,
        "demo/pipeline.html",
        {
            "settings": settings,
            "workspace": workspace,
            "current_user": current_user,
            "columns": columns,
            "estimated_pipeline_value": estimated_pipeline_value,
            "twilio_live": logic.twilio_enabled(),
            "active_page": "pipeline",
            "page_title": "Pipeline",
            "page_subtitle": "Track leads from qualification to outcome.",
        },
    )


@app.get("/app/settings", response_class=HTMLResponse)
def app_settings(
    request: Request,
    billing_success: Optional[str] = Query(None),
    billing_error: Optional[str] = Query(None),
    number_success: Optional[str] = Query(None),
    number_error: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    current_user = get_current_user_from_cookie(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    workspace = get_current_workspace(request, db)
    if not workspace:
        return RedirectResponse(url="/signup", status_code=303)

    first_name, last_name = split_full_name(current_user.full_name)
    settings = get_workspace_settings(db, workspace.id)
    workflow_steps = get_settings_workflow_steps(db, current_user, settings.business_name)
    subscription_snapshot = get_subscription_snapshot(workspace)

    return templates.TemplateResponse(
        request,
        "demo/settings.html",
        {
            "settings": settings,
            "workspace": workspace,
            "current_user": current_user,
            "profile_first_name": first_name,
            "profile_last_name": last_name,
            "workflow_steps": workflow_steps,
            "subscription_snapshot": subscription_snapshot,
            "billing_success": billing_success,
            "billing_error": billing_error,
            "number_success": number_success,
            "number_error": number_error,
            "twilio_live": logic.twilio_enabled(),
            "active_page": "settings",
            "page_title": "Settings",
            "page_subtitle": "Manage business identity and workspace basics.",
        },
    )


@app.get("/app/lead/{lead_id}", response_class=HTMLResponse)
def app_lead_detail(request: Request, lead_id: int, db: Session = Depends(get_db)):
    current_user = get_current_user_from_cookie(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    workspace = get_current_workspace(request, db)
    if not workspace:
        return RedirectResponse(url="/signup", status_code=303)

    settings = get_workspace_settings(db, workspace.id)
    lead = (
        db.query(models.Lead)
        .filter(models.Lead.workspace_id == workspace.id)
        .filter(models.Lead.id == lead_id)
        .first()
    )

    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    activity_log = build_activity_log(lead)
    stage_options = [
        ("new", "New"),
        ("qualified", "Qualified"),
        ("contacted", "Contacted"),
        ("booked", "Estimate Scheduled"),
        ("closed", "Won"),
        ("lost", "Lost"),
    ]

    return templates.TemplateResponse(
        request,
        "lead_detail.html",
        {
            "settings": settings,
            "workspace": workspace,
            "current_user": current_user,
            "lead": lead,
            "activity_log": activity_log,
            "stage_options": stage_options,
            "twilio_live": logic.twilio_enabled(),
            "recommended_response_time": logic.recommended_response_time,
            "active_page": "pipeline",
            "page_title": "Lead Record",
            "page_subtitle": "Review the full lead lifecycle and update next actions.",
        },
    )


def split_full_name(full_name: str) -> tuple[str, str]:
    normalized = (full_name or "").strip()
    if not normalized:
        return "", ""
    parts = normalized.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def get_ui_context(request: Request, db: Session) -> tuple[Optional[models.AppUser], Optional[models.Workspace], str]:
    current_user = get_current_user_from_cookie(request, db)
    workspace = get_current_workspace(request, db) if current_user else None
    route_prefix = "/app" if workspace else "/demo"
    return current_user, workspace, route_prefix


def default_workflow_steps(business_name: str = "your roofing company") -> list[dict]:
    name = (business_name or "your roofing company").strip() or "your roofing company"
    return [
        {
            "id": "step-1",
            "title": "Instant Reply",
            "description": "First text sent immediately after a missed call.",
            "trigger": "missed_call",
            "delayMinutes": 0,
            "message": f"Hey, thanks for calling {name}. Sorry we missed you — are you looking for a repair, replacement, or inspection?",
        },
        {
            "id": "step-2",
            "title": "Ask for Postal Code",
            "description": "Capture service area and route the lead properly.",
            "trigger": "after_reply",
            "delayMinutes": 0,
            "message": "Thanks — what’s the postal code for the property?",
        },
        {
            "id": "step-3",
            "title": "Ask About the Job",
            "description": "Understand what kind of roofing help they need.",
            "trigger": "after_reply",
            "delayMinutes": 0,
            "message": "Got it. Is this for a repair, replacement, leak, storm damage, or inspection?",
        },
        {
            "id": "step-4",
            "title": "Ask About Urgency",
            "description": "Identify emergencies and high-priority jobs.",
            "trigger": "after_reply",
            "delayMinutes": 0,
            "message": "How urgent is this — emergency, soon, or just getting quotes?",
        },
        {
            "id": "step-5",
            "title": "Final Handoff",
            "description": "Let the customer know your team will follow up.",
            "trigger": "after_reply",
            "delayMinutes": 0,
            "message": f"Thanks — your request has been captured for {name}. A roofing specialist will follow up shortly to schedule an inspection or estimate.",
        },
    ]


def normalize_workflow_steps(raw_steps, business_name: str = "your roofing company") -> list[dict]:
    fallback = default_workflow_steps(business_name)
    if not isinstance(raw_steps, list) or not raw_steps:
        return fallback

    normalized_steps = []
    for index, step in enumerate(raw_steps):
        if not isinstance(step, dict):
            continue
        fallback_step = fallback[min(index, len(fallback) - 1)]
        normalized_steps.append(
            {
                "id": str(step.get("id") or f"step-{index + 1}"),
                "title": str(step.get("title") or fallback_step["title"]),
                "description": str(step.get("description") or fallback_step["description"]),
                "trigger": "missed_call" if step.get("trigger") == "missed_call" else "after_reply",
                "delayMinutes": max(0, int(step.get("delayMinutes") or 0)),
                "message": str(step.get("message") or ""),
            }
        )

    return normalized_steps or fallback


def get_settings_workflow_steps(db: Session, user: Optional[models.AppUser], business_name: str) -> list[dict]:
    if not user:
        return default_workflow_steps(business_name)

    progress = (
        db.query(models.OnboardingProgress)
        .filter(models.OnboardingProgress.user_id == user.id)
        .first()
    )
    if not progress or not progress.workflow_data:
        return default_workflow_steps(business_name)

    raw_steps = progress.workflow_data.get("steps_json")
    if isinstance(raw_steps, str):
        try:
            raw_steps = json.loads(raw_steps)
        except json.JSONDecodeError:
            raw_steps = []

    return normalize_workflow_steps(raw_steps, business_name)


def render_app_settings_template(
    request: Request,
    db: Session,
    current_user: models.AppUser,
    workspace: models.Workspace,
    *,
    account_error: Optional[str] = None,
    account_success: Optional[str] = None,
    password_error: Optional[str] = None,
    password_success: Optional[str] = None,
    profile_first_name: Optional[str] = None,
    profile_last_name: Optional[str] = None,
    profile_email: Optional[str] = None,
    workflow_steps: Optional[list[dict]] = None,
    billing_error: Optional[str] = None,
    billing_success: Optional[str] = None,
    number_error: Optional[str] = None,
    number_success: Optional[str] = None,
):
    settings = get_workspace_settings(db, workspace.id)
    first_name, last_name = split_full_name(current_user.full_name)
    effective_workflow_steps = workflow_steps or get_settings_workflow_steps(db, current_user, settings.business_name)
    subscription_snapshot = get_subscription_snapshot(workspace)
    return templates.TemplateResponse(
        request,
        "demo/settings.html",
        {
            "settings": settings,
            "workspace": workspace,
            "current_user": current_user,
            "profile_first_name": profile_first_name if profile_first_name is not None else first_name,
            "profile_last_name": profile_last_name if profile_last_name is not None else last_name,
            "profile_email": profile_email if profile_email is not None else (current_user.email or ""),
            "account_error": account_error,
            "account_success": account_success,
            "password_error": password_error,
            "password_success": password_success,
            "workflow_steps": effective_workflow_steps,
            "subscription_snapshot": subscription_snapshot,
            "billing_error": billing_error,
            "billing_success": billing_success,
            "number_error": number_error,
            "number_success": number_success,
            "twilio_live": logic.twilio_enabled(),
            "active_page": "settings",
            "page_title": "Settings",
            "page_subtitle": "Manage business identity and workspace basics.",
        },
    )


def parse_lead_notes(lead: models.Lead) -> list[dict]:
    raw = (lead.notes or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            normalized = []
            for item in parsed:
                if isinstance(item, dict) and str(item.get("text", "")).strip():
                    normalized.append(
                        {
                            "id": str(item.get("id") or secrets.token_hex(6)),
                            "text": str(item.get("text", "")).strip(),
                            "created_at": str(item.get("created_at") or datetime.utcnow().isoformat()),
                        }
                    )
            return normalized
    except json.JSONDecodeError:
        pass
    return [{"id": secrets.token_hex(6), "text": raw, "created_at": datetime.utcnow().isoformat()}]


def save_lead_notes(lead: models.Lead, notes_list: list[dict]) -> None:
    lead.notes = json.dumps(notes_list)
    lead.updated_at = datetime.utcnow()


# --------------------------------------------------
# DEMO UI FORM ACTIONS
# --------------------------------------------------

@app.post("/ui/settings/update")
def ui_update_settings(
    request: Request,
    business_name: str = Form(...),
    first_message: str = Form(""),
    notification_email: str = Form(""),
    team_mobile: str = Form(""),
    phone_mode: str = Form("existing"),
    coverage_mode: str = Form("always"),
    workday_start: str = Form(""),
    workday_end: str = Form(""),
    business_days: str = Form(""),
    workflow_steps_json: str = Form("[]"),
    db: Session = Depends(get_db),
):
    current_user = get_current_user_from_cookie(request, db)
    workspace = get_current_workspace(request, db)
    settings = get_workspace_settings(db, workspace.id) if workspace else logic.get_or_create_business_settings(db)
    settings.business_name = (business_name or "").strip() or settings.business_name
    settings.first_message = (first_message or "").strip() or settings.first_message

    if workspace:
        workspace.notification_email = current_user.email if current_user else workspace.notification_email
        workspace.team_mobile = (team_mobile or "").strip() or None
        workspace.phone_mode = (phone_mode or "existing").strip()
        workspace.coverage_mode = (coverage_mode or "always").strip()

        if workspace.coverage_mode == "after_hours":
            workspace.workday_start = (workday_start or "").strip() or None
            workspace.workday_end = (workday_end or "").strip() or None
            workspace.business_days = (business_days or "").strip() or None
        else:
            workspace.workday_start = None
            workspace.workday_end = None
            workspace.business_days = None

    parsed_workflow_steps = []
    try:
        parsed_workflow_steps = json.loads(workflow_steps_json or "[]")
    except json.JSONDecodeError:
        parsed_workflow_steps = []

    normalized_steps = normalize_workflow_steps(parsed_workflow_steps, settings.business_name)
    if normalized_steps:
        settings.first_message = normalized_steps[0]["message"] or settings.first_message

    if current_user:
        save_workflow_step(db, current_user.id, {"steps_json": normalized_steps})

    db.commit()
    redirect_url = "/app/settings" if workspace else "/demo/settings"
    return RedirectResponse(url=redirect_url, status_code=303)


@app.post("/ui/settings/phone-number/change")
def ui_change_service_number(
    request: Request,
    selected_twilio_number: str = Form(""),
    confirm_new_number_fee: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    current_user = get_current_user_from_cookie(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    workspace = get_current_workspace(request, db)
    if not workspace:
        return RedirectResponse(url="/signup", status_code=303)

    if confirm_new_number_fee != "yes":
        return RedirectResponse(
            url="/app/settings?number_error=Please+confirm+the+number+change+charge+before+continuing.",
            status_code=303,
        )

    normalized_number = (selected_twilio_number or "").strip()
    if not normalized_number:
        return RedirectResponse(
            url="/app/settings?number_error=Please+choose+a+new+service+number+first.",
            status_code=303,
        )

    old_number = workspace.business_phone or workspace.active_twilio_number or "Not set"
    workspace.phone_mode = "new"
    workspace.pending_twilio_number = None
    workspace.active_twilio_number = normalized_number
    workspace.business_phone = normalized_number

    db.commit()

    return RedirectResponse(
        url=(
            "/app/settings?number_success="
            + quote_plus(
                f"Service number updated from {old_number} to {normalized_number}. "
                "Your old number has been removed."
            )
        ),
        status_code=303,
    )


@app.post("/ui/settings/account")
def ui_update_account(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(""),
    email: str = Form(...),
    email_otp: str = Form(""),
    db: Session = Depends(get_db),
):
    current_user = get_current_user_from_cookie(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    workspace = get_current_workspace(request, db)
    if not workspace:
        return RedirectResponse(url="/signup", status_code=303)

    normalized_first_name = (first_name or "").strip()
    normalized_last_name = (last_name or "").strip()
    normalized_email = (email or "").strip().lower()
    normalized_otp = (email_otp or "").strip()

    if not normalized_first_name:
        return render_app_settings_template(
            request,
            db,
            current_user,
            workspace,
            account_error="First name is required.",
            profile_first_name=normalized_first_name,
            profile_last_name=normalized_last_name,
            profile_email=normalized_email,
        )

    if not normalized_email:
        return render_app_settings_template(
            request,
            db,
            current_user,
            workspace,
            account_error="Email is required.",
            profile_first_name=normalized_first_name,
            profile_last_name=normalized_last_name,
            profile_email=normalized_email,
        )

    existing_user = (
        db.query(models.AppUser)
        .filter(models.AppUser.email == normalized_email, models.AppUser.id != current_user.id)
        .first()
    )
    if existing_user:
        return render_app_settings_template(
            request,
            db,
            current_user,
            workspace,
            account_error="That email is already used by another account.",
            profile_first_name=normalized_first_name,
            profile_last_name=normalized_last_name,
            profile_email=normalized_email,
        )

    current_email = (current_user.email or "").strip().lower()

    if normalized_email != current_email:
        if not normalized_otp:
            code = generate_reset_code()
            code_hash = hash_reset_code(code)
            expires_at = datetime.utcnow() + timedelta(minutes=EMAIL_CHANGE_CODE_MINUTES)

            (
                db.query(models.EmailChangeCode)
                .filter(
                    models.EmailChangeCode.user_id == current_user.id,
                    models.EmailChangeCode.new_email == normalized_email,
                    models.EmailChangeCode.used_at.is_(None),
                )
                .delete(synchronize_session=False)
            )

            pending_codes = (
                db.query(models.EmailChangeCode)
                .filter(
                    models.EmailChangeCode.user_id == current_user.id,
                    models.EmailChangeCode.used_at.is_(None),
                )
                .all()
            )
            for pending in pending_codes:
                pending.used_at = datetime.utcnow()

            db.add(
                models.EmailChangeCode(
                    user_id=current_user.id,
                    new_email=normalized_email,
                    code_hash=code_hash,
                    expires_at=expires_at,
                )
            )
            db.commit()

            sent_to_email = send_email_change_code(normalized_email, code)
            preview_code = None if sent_to_email else code
            info_message = "We sent a one-time code to your new email. Enter it below to confirm this change."
            if not sent_to_email:
                info_message = "Email delivery is not configured yet. Use the temporary code below to confirm this change."
                info_message = f"{info_message} Code: {preview_code}"

            return render_app_settings_template(
                request,
                db,
                current_user,
                workspace,
                account_success=info_message,
                profile_first_name=normalized_first_name,
                profile_last_name=normalized_last_name,
                profile_email=normalized_email,
            )

        email_change = (
            db.query(models.EmailChangeCode)
            .filter(
                models.EmailChangeCode.user_id == current_user.id,
                models.EmailChangeCode.new_email == normalized_email,
                models.EmailChangeCode.used_at.is_(None),
                models.EmailChangeCode.expires_at >= datetime.utcnow(),
            )
            .order_by(models.EmailChangeCode.created_at.desc())
            .first()
        )

        if not email_change or hash_reset_code(normalized_otp) != email_change.code_hash:
            return render_app_settings_template(
                request,
                db,
                current_user,
                workspace,
                account_error="Invalid or expired verification code for the new email.",
                profile_first_name=normalized_first_name,
                profile_last_name=normalized_last_name,
                profile_email=normalized_email,
            )

        email_change.used_at = datetime.utcnow()

    current_user.full_name = f"{normalized_first_name} {normalized_last_name}".strip()
    current_user.email = normalized_email
    db.commit()

    return render_app_settings_template(
        request,
        db,
        current_user,
        workspace,
        account_success="Profile updated successfully.",
    )


@app.post("/ui/settings/password")
def ui_update_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_new_password: str = Form(...),
    db: Session = Depends(get_db),
):
    current_user = get_current_user_from_cookie(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    workspace = get_current_workspace(request, db)
    if not workspace:
        return RedirectResponse(url="/signup", status_code=303)

    if not verify_password(current_password, current_user.password_hash):
        return render_app_settings_template(
            request,
            db,
            current_user,
            workspace,
            password_error="Current password is incorrect.",
        )

    if new_password != confirm_new_password:
        return render_app_settings_template(
            request,
            db,
            current_user,
            workspace,
            password_error="New password and confirmation do not match.",
        )

    password_error = validate_password_rules(new_password)
    if password_error:
        return render_app_settings_template(
            request,
            db,
            current_user,
            workspace,
            password_error=password_error,
        )

    if verify_password(new_password, current_user.password_hash):
        return render_app_settings_template(
            request,
            db,
            current_user,
            workspace,
            password_error="New password must be different from your current password.",
        )

    current_user.password_hash = hash_password(new_password)
    db.commit()

    return render_app_settings_template(
        request,
        db,
        current_user,
        workspace,
        password_success="Password updated successfully.",
    )


@app.post("/ui/settings/billing/portal")
def ui_settings_billing_portal(
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = get_current_user_from_cookie(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    workspace = get_current_workspace(request, db)
    if not workspace:
        return RedirectResponse(url="/signup", status_code=303)

    if not stripe.api_key:
        return RedirectResponse(
            url="/app/settings?billing_error=Stripe+is+not+configured+yet.",
            status_code=303,
        )

    if not workspace.stripe_customer_id:
        return RedirectResponse(
            url="/app/settings?billing_error=No+Stripe+customer+is+linked+to+this+workspace.",
            status_code=303,
        )

    try:
        session = stripe.billing_portal.Session.create(
            customer=workspace.stripe_customer_id,
            return_url=f"{APP_BASE_URL}/app/settings",
        )
        return RedirectResponse(url=session.url, status_code=303)
    except Exception:
        return RedirectResponse(
            url="/app/settings?billing_error=We+could+not+open+the+billing+portal.+Please+try+again.",
            status_code=303,
        )


@app.post("/ui/settings/billing/cancel")
def ui_settings_cancel_membership(
    request: Request,
    confirm_text: str = Form(""),
    acknowledge_end_of_cycle: str = Form(""),
    db: Session = Depends(get_db),
):
    current_user = get_current_user_from_cookie(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    workspace = get_current_workspace(request, db)
    if not workspace:
        return RedirectResponse(url="/signup", status_code=303)

    if (confirm_text or "").strip().upper() != "CANCEL":
        return render_app_settings_template(
            request,
            db,
            current_user,
            workspace,
            billing_error='Type "CANCEL" exactly to confirm cancellation.',
        )

    if acknowledge_end_of_cycle != "yes":
        return render_app_settings_template(
            request,
            db,
            current_user,
            workspace,
            billing_error="Please confirm that cancellation takes effect at the end of your current billing cycle.",
        )

    if not stripe.api_key or not workspace.stripe_subscription_id:
        return render_app_settings_template(
            request,
            db,
            current_user,
            workspace,
            billing_error="No active Stripe subscription was found for this workspace.",
        )

    try:
        stripe.Subscription.modify(
            workspace.stripe_subscription_id,
            cancel_at_period_end=True,
        )
        workspace.status = "cancel_at_period_end"
        db.commit()
    except Exception:
        return render_app_settings_template(
            request,
            db,
            current_user,
            workspace,
            billing_error="We couldn't schedule cancellation right now. Please try again in a minute.",
        )

    return RedirectResponse(
        url="/app/settings?billing_success=Cancellation+scheduled.+Your+subscription+remains+active+until+the+end+of+the+current+billing+cycle.",
        status_code=303,
    )


@app.post("/ui/leads/create")
def ui_create_lead(
    request: Request,
    phone_number: str = Form(...),
    db: Session = Depends(get_db),
):
    _, workspace, route_prefix = get_ui_context(request, db)
    normalized_phone = (phone_number or "").strip()
    latest_query = db.query(models.Lead).filter(models.Lead.phone_number == normalized_phone)
    if workspace:
        latest_query = latest_query.filter(models.Lead.workspace_id == workspace.id)
    latest = latest_query.order_by(models.Lead.created_at.desc()).first()
    if latest and not logic.is_finished_lead(latest):
        return RedirectResponse(url=f"{route_prefix}/inbox?lead_id={latest.id}", status_code=303)

    try:
        if workspace:
            logic.enforce_workspace_recovered_lead_limit(db, workspace.id)
            lead = models.Lead(
                workspace_id=workspace.id,
                phone_number=normalized_phone,
                source="manual",
            )
            db.add(lead)
            db.commit()
            db.refresh(lead)
        else:
            lead = logic.create_missed_call_lead(db, phone_number=normalized_phone)
    except ValueError as exc:
        return RedirectResponse(
            url=f"{route_prefix}/inbox?upgrade_required=lead_limit&limit_message={quote_plus(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(url=f"{route_prefix}/inbox?lead_id={lead.id}", status_code=303)


@app.post("/ui/messages/send")
def ui_send_message(
    request: Request,
    lead_id: int = Form(...),
    body: str = Form(...),
    crm_status_filter: str = Form("all"),
    priority_filter: str = Form("all"),
    insurance_filter: str = Form("all"),
    search: str = Form(""),
    db: Session = Depends(get_db),
):
    _, workspace, route_prefix = get_ui_context(request, db)
    lead_query = db.query(models.Lead).filter(models.Lead.id == lead_id)
    if workspace:
        lead_query = lead_query.filter(models.Lead.workspace_id == workspace.id)
    lead = lead_query.first()
    if not lead:
        return RedirectResponse(url=f"{route_prefix}/inbox", status_code=303)

    logic.process_inbound_message(db, lead, body)

    return RedirectResponse(
        url=(
            f"{route_prefix}/inbox?lead_id={lead_id}"
            f"&crm_status_filter={crm_status_filter}"
            f"&priority_filter={priority_filter}"
            f"&insurance_filter={insurance_filter}"
            f"&search={search}"
        ),
        status_code=303,
    )


@app.post("/ui/leads/update-status")
def ui_update_crm_status(
    request: Request,
    lead_id: int = Form(...),
    crm_status: str = Form(...),
    crm_status_filter: str = Form("all"),
    priority_filter: str = Form("all"),
    insurance_filter: str = Form("all"),
    search: str = Form(""),
    db: Session = Depends(get_db),
):
    _, workspace, route_prefix = get_ui_context(request, db)
    lead_query = db.query(models.Lead).filter(models.Lead.id == lead_id)
    if workspace:
        lead_query = lead_query.filter(models.Lead.workspace_id == workspace.id)
    lead = lead_query.first()
    if not lead:
        return RedirectResponse(url=f"{route_prefix}/inbox", status_code=303)

    logic.update_crm_status(db, lead, crm_status)

    return RedirectResponse(
        url=(
            f"{route_prefix}/inbox?lead_id={lead_id}"
            f"&crm_status_filter={crm_status_filter}"
            f"&priority_filter={priority_filter}"
            f"&insurance_filter={insurance_filter}"
            f"&search={search}"
        ),
        status_code=303,
    )


@app.post("/ui/leads/update-notes")
def ui_update_lead_notes(
    request: Request,
    lead_id: int = Form(...),
    notes: str = Form(...),
    crm_status_filter: str = Form(""),
    priority_filter: str = Form("all"),
    insurance_filter: str = Form("all"),
    search: str = Form(""),
    db: Session = Depends(get_db),
):
    _, workspace, route_prefix = get_ui_context(request, db)
    lead_query = db.query(models.Lead).filter(models.Lead.id == lead_id)
    if workspace:
        lead_query = lead_query.filter(models.Lead.workspace_id == workspace.id)
    lead = lead_query.first()

    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    new_note_text = (notes or "").strip()
    existing_notes = parse_lead_notes(lead)
    if new_note_text:
        existing_notes.insert(
            0,
            {
                "id": secrets.token_hex(6),
                "text": new_note_text,
                "created_at": datetime.utcnow().isoformat(),
            },
        )
    save_lead_notes(lead, existing_notes)
    db.commit()

    if crm_status_filter != "":
        return RedirectResponse(
            url=(
                f"{route_prefix}/inbox?lead_id={lead_id}"
                f"&crm_status_filter={crm_status_filter}"
                f"&priority_filter={priority_filter}"
                f"&insurance_filter={insurance_filter}"
                f"&search={search}"
            ),
            status_code=303,
        )

    return RedirectResponse(url=f"{route_prefix}/lead/{lead_id}", status_code=303)


@app.post("/ui/leads/delete-note")
def ui_delete_lead_note(
    request: Request,
    lead_id: int = Form(...),
    note_id: str = Form(...),
    crm_status_filter: str = Form(""),
    priority_filter: str = Form("all"),
    insurance_filter: str = Form("all"),
    search: str = Form(""),
    db: Session = Depends(get_db),
):
    _, workspace, route_prefix = get_ui_context(request, db)
    lead_query = db.query(models.Lead).filter(models.Lead.id == lead_id)
    if workspace:
        lead_query = lead_query.filter(models.Lead.workspace_id == workspace.id)
    lead = lead_query.first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    remaining = [note for note in parse_lead_notes(lead) if note.get("id") != note_id]
    save_lead_notes(lead, remaining)
    db.commit()

    if crm_status_filter != "":
        return RedirectResponse(
            url=(
                f"{route_prefix}/inbox?lead_id={lead_id}"
                f"&crm_status_filter={crm_status_filter}"
                f"&priority_filter={priority_filter}"
                f"&insurance_filter={insurance_filter}"
                f"&search={search}"
            ),
            status_code=303,
        )
    return RedirectResponse(url=f"{route_prefix}/lead/{lead_id}", status_code=303)


@app.post("/ui/leads/update-stage")
def ui_update_lead_stage(
    request: Request,
    lead_id: int = Form(...),
    crm_status: str = Form(...),
    return_to: str = Form(""),
    inbox_path: str = Form("/demo/inbox"),
    crm_status_filter: str = Form("all"),
    priority_filter: str = Form("all"),
    insurance_filter: str = Form("all"),
    search: str = Form(""),
    db: Session = Depends(get_db),
):
    _, workspace, route_prefix = get_ui_context(request, db)
    lead_query = db.query(models.Lead).filter(models.Lead.id == lead_id)
    if workspace:
        lead_query = lead_query.filter(models.Lead.workspace_id == workspace.id)
    lead = lead_query.first()

    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    allowed_statuses = {"new", "qualified", "contacted", "booked", "closed", "lost"}
    if crm_status not in allowed_statuses:
        raise HTTPException(status_code=400, detail="Invalid crm_status")

    if (
        inbox_path == "/app/inbox"
        and is_pilot_workspace(lead.workspace)
        and crm_status in PILOT_BLOCKED_STAGE_UPDATES
    ):
        return RedirectResponse(
            url=(
                f"/app/inbox?lead_id={lead_id}"
                f"&crm_status_filter={crm_status_filter}"
                f"&priority_filter={priority_filter}"
                f"&insurance_filter={insurance_filter}"
                f"&search={search}"
                "&upgrade_required=advanced_stage_management"
            ),
            status_code=303,
        )

    previous_crm_status = lead.crm_status

    if crm_status == "new":
        lead.status = "new"
        lead.crm_status = "new"
    elif crm_status == "qualified":
        lead.status = "qualified"
        lead.crm_status = "new"
    else:
        lead.crm_status = crm_status
        if crm_status in ["contacted", "booked", "closed", "lost"]:
            lead.status = "qualified"

    if crm_status == "booked" and previous_crm_status != "booked":
        booking_message = "Great — your estimate request is now marked as scheduled. We’ll follow up with your confirmed appointment day and arrival window."
        latest_message = (
            db.query(models.Message)
            .filter(models.Message.lead_id == lead.id)
            .order_by(models.Message.created_at.desc())
            .first()
        )
        if not latest_message or latest_message.body != booking_message:
            db.add(
                models.Message(
                    workspace_id=lead.workspace_id,
                    lead_id=lead.id,
                    direction="outbound",
                    body=booking_message,
                )
            )

    db.commit()

    if return_to == "inbox":
        safe_inbox_path = inbox_path if inbox_path in {"/demo/inbox", "/app/inbox"} else f"{route_prefix}/inbox"
        return RedirectResponse(
            url=(
                f"{safe_inbox_path}?lead_id={lead_id}"
                f"&crm_status_filter={crm_status_filter}"
                f"&priority_filter={priority_filter}"
                f"&insurance_filter={insurance_filter}"
                f"&search={search}"
            ),
            status_code=303,
        )

    return RedirectResponse(url=f"{route_prefix}/lead/{lead_id}", status_code=303)


# --------------------------------------------------
# API
# --------------------------------------------------

@app.get("/api/settings", response_model=schemas.BusinessSettingsOut)
def api_get_settings(db: Session = Depends(get_db)):
    return logic.get_or_create_business_settings(db)


@app.post("/api/settings", response_model=schemas.BusinessSettingsOut)
def api_update_settings(
    payload: schemas.BusinessSettingsUpdate,
    db: Session = Depends(get_db),
):
    return logic.update_business_name(db, payload.business_name)


@app.post("/api/leads/missed-call", response_model=schemas.LeadOut)
def api_create_missed_call_lead(payload: schemas.LeadCreate, db: Session = Depends(get_db)):
    try:
        return logic.create_missed_call_lead(
            db,
            phone_number=payload.phone_number,
            source=payload.source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@app.get("/api/leads", response_model=list[schemas.LeadOut])
def api_list_leads(db: Session = Depends(get_db)):
    return db.query(models.Lead).order_by(models.Lead.created_at.desc()).all()


@app.get("/api/leads/{lead_id}", response_model=schemas.LeadOut)
def api_get_lead(lead_id: int, db: Session = Depends(get_db)):
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


@app.post("/api/messages/inbound")
def api_inbound_message(payload: schemas.InboundMessageCreate, db: Session = Depends(get_db)):
    lead = db.query(models.Lead).filter(models.Lead.id == payload.lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    reply = logic.process_inbound_message(db, lead, payload.body)

    return {
        "lead_id": lead.id,
        "status": lead.status,
        "conversation_state": lead.conversation_state,
        "priority": lead.priority,
        "reply": reply,
    }


# --------------------------------------------------
# TWILIO WEBHOOK
# --------------------------------------------------

@app.post("/webhooks/twilio/inbound", response_class=HTMLResponse)
def twilio_inbound(
    From: str = Form(...),
    Body: str = Form(...),
    db: Session = Depends(get_db),
):
    # Demo-only for now
    text = logic.normalize_text(Body)
    latest_lead = logic.find_latest_lead_for_phone(db, From)

    if logic.should_start_new_lead(latest_lead, Body):
        try:
            lead = logic.create_missed_call_lead(db, phone_number=From, source="sms_inbound")
        except ValueError as exc:
            resp = MessagingResponse()
            resp.message(str(exc))
            return HTMLResponse(content=str(resp), media_type="application/xml")

        if text in logic.RESTART_KEYWORDS:
            reply = "Got it — let’s start a new request. Are you looking for a repair, replacement, or inspection?"
        elif text in logic.VALID_JOB_TYPES:
            reply = logic.process_inbound_message(db, lead, Body, send_outbound_sms=False)
        else:
            reply = "Are you looking for a repair, replacement, or inspection?"
            logic.create_message(db, lead.id, "inbound", Body)
            logic.create_message(db, lead.id, "outbound", reply)
            db.commit()
    else:
        lead = latest_lead
        reply = logic.process_inbound_message(db, lead, Body, send_outbound_sms=False)

    resp = MessagingResponse()
    resp.message(reply)
    return HTMLResponse(content=str(resp), media_type="application/xml")

def validate_password_rules(password: str) -> Optional[str]:
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if not re.search(r"[A-Z]", password):
        return "Password must include at least one uppercase letter."
    if not re.search(r"[a-z]", password):
        return "Password must include at least one lowercase letter."
    if not re.search(r"[0-9]", password):
        return "Password must include at least one number."
    if not re.search(r"[^A-Za-z0-9]", password):
        return "Password must include at least one special character."
    return None

@app.get("/api/auth/check-email")
def check_email_exists(
    email: str = Query(...),
    db: Session = Depends(get_db),
):
    normalized_email = email.strip().lower()

    if not normalized_email:
        return {"exists": False}

    existing_user = (
        db.query(models.AppUser)
        .filter(models.AppUser.email == normalized_email)
        .first()
    )

    return {"exists": existing_user is not None}
@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    return templates.TemplateResponse(
        request,
        "terms.html",
        {"page_title": "Terms of Service"},
    )


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request):
    return templates.TemplateResponse(
        request,
        "privacy.html",
        {"page_title": "Privacy Policy"},
    )
    
from typing import List, Dict
from fastapi import Query, HTTPException
from twilio.rest import Client

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()

def get_twilio_client() -> Client:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise HTTPException(status_code=500, detail="Twilio credentials are missing.")
    return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def provision_twilio_number(number: str):
    normalized_number = (number or "").strip()
    if not normalized_number:
        raise HTTPException(status_code=400, detail="No Twilio number was selected.")

    client = get_twilio_client()
    existing = client.incoming_phone_numbers.list(phone_number=normalized_number, limit=1)
    if existing:
        return existing[0]

    sms_webhook_url = f"{APP_BASE_URL}/webhooks/twilio/inbound"
    return client.incoming_phone_numbers.create(
        phone_number=normalized_number,
        sms_url=sms_webhook_url,
        sms_method="POST",
    )


CITY_AREA_CODE_MAP: Dict[str, List[str]] = {
    "calgary": ["403", "587", "825", "368"],
    "edmonton": ["780", "587", "825", "368"],
    "red deer": ["403", "587", "825", "368"],
    "lethbridge": ["403", "587", "825", "368"],
    "medicine hat": ["403", "587", "825", "368"],
}

PROVINCE_AREA_CODE_MAP: Dict[str, List[str]] = {
    "AB": ["403", "587", "780", "825", "368"],
    "BC": ["236", "250", "604", "672", "778"],
    "SK": ["306", "639"],
    "MB": ["204", "431"],
    "ON": ["226", "249", "289", "343", "365", "416", "437", "519", "548", "613", "647", "705", "742", "807", "905"],
    "QC": ["263", "354", "367", "418", "438", "450", "468", "514", "579", "581", "819", "873"],
}


@app.get("/api/twilio/available-numbers")
def api_twilio_available_numbers():
    print("DEBUG: /api/twilio/available-numbers route hit")

    client = get_twilio_client()

    try:
        numbers = client.available_phone_numbers("CA").local.list(limit=10)
        result = [n.phone_number for n in numbers]

        print("DEBUG: Twilio numbers returned:", result)

        return {
            "numbers": result
        }
    except Exception as exc:
        print("DEBUG: Twilio lookup failed:", str(exc))
        raise HTTPException(status_code=500, detail=f"Twilio lookup failed: {exc}")

def unique_phone_numbers(numbers) -> List[str]:
    seen = set()
    result = []
    for n in numbers:
        phone = getattr(n, "phone_number", None)
        if not phone:
            continue
        if phone in seen:
            continue
        seen.add(phone)
        result.append(phone)
    return result

def try_twilio_local_search(client: Client, **kwargs) -> List[str]:
    try:
        numbers = client.available_phone_numbers("CA").local.list(limit=10, **kwargs)
        return unique_phone_numbers(numbers)
    except Exception:
        return []

@app.get("/onboarding/business")
def onboarding_business_page(plan: str = Query("pilot")):
    return RedirectResponse(url=f"/onboarding/workflow?plan={plan}", status_code=303)


@app.post("/onboarding/business")
def onboarding_business_submit(
    request: Request,
    plan: str = Form("pilot"),
    company_name: str = Form(""),
    city: str = Form(""),
    province: str = Form(""),
    industry: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_current_user(request, db)
    workspace = get_current_workspace(request, db)

    business_data = {
        "company_name": company_name.strip(),
        "city": city.strip(),
        "province": province.strip(),
        "industry": industry.strip(),
    }

    save_business_step(db, user.id, business_data)

    if workspace:
        workspace.company_name = company_name.strip()
        workspace.primary_service_area = f"{city.strip()}, {province.strip()}"
        db.commit()

    return RedirectResponse(
        url=f"/onboarding/workflow?plan={plan}",
        status_code=303,
    )

@app.get("/onboarding/workflow", response_class=HTMLResponse)
def onboarding_workflow_page(
    request: Request,
    plan: str = Query("pilot"),
    db: Session = Depends(get_db),
):
    user = require_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    progress = get_or_create_onboarding_progress(db, user.id)

    if not progress.business_data:
        return RedirectResponse(url=f"/signup?plan={plan}", status_code=303)

    return templates.TemplateResponse(
        request,
        "onboarding/workflow.html",
        {
            "page_title": "Workflow Preferences",
            "plan": plan.lower(),
            "signup_data": progress.business_data,
            "workflow_data": progress.workflow_data or {},
            "business_name": progress.business_data.get("company_name", "your roofing company"),
        },
    )

@app.get("/onboarding/phone-setup", response_class=HTMLResponse)
def onboarding_phone_setup_page(
    request: Request,
    plan: str = Query("pilot"),
    db: Session = Depends(get_db),
):
    user = require_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    progress = get_or_create_onboarding_progress(db, user.id)

    if not progress.business_data:
        return RedirectResponse(url=f"/onboarding/business?plan={plan}", status_code=303)
    if not progress.workflow_data:
        return RedirectResponse(url=f"/onboarding/workflow?plan={plan}", status_code=303)

    phone_setup_data = {
        "phone_mode": "existing",
        "business_phone": "",
        "selected_twilio_number": "",
        "coverage_mode": "always",
        "workday_start": "",
        "workday_end": "",
        "business_days": "",
        "notification_email": user.email or "",
        "team_mobile": "",
    }

    if progress.phone_setup_data:
        phone_setup_data.update(progress.phone_setup_data)

    signup_data = progress.business_data

    return templates.TemplateResponse(
        request,
        "onboarding/phone_setup.html",
        {
            "page_title": "Phone Setup",
            "plan": plan.lower(),
            "signup_data": signup_data,
            "signup_city": signup_data.get("city", ""),
            "signup_province": signup_data.get("province", ""),
            "workflow_data": progress.workflow_data,
            "phone_setup_data": phone_setup_data,
        },
    )


@app.post("/onboarding/phone-setup")
def onboarding_phone_setup_submit(
    request: Request,
    plan: str = Form("pilot"),
    phone_mode: str = Form("existing"),
    business_phone: str = Form(""),
    selected_twilio_number: str = Form(""),
    coverage_mode: str = Form("always"),
    workday_start: str = Form(""),
    workday_end: str = Form(""),
    business_days: str = Form(""),
    notification_email: str = Form(""),
    team_mobile: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_current_user(request, db)
    workspace = get_current_workspace(request, db)
    progress = get_or_create_onboarding_progress(db, user.id)

    if not progress.business_data:
        return RedirectResponse(url=f"/onboarding/business?plan={plan}", status_code=303)
    if not progress.workflow_data:
        return RedirectResponse(url=f"/onboarding/workflow?plan={plan}", status_code=303)

    phone_setup_data = {
        "phone_mode": (phone_mode or "existing").strip(),
        "business_phone": (business_phone or "").strip(),
        "selected_twilio_number": (selected_twilio_number or "").strip(),
        "coverage_mode": (coverage_mode or "always").strip(),
        "workday_start": (workday_start or "").strip(),
        "workday_end": (workday_end or "").strip(),
        "business_days": (business_days or "").strip(),
        "notification_email": (notification_email or "").strip(),
        "team_mobile": (team_mobile or "").strip(),
    }

    save_phone_step(db, user.id, phone_setup_data)

    if workspace:
        workspace.phone_mode = phone_setup_data["phone_mode"]
        workspace.coverage_mode = phone_setup_data["coverage_mode"]

        if workspace.phone_mode == "existing":
            workspace.business_phone = phone_setup_data["business_phone"] or None
            workspace.pending_twilio_number = None
            workspace.active_twilio_number = None
        else:
            selected_number = phone_setup_data["selected_twilio_number"] or None
            if selected_number:
                try:
                    purchased_number = provision_twilio_number(selected_number)
                    workspace.active_twilio_number = getattr(purchased_number, "phone_number", selected_number)
                    workspace.pending_twilio_number = None
                    workspace.business_phone = workspace.active_twilio_number
                except Exception as exc:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Could not purchase the selected number: {exc}",
                    )
            else:
                workspace.business_phone = None
                workspace.active_twilio_number = None
                workspace.pending_twilio_number = None

        if workspace.coverage_mode == "after_hours":
            workspace.workday_start = phone_setup_data["workday_start"] or None
            workspace.workday_end = phone_setup_data["workday_end"] or None
            workspace.business_days = phone_setup_data["business_days"] or None
        else:
            workspace.workday_start = None
            workspace.workday_end = None
            workspace.business_days = None

        workspace.notification_email = phone_setup_data["notification_email"] or None
        workspace.team_mobile = phone_setup_data["team_mobile"] or None
        db.commit()

    return RedirectResponse(
        url=f"/onboarding/review?plan={plan}",
        status_code=303,
    )

@app.post("/onboarding/review")
def onboarding_review_submit(
    request: Request,
    plan: str = Form("pilot"),
    db: Session = Depends(get_db),
):
    user = require_current_user(request, db)
    progress = get_or_create_onboarding_progress(db, user.id)

    if not progress.business_data:
        return RedirectResponse(url=f"/onboarding/business?plan={plan}", status_code=303)
    if not progress.workflow_data:
        return RedirectResponse(url=f"/onboarding/workflow?plan={plan}", status_code=303)
    if not progress.phone_setup_data:
        return RedirectResponse(url=f"/onboarding/phone-setup?plan={plan}", status_code=303)

    return RedirectResponse(url=f"/billing?plan={plan}", status_code=303)

def get_or_create_onboarding_progress(db: Session, user_id: int) -> models.OnboardingProgress:
    progress = (
        db.query(models.OnboardingProgress)
        .filter(models.OnboardingProgress.user_id == user_id)
        .first()
    )
    if not progress:
        progress = models.OnboardingProgress(user_id=user_id)
        db.add(progress)
        db.commit()
        db.refresh(progress)
    return progress


def save_business_step(db: Session, user_id: int, data: dict) -> models.OnboardingProgress:
    progress = get_or_create_onboarding_progress(db, user_id)
    progress.business_data = data
    progress.last_completed_step = max(progress.last_completed_step, 1)
    db.commit()
    db.refresh(progress)
    return progress


def save_workflow_step(db: Session, user_id: int, data: dict) -> models.OnboardingProgress:
    progress = get_or_create_onboarding_progress(db, user_id)
    progress.workflow_data = data
    progress.last_completed_step = max(progress.last_completed_step, 2)
    db.commit()
    db.refresh(progress)
    return progress


def save_phone_step(db: Session, user_id: int, data: dict) -> models.OnboardingProgress:
    progress = get_or_create_onboarding_progress(db, user_id)
    progress.phone_setup_data = data
    progress.last_completed_step = max(progress.last_completed_step, 3)
    db.commit()
    db.refresh(progress)
    return progress
    
def require_current_user(request: Request, db: Session) -> Optional[models.AppUser]:
    return get_current_user_from_cookie(request, db)

@app.post("/onboarding/workflow")
def onboarding_workflow_submit(
    request: Request,
    plan: str = Form("pilot"),
    steps_json: str = Form("[]"),
    db: Session = Depends(get_db),
):
    user = require_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    progress = get_or_create_onboarding_progress(db, user.id)

    if not progress.business_data:
        return RedirectResponse(url=f"/signup?plan={plan}", status_code=303)

    workflow_data = {
        "steps_json": steps_json,
    }

    save_workflow_step(db, user.id, workflow_data)

    return RedirectResponse(
        url=f"/onboarding/phone-setup?plan={plan}",
        status_code=303,
    )

@app.get("/onboarding/review", response_class=HTMLResponse)
def onboarding_review_page(
    request: Request,
    plan: str = Query("pilot"),
    db: Session = Depends(get_db),
):
    user = require_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    progress = get_or_create_onboarding_progress(db, user.id)

    if not progress.business_data:
        return RedirectResponse(url=f"/onboarding/business?plan={plan}", status_code=303)
    if not progress.workflow_data:
        return RedirectResponse(url=f"/onboarding/workflow?plan={plan}", status_code=303)
    if not progress.phone_setup_data:
        return RedirectResponse(url=f"/onboarding/phone-setup?plan={plan}", status_code=303)

    signup_data = progress.business_data
    phone_setup_data = dict(progress.phone_setup_data)

    day_map = {
        "mon": "Mon",
        "tue": "Tue",
        "wed": "Wed",
        "thu": "Thu",
        "fri": "Fri",
        "sat": "Sat",
        "sun": "Sun",
    }

    raw_days = phone_setup_data.get("business_days", "")
    phone_setup_data["business_days_display"] = ", ".join(
        day_map.get(day.strip(), day.strip().title())
        for day in raw_days.split(",")
        if day.strip()
    )

    return templates.TemplateResponse(
        request,
        "onboarding/review.html",
        {
            "page_title": "Review Setup",
            "plan": plan.lower(),
            "signup_data": signup_data,
            "workflow_data": progress.workflow_data,
            "phone_setup_data": phone_setup_data,
        },
    )
