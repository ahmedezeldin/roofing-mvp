import os
import re
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.orm import Session

from . import models

try:
    from twilio.rest import Client
except Exception:
    Client = None


VALID_JOB_TYPES = {
    "repair": "repair",
    "replacement": "replacement",
    "inspection": "inspection",
}

VALID_URGENCY = {
    "emergency": "emergency",
    "this week": "this week",
    "this_week": "this week",
    "week": "this week",
    "flexible": "flexible",
}

VALID_YES_NO = {
    "yes": "yes",
    "y": "yes",
    "no": "no",
    "n": "no",
}

RESTART_KEYWORDS = {
    "new",
    "new issue",
    "start over",
    "restart",
    "another issue",
    "different issue",
}


def normalize_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


def is_valid_postal_code(text: str) -> bool:
    pattern = r"^[A-Za-z]\d[A-Za-z][ -]?\d[A-Za-z]\d$"
    return bool(re.match(pattern, text.strip()))


def normalize_postal_code(text: str) -> str:
    cleaned = text.strip().upper().replace(" ", "")
    if len(cleaned) == 6:
        return cleaned[:3] + " " + cleaned[3:]
    return text.strip().upper()


def looks_like_name(text: str) -> bool:
    cleaned = text.strip()
    if len(cleaned) < 2:
        return False
    if len(cleaned.split()) > 4:
        return False
    return bool(re.match(r"^[A-Za-z][A-Za-z\s'\-]+$", cleaned))


def get_or_create_business_settings(db: Session) -> models.BusinessSetting:
    settings = db.query(models.BusinessSetting).first()
    if not settings:
        settings = models.BusinessSetting()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def update_business_name(db: Session, business_name: str) -> models.BusinessSetting:
    settings = get_or_create_business_settings(db)
    settings.business_name = business_name.strip()
    settings.first_message_template = (
        "Hey, thanks for calling {business_name}. Sorry we missed you — "
        "are you looking for a repair, replacement, or inspection?"
    )
    db.commit()
    db.refresh(settings)
    return settings


def initial_missed_call_message(db: Session) -> str:
    settings = get_or_create_business_settings(db)
    return settings.first_message_template.format(business_name=settings.business_name)


def compute_priority(urgency: Optional[str]) -> Optional[str]:
    if urgency == "emergency":
        return "high"
    if urgency == "this week":
        return "medium"
    if urgency == "flexible":
        return "low"
    return None


def recommended_response_time(priority: Optional[str]) -> str:
    if priority == "high":
        return "Call within 5 minutes"
    if priority == "medium":
        return "Follow up today"
    if priority == "low":
        return "Follow up within 24 hours"
    return "Follow up soon"


def create_message(db: Session, lead_id: int, direction: str, body: str) -> models.Message:
    msg = models.Message(
        lead_id=lead_id,
        direction=direction,
        body=body,
    )
    db.add(msg)
    db.flush()
    return msg


def twilio_enabled() -> bool:
    return all([
        os.getenv("TWILIO_ACCOUNT_SID"),
        os.getenv("TWILIO_AUTH_TOKEN"),
        os.getenv("TWILIO_FROM_NUMBER"),
    ])


def send_sms_if_configured(to_number: str, body: str) -> bool:
    if not twilio_enabled():
        print("[SMS DEBUG] Twilio not enabled - missing env vars")
        return False

    if Client is None:
        print("[SMS DEBUG] Twilio client import failed")
        return False

    print(f"[SMS DEBUG] FROM={os.getenv('TWILIO_FROM_NUMBER')}")
    print(f"[SMS DEBUG] TO={to_number}")
    print(f"[SMS DEBUG] BODY={body}")

    client = Client(
        os.getenv("TWILIO_ACCOUNT_SID"),
        os.getenv("TWILIO_AUTH_TOKEN"),
    )

    message = client.messages.create(
        body=body,
        from_=os.getenv("TWILIO_FROM_NUMBER"),
        to=to_number,
    )

    print(f"[SMS DEBUG] Twilio message SID: {message.sid}")
    print(f"[SMS DEBUG] Twilio status: {message.status}")
    return True


def owner_alert_enabled() -> bool:
    return twilio_enabled() and bool(os.getenv("OWNER_ALERT_NUMBER"))


def send_owner_sms_alert(lead: models.Lead, business_name: str) -> bool:
    if not owner_alert_enabled():
        print("[OWNER ALERT DEBUG] Owner SMS alert not enabled - missing OWNER_ALERT_NUMBER or Twilio env vars")
        return False

    owner_number = os.getenv("OWNER_ALERT_NUMBER")

    body = (
        f"New qualified lead for {business_name}\n\n"
        f"Name: {lead.customer_name or '—'}\n"
        f"Phone: {lead.phone_number}\n"
        f"Job: {lead.job_type or '—'}\n"
        f"Postal: {lead.postal_code or '—'}\n"
        f"Urgency: {lead.urgency or '—'}\n"
        f"Insurance: {lead.insurance_claim or '—'}\n"
        f"Priority: {lead.priority or '—'}\n"
        f"Action: {recommended_response_time(lead.priority)}"
    )

    try:
        sent = send_sms_if_configured(owner_number, body)
        print(f"[OWNER ALERT DEBUG] Owner SMS alert sent={sent} to {owner_number}")
        return sent
    except Exception as e:
        print(f"[OWNER ALERT ERROR] Failed to send owner SMS alert: {e}")
        return False


def create_outbound_message(db: Session, lead_id: int, body: str, phone_number: str) -> models.Message:
    print("[SMS DEBUG] create_outbound_message called")

    msg = create_message(db, lead_id, "outbound", body)

    try:
        sent = send_sms_if_configured(phone_number, body)
        print(f"[SMS DEBUG] attempted send to {phone_number}, success={sent}")
    except Exception as e:
        print(f"[SMS ERROR] Failed to send SMS to {phone_number}: {e}")

    return msg


def create_missed_call_lead(db: Session, phone_number: str, source: str = "missed_call") -> models.Lead:
    lead = models.Lead(
        phone_number=phone_number,
        source=source,
        conversation_state="awaiting_job_type",
        status="new",
        crm_status="new",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(lead)
    db.flush()

    first_message = initial_missed_call_message(db)
    create_outbound_message(db, lead.id, first_message, phone_number)

    db.commit()
    db.refresh(lead)
    return lead


def update_crm_status(db: Session, lead: models.Lead, crm_status: str) -> models.Lead:
    lead.crm_status = crm_status
    lead.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(lead)
    return lead


def update_notes(db: Session, lead: models.Lead, notes: str) -> models.Lead:
    lead.notes = notes
    lead.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(lead)
    return lead


def is_finished_lead(lead: models.Lead) -> bool:
    return lead.conversation_state == "qualified" or lead.status == "qualified"


def find_latest_lead_for_phone(db: Session, phone_number: str) -> Optional[models.Lead]:
    return (
        db.query(models.Lead)
        .filter(models.Lead.phone_number == phone_number)
        .order_by(models.Lead.created_at.desc())
        .first()
    )


def should_start_new_lead(latest_lead: Optional[models.Lead], inbound_text: str) -> bool:
    if not latest_lead:
        return True

    text = normalize_text(inbound_text)

    if text in RESTART_KEYWORDS:
        return True

    if is_finished_lead(latest_lead):
        return True

    return False


def process_inbound_message(
    db: Session,
    lead: models.Lead,
    inbound_text: str,
    send_outbound_sms: bool = True,
) -> str:
    text = normalize_text(inbound_text)

    create_message(db, lead.id, "inbound", inbound_text)

    just_qualified = False

    if lead.conversation_state == "awaiting_job_type":
        if text in VALID_JOB_TYPES:
            lead.job_type = VALID_JOB_TYPES[text]
            lead.conversation_state = "awaiting_postal_code"
            lead.status = "in_progress"
            reply = "What postal code is the property in?"
        else:
            reply = "Thanks — are you looking for a repair, replacement, or inspection?"

    elif lead.conversation_state == "awaiting_postal_code":
        if is_valid_postal_code(inbound_text):
            lead.postal_code = normalize_postal_code(inbound_text)
            lead.conversation_state = "awaiting_name"
            lead.status = "in_progress"
            reply = "Thanks — what’s your name?"
        else:
            reply = "Please send the property postal code in a format like T3K 5X2."

    elif lead.conversation_state == "awaiting_name":
        if looks_like_name(inbound_text):
            lead.customer_name = inbound_text.strip()
            lead.conversation_state = "awaiting_urgency"
            lead.status = "in_progress"
            reply = "How urgent is it: emergency, this week, or flexible?"
        else:
            reply = "Please reply with your name."

    elif lead.conversation_state == "awaiting_urgency":
        if text in VALID_URGENCY:
            lead.urgency = VALID_URGENCY[text]
            lead.priority = compute_priority(lead.urgency)
            lead.conversation_state = "awaiting_insurance_claim"
            lead.status = "in_progress"
            reply = "Is this related to an insurance claim? Reply yes or no."
        else:
            reply = "How urgent is it: emergency, this week, or flexible?"

    elif lead.conversation_state == "awaiting_insurance_claim":
        if text in VALID_YES_NO:
            lead.insurance_claim = VALID_YES_NO[text]
            lead.conversation_state = "qualified"
            lead.status = "qualified"
            just_qualified = True
            reply = "Thanks — your request has been captured. A roofing specialist will follow up shortly."
        else:
            reply = "Is this related to an insurance claim? Reply yes or no."

    else:
        reply = "Thanks — your request has already been captured. A roofing specialist will follow up shortly."

    lead.updated_at = datetime.utcnow()

    if send_outbound_sms:
        create_outbound_message(db, lead.id, reply, lead.phone_number)
    else:
        create_message(db, lead.id, "outbound", reply)

    db.commit()
    db.refresh(lead)

    if just_qualified:
        settings = get_or_create_business_settings(db)
        send_owner_sms_alert(lead, settings.business_name)

    return reply