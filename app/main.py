import os
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta

import stripe
from fastapi import FastAPI, Request, Depends, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from twilio.twiml.messaging_response import MessagingResponse

from .db import Base, engine, get_db
from . import models, schemas, logic


Base.metadata.create_all(bind=engine)

app = FastAPI(title="Roofing Front Desk")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# ----------------------------
# STRIPE CONFIG
# ----------------------------

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

APP_BASE_URL = os.getenv("APP_BASE_URL", "https://www.roofingfrontdesk.com")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

STRIPE_PRICE_PILOT = os.getenv("STRIPE_PRICE_PILOT", "")
STRIPE_PRICE_PILOT_SETUP = os.getenv("STRIPE_PRICE_PILOT_SETUP", "")

STRIPE_PRICE_GROWTH = os.getenv("STRIPE_PRICE_GROWTH", "")
STRIPE_PRICE_GROWTH_SETUP = os.getenv("STRIPE_PRICE_GROWTH_SETUP", "")


def format_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    return dt.strftime("%Y-%m-%d %I:%M %p")


templates.env.globals["format_dt"] = format_dt


def get_response_sla_label(priority: Optional[str]) -> str:
    priority = (priority or "").lower()
    if priority == "high":
        return "Under 5 min"
    if priority == "medium":
        return "Same day"
    return "Business hours"


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


# ----------------------------
# PUBLIC PAGES
# ----------------------------

@app.get("/", response_class=HTMLResponse)
def landing_page(request: Request):
    return templates.TemplateResponse(
        request,
        "landing.html",
        {
            "page_title": "Roofing Front Desk",
        },
    )


@app.get("/pricing", response_class=HTMLResponse)
def pricing_page(request: Request):
    return templates.TemplateResponse(
        request,
        "pricing.html",
        {
            "page_title": "Pricing",
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "page_title": "Login",
        },
    )


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    return templates.TemplateResponse(
        request,
        "signup.html",
        {
            "page_title": "Start Setup",
        },
    )


@app.get("/onboarding/company", response_class=HTMLResponse)
def onboarding_company_page(request: Request):
    return templates.TemplateResponse(
        request,
        "onboarding/company.html",
        {
            "page_title": "Company Details",
        },
    )


@app.get("/onboarding/workflow", response_class=HTMLResponse)
def onboarding_workflow_page(request: Request):
    return templates.TemplateResponse(
        request,
        "onboarding/workflow.html",
        {
            "page_title": "Workflow Preferences",
            "business_name": "your roofing company",
        },
    )


@app.get("/onboarding/twilio", response_class=HTMLResponse)
def onboarding_twilio_page(request: Request):
    return templates.TemplateResponse(
        request,
        "onboarding/twilio.html",
        {
            "page_title": "Phone Setup",
        },
    )


@app.get("/onboarding/complete", response_class=HTMLResponse)
def onboarding_complete_page(request: Request):
    return templates.TemplateResponse(
        request,
        "onboarding/complete.html",
        {
            "page_title": "Setup Complete",
        },
    )


@app.get("/billing", response_class=HTMLResponse)
def billing_page(
    request: Request,
    plan: str = Query("growth"),
    canceled: int = Query(0),
):
    normalized_plan = plan.lower()

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
    session_data = None

    if session_id and stripe.api_key:
        try:
            session_data = stripe.checkout.Session.retrieve(session_id)
        except Exception:
            session_data = None

    return templates.TemplateResponse(
        request,
        "billing_success.html",
        {
            "page_title": "Billing Success",
            "session_id": session_id,
            "session_data": session_data,
        },
    )


# ----------------------------
# STRIPE CHECKOUT
# ----------------------------
@app.post("/stripe/create-checkout-session")
def create_checkout_session(plan: str = Form(...)):
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Missing STRIPE_SECRET_KEY")

    plan_name, line_items = get_checkout_prices(plan)

    print("DEBUG plan:", plan)
    print("DEBUG plan_name:", plan_name)
    print("DEBUG STRIPE_PRICE_GROWTH:", STRIPE_PRICE_GROWTH)
    print("DEBUG STRIPE_PRICE_GROWTH_SETUP:", STRIPE_PRICE_GROWTH_SETUP)
    print("DEBUG STRIPE_PRICE_PILOT:", STRIPE_PRICE_PILOT)
    print("DEBUG STRIPE_PRICE_PILOT_SETUP:", STRIPE_PRICE_PILOT_SETUP)
    print("DEBUG line_items:", line_items)

    try:
        checkout_session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=line_items,
            success_url=f"{APP_BASE_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{APP_BASE_URL}/billing?plan={plan}&canceled=1",
            allow_promotion_codes=True,
        )
        return RedirectResponse(url=checkout_session.url, status_code=303)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Stripe checkout error: {exc}")


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
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

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        print("checkout.session.completed", session.get("id"))

    elif event_type == "customer.subscription.created":
        subscription = event["data"]["object"]
        print("customer.subscription.created", subscription.get("id"))

    elif event_type == "customer.subscription.updated":
        subscription = event["data"]["object"]
        print("customer.subscription.updated", subscription.get("id"))

    elif event_type == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        print("customer.subscription.deleted", subscription.get("id"))

    elif event_type == "invoice.paid":
        invoice = event["data"]["object"]
        print("invoice.paid", invoice.get("id"))

    elif event_type == "invoice.payment_failed":
        invoice = event["data"]["object"]
        print("invoice.payment_failed", invoice.get("id"))

    return JSONResponse({"received": True})


# ----------------------------
# DEMO INDEX
# ----------------------------

@app.get("/demo", response_class=HTMLResponse)
def demo_index(request: Request):
    return templates.TemplateResponse(
        request,
        "demo/index.html",
        {
            "page_title": "Product Demo",
        },
    )


# ----------------------------
# DEMO APP PAGES
# ----------------------------

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


# ----------------------------
# DEMO UI FORM ACTIONS
# ----------------------------

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


# ----------------------------
# API
# ----------------------------

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


# ----------------------------
# TWILIO WEBHOOK
# ----------------------------

@app.post("/webhooks/twilio/inbound", response_class=HTMLResponse)
def twilio_inbound(
    From: str = Form(...),
    Body: str = Form(...),
    db: Session = Depends(get_db),
):
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
