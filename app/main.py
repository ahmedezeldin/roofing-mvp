import os
import secrets
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta

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

pwd_context = CryptContext(schemes=["bcrypt_sha256"], deprecated="auto")

AUTH_COOKIE_NAME = "rfd_session"
AUTH_COOKIE_SECURE = True
AUTH_COOKIE_SAMESITE = "lax"

SHORT_SESSION_DAYS = 1
REMEMBER_ME_DAYS = 30
# --------------------------------------------------
# TEMPLATE HELPERS
# --------------------------------------------------

def format_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    return dt.strftime("%Y-%m-%d %I:%M %p")


templates.env.globals["format_dt"] = format_dt


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


def get_workspace_settings(db: Session, workspace_id: int) -> Optional[models.BusinessSettings]:
    return (
        db.query(models.BusinessSettings)
        .filter(models.BusinessSettings.workspace_id == workspace_id)
        .first()
    )


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

    return {
        "new_today": new_today,
        "hot_leads": hot_leads,
        "contacted_count": contacted_count,
        "booked_week": booked_week,
        "won_month": won_month,
        "emergency_overdue": emergency_overdue,
        "recent_hot_leads": recent_hot_leads,
        "response_sla_label": get_response_sla_label("high"),
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

    return {
        "new_today": new_today,
        "hot_leads": hot_leads,
        "contacted_count": contacted_count,
        "booked_week": booked_week,
        "won_month": won_month,
        "emergency_overdue": emergency_overdue,
        "recent_hot_leads": recent_hot_leads,
        "response_sla_label": get_response_sla_label("high"),
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
        {"page_title": "Login"},
    )


@app.post("/login")
def login_submit(
    email: str = Form(...),
    password: str = Form(...),
    remember_me: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    user = (
        db.query(models.AppUser)
        .filter(models.AppUser.email == email.strip().lower())
        .first()
    )

    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

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
        },
    )


@app.post("/signup")
def signup_submit(
    full_name: str = Form(...),
    email: str = Form(...),
    company_name: str = Form(...),
    phone: str = Form(...),
    service_area: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    plan: str = Form("pilot"),
    agree_terms: str = Form(...),
    db: Session = Depends(get_db),
):
    existing_user = (
        db.query(models.AppUser)
        .filter(models.AppUser.email == email.strip().lower())
        .first()
    )
    if existing_user:
        raise HTTPException(status_code=400, detail="An account with this email already exists.")

    if password != confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")

    user = models.AppUser(
        full_name=full_name.strip(),
        email=email.strip().lower(),
        company_name=company_name.strip(),
        password_hash=hash_password(password),
    )
    db.add(user)
    db.flush()

    create_workspace_for_user(
        db=db,
        user=user,
        plan=plan,
        company_name=company_name,
        business_phone=phone,
        primary_service_area=service_area,
    )

    db.commit()

    return RedirectResponse(url=f"/onboarding/workflow?plan={plan}", status_code=303)


@app.get("/onboarding/company", response_class=HTMLResponse)
def onboarding_company_page(request: Request):
    return templates.TemplateResponse(
        request,
        "onboarding/company.html",
        {"page_title": "Company Details"},
    )


@app.get("/onboarding/workflow", response_class=HTMLResponse)
def onboarding_workflow_page(
    request: Request,
    plan: str = Query("pilot"),
):
    return templates.TemplateResponse(
        request,
        "onboarding/workflow.html",
        {
            "page_title": "Workflow Preferences",
            "business_name": "your roofing company",
            "plan": plan.lower(),
        },
    )


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
    plan: str = Query("growth"),
    canceled: int = Query(0),
):
    normalized_plan = (plan or "growth").lower()

    if normalized_plan == "pilot":
        selected_plan = "Pilot"
        monthly_price = "$499"
        setup_fee = "$750"
        due_today = "$750"
    else:
        selected_plan = "Growth"
        monthly_price = "$999"
        setup_fee = "$1,500"
        due_today = "$1,500"

    return templates.TemplateResponse(
        request,
        "billing.html",
        {
            "page_title": "Billing",
            "selected_plan": selected_plan,
            "selected_plan_slug": normalized_plan,
            "monthly_price": monthly_price,
            "setup_fee": setup_fee,
            "due_today": due_today,
            "canceled": bool(canceled),
        },
    )


@app.get("/billing/success", response_class=HTMLResponse)
def billing_success_page(
    request: Request,
    session_id: Optional[str] = Query(None),
):
    session_status = "complete"
    customer_email = None
    subscription_id = None

    if session_id and stripe.api_key:
        try:
            session_data = stripe.checkout.Session.retrieve(session_id)
            session_status = getattr(session_data, "status", None) or "complete"
            customer_email = getattr(session_data, "customer_details", None)
            if customer_email:
                customer_email = getattr(customer_email, "email", None)
            subscription_id = getattr(session_data, "subscription", None)
        except Exception:
            pass

    return HTMLResponse(
        f"""
        <html>
          <head>
            <title>Welcome to Roofing Front Desk</title>
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <style>
              body {{
                margin: 0;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
                background: #f3f5f9;
                color: #0f172a;
              }}
              .wrap {{
                max-width: 760px;
                margin: 60px auto;
                padding: 24px;
              }}
              .card {{
                background: white;
                border-radius: 24px;
                padding: 40px;
                box-shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
                border: 1px solid #e5e7eb;
              }}
              .badge {{
                display: inline-block;
                background: #ecfdf3;
                color: #166534;
                font-weight: 700;
                font-size: 14px;
                padding: 8px 14px;
                border-radius: 999px;
                margin-bottom: 18px;
              }}
              h1 {{
                font-size: 42px;
                line-height: 1.1;
                margin: 0 0 14px;
                letter-spacing: -0.02em;
              }}
              .sub {{
                font-size: 18px;
                line-height: 1.6;
                color: #475569;
                margin-bottom: 28px;
              }}
              .highlight {{
                background: #f8fafc;
                border: 1px solid #e2e8f0;
                border-radius: 18px;
                padding: 22px;
                margin-bottom: 26px;
              }}
              .highlight p {{
                margin: 0;
                font-size: 16px;
                line-height: 1.7;
                color: #334155;
              }}
              .section-title {{
                font-size: 22px;
                font-weight: 800;
                margin: 28px 0 16px;
              }}
              .steps {{
                display: grid;
                gap: 14px;
                margin-bottom: 28px;
              }}
              .step {{
                background: #ffffff;
                border: 1px solid #e5e7eb;
                border-radius: 16px;
                padding: 16px 18px;
              }}
              .step strong {{
                display: block;
                font-size: 16px;
                margin-bottom: 6px;
              }}
              .step span {{
                color: #475569;
                font-size: 15px;
                line-height: 1.5;
              }}
              .meta {{
                margin-top: 18px;
                font-size: 14px;
                color: #64748b;
                line-height: 1.8;
              }}
              .cta-row {{
                display: flex;
                gap: 12px;
                flex-wrap: wrap;
                margin-top: 30px;
              }}
              .btn {{
                display: inline-block;
                text-decoration: none;
                padding: 14px 22px;
                border-radius: 14px;
                font-weight: 700;
                font-size: 15px;
              }}
              .btn-primary {{
                background: #2563eb;
                color: white;
              }}
              .btn-secondary {{
                background: #eef2ff;
                color: #1e3a8a;
              }}
            </style>
          </head>
          <body>
            <div class="wrap">
              <div class="card">
                <div class="badge">✓ Payment confirmed</div>
                <h1>Welcome to Roofing Front Desk</h1>
                <p class="sub">
                  Your payment went through successfully and your workspace is now reserved.
                </p>

                <div class="highlight">
                  <p>
                    <strong>You made the right move.</strong> Roofing Front Desk is built to help your
                    business stop losing revenue after hours, during missed calls, and when your team is too busy to respond fast enough.
                  </p>
                </div>

                <div class="section-title">What happens next</div>

                <div class="steps">
                  <div class="step">
                    <strong>1) Your onboarding spot is reserved</strong>
                    <span>We’ve confirmed your purchase and your setup process is ready to begin.</span>
                  </div>

                  <div class="step">
                    <strong>2) We’ll configure your workspace</strong>
                    <span>You’ll go through company details, workflow preferences, and phone setup so the system matches how your roofing business actually operates.</span>
                  </div>

                  <div class="step">
                    <strong>3) Your lead desk gets prepared for go-live</strong>
                    <span>Once setup is complete, your system will be ready to help capture inbound opportunities and keep conversations moving.</span>
                  </div>
                </div>

                <div class="cta-row">
                  <a class="btn btn-primary" href="/onboarding/company">Continue setup</a>
                  <a class="btn btn-secondary" href="/demo/dashboard">View product demo</a>
                </div>

                <div class="meta">
                  <div><strong>Session status:</strong> {session_status}</div>
                  <div><strong>Customer email:</strong> {customer_email or "Not available"}</div>
                  <div><strong>Subscription ID:</strong> {subscription_id or "Not available"}</div>
                </div>
              </div>
            </div>
          </body>
        </html>
        """
    )
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
    plan_name, line_items = get_checkout_prices(plan)

    metadata = {
        "plan": plan.lower(),
        "plan_name": plan_name,
        "source": "roofing_front_desk_billing",
    }

    if workspace:
        metadata["workspace_id"] = str(workspace.id)

    try:
        checkout_session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=line_items,
            success_url=f"{APP_BASE_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{APP_BASE_URL}/billing?plan={plan}&canceled=1",
            allow_promotion_codes=True,
            metadata=metadata,
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

        workspace_id = metadata.get("workspace_id")
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

    return templates.TemplateResponse(
        request,
        "demo/pipeline.html",
        {
            "settings": settings,
            "columns": columns,
            "twilio_live": logic.twilio_enabled(),
            "active_page": "pipeline",
            "page_title": "Pipeline",
            "page_subtitle": "Track leads from qualification to outcome.",
        },
    )


@app.get("/demo/settings", response_class=HTMLResponse)
def demo_settings(request: Request, db: Session = Depends(get_db)):
    settings = logic.get_or_create_business_settings(db)

    return templates.TemplateResponse(
        request,
        "demo/settings.html",
        {
            "settings": settings,
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

    # Reusing demo template for now
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
            "high_priority_count": high_priority_count,
            "qualified_count": qualified_count,
            "booked_count": booked_count,
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

    settings = get_workspace_settings(db, workspace.id)

    leads = (
        db.query(models.Lead)
        .filter(models.Lead.workspace_id == workspace.id)
        .order_by(models.Lead.updated_at.desc())
        .all()
    )
    columns = build_pipeline_columns(leads)

    return templates.TemplateResponse(
        request,
        "demo/pipeline.html",
        {
            "settings": settings,
            "workspace": workspace,
            "current_user": current_user,
            "columns": columns,
            "twilio_live": logic.twilio_enabled(),
            "active_page": "pipeline",
            "page_title": "Pipeline",
            "page_subtitle": "Track leads from qualification to outcome.",
        },
    )


@app.get("/app/settings", response_class=HTMLResponse)
def app_settings(request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user_from_cookie(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    workspace = get_current_workspace(request, db)
    if not workspace:
        return RedirectResponse(url="/signup", status_code=303)

    settings = get_workspace_settings(db, workspace.id)

    return templates.TemplateResponse(
        request,
        "demo/settings.html",
        {
            "settings": settings,
            "workspace": workspace,
            "current_user": current_user,
            "twilio_live": logic.twilio_enabled(),
            "active_page": "settings",
            "page_title": "Settings",
            "page_subtitle": "Manage business identity and workspace basics.",
        },
    )


# --------------------------------------------------
# DEMO UI FORM ACTIONS
# --------------------------------------------------

@app.post("/ui/settings/update")
def ui_update_settings(
    business_name: str = Form(...),
    db: Session = Depends(get_db),
):
    logic.update_business_name(db, business_name)
    return RedirectResponse(url="/demo/settings", status_code=303)


@app.post("/ui/leads/create")
def ui_create_lead(phone_number: str = Form(...), db: Session = Depends(get_db)):
    logic.create_missed_call_lead(db, phone_number=phone_number)
    return RedirectResponse(url="/demo/inbox", status_code=303)


@app.post("/ui/messages/send")
def ui_send_message(
    lead_id: int = Form(...),
    body: str = Form(...),
    crm_status_filter: str = Form("all"),
    priority_filter: str = Form("all"),
    insurance_filter: str = Form("all"),
    search: str = Form(""),
    db: Session = Depends(get_db),
):
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if not lead:
        return RedirectResponse(url="/demo/inbox", status_code=303)

    logic.process_inbound_message(db, lead, body)

    return RedirectResponse(
        url=(
            f"/demo/inbox?lead_id={lead_id}"
            f"&crm_status_filter={crm_status_filter}"
            f"&priority_filter={priority_filter}"
            f"&insurance_filter={insurance_filter}"
            f"&search={search}"
        ),
        status_code=303,
    )


@app.post("/ui/leads/update-status")
def ui_update_crm_status(
    lead_id: int = Form(...),
    crm_status: str = Form(...),
    crm_status_filter: str = Form("all"),
    priority_filter: str = Form("all"),
    insurance_filter: str = Form("all"),
    search: str = Form(""),
    db: Session = Depends(get_db),
):
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()
    if not lead:
        return RedirectResponse(url="/demo/inbox", status_code=303)

    logic.update_crm_status(db, lead, crm_status)

    return RedirectResponse(
        url=(
            f"/demo/inbox?lead_id={lead_id}"
            f"&crm_status_filter={crm_status_filter}"
            f"&priority_filter={priority_filter}"
            f"&insurance_filter={insurance_filter}"
            f"&search={search}"
        ),
        status_code=303,
    )


@app.post("/ui/leads/update-notes")
def ui_update_lead_notes(
    lead_id: int = Form(...),
    notes: str = Form(...),
    crm_status_filter: str = Form(""),
    priority_filter: str = Form("all"),
    insurance_filter: str = Form("all"),
    search: str = Form(""),
    db: Session = Depends(get_db),
):
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()

    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    lead.notes = notes
    db.commit()

    if crm_status_filter != "":
        return RedirectResponse(
            url=(
                f"/demo/inbox?lead_id={lead_id}"
                f"&crm_status_filter={crm_status_filter}"
                f"&priority_filter={priority_filter}"
                f"&insurance_filter={insurance_filter}"
                f"&search={search}"
            ),
            status_code=303,
        )

    return RedirectResponse(url=f"/demo/lead/{lead_id}", status_code=303)


@app.post("/ui/leads/update-stage")
def ui_update_lead_stage(
    lead_id: int = Form(...),
    crm_status: str = Form(...),
    db: Session = Depends(get_db),
):
    lead = db.query(models.Lead).filter(models.Lead.id == lead_id).first()

    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    if crm_status == "qualified":
        lead.status = "qualified"
        lead.crm_status = "new"
    else:
        lead.crm_status = crm_status
        if crm_status in ["contacted", "booked", "closed"]:
            lead.status = "qualified"

    db.commit()

    return RedirectResponse(url=f"/demo/lead/{lead_id}", status_code=303)


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
    return logic.create_missed_call_lead(
        db,
        phone_number=payload.phone_number,
        source=payload.source,
    )


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
    db: Session = Depends(get_db),):
    # Demo-only for now
    text = logic.normalize_text(Body)
    latest_lead = logic.find_latest_lead_for_phone(db, From)

    if logic.should_start_new_lead(latest_lead, Body):
        lead = logic.create_missed_call_lead(db, phone_number=From, source="sms_inbound")

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
