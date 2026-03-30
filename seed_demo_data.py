from datetime import datetime, timedelta, UTC
from sqlalchemy.orm import Session

from app.db import engine
from app import models


def upsert_settings(db: Session) -> None:
    settings = db.query(models.BusinessSetting).first()
    if not settings:
        settings = models.BusinessSetting(
            business_name="ABC Roofing",
            first_message_template="Hey, thanks for calling {business_name}. Sorry we missed you — are you looking for a repair, replacement, or inspection?",
        )
        db.add(settings)
    else:
        settings.business_name = "ABC Roofing"
    db.commit()


def create_lead(
    db: Session,
    *,
    phone_number: str,
    customer_name: str | None,
    source: str,
    status: str,
    crm_status: str,
    conversation_state: str,
    job_type: str | None,
    postal_code: str | None,
    urgency: str | None,
    priority: str | None,
    insurance_claim: str | None,
    notes: str | None,
    created_at: datetime,
    updated_at: datetime,
    messages: list[tuple[str, str, datetime]],
) -> None:
    existing = db.query(models.Lead).filter(models.Lead.phone_number == phone_number).first()
    if existing:
        db.query(models.Message).filter(models.Message.lead_id == existing.id).delete()
        db.delete(existing)
        db.commit()

    lead = models.Lead(
        phone_number=phone_number,
        customer_name=customer_name,
        source=source,
        status=status,
        crm_status=crm_status,
        conversation_state=conversation_state,
        job_type=job_type,
        postal_code=postal_code,
        urgency=urgency,
        priority=priority,
        insurance_claim=insurance_claim,
        notes=notes,
        created_at=created_at,
        updated_at=updated_at,
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)

    for direction, body, ts in messages:
        msg = models.Message(
            lead_id=lead.id,
            direction=direction,
            body=body,
            created_at=ts,
        )
        db.add(msg)

    db.commit()


def main() -> None:
    now = datetime.now(UTC)

    with Session(engine) as db:
        upsert_settings(db)

        # Optional: clear all existing demo leads/messages first
        db.query(models.Message).delete()
        db.query(models.Lead).delete()
        db.commit()

        create_lead(
            db,
            phone_number="+18254494924",
            customer_name="Ahmed Ezeldin",
            source="missed_call",
            status="qualified",
            crm_status="new",
            conversation_state="done",
            job_type="repair",
            postal_code="T1H 5S3",
            urgency="emergency",
            priority="high",
            insurance_claim="yes",
            notes="Storm damage visible. Customer wants same-day callback.",
            created_at=now - timedelta(hours=2),
            updated_at=now - timedelta(minutes=12),
            messages=[
                ("outbound", "Hey, thanks for calling ABC Roofing. Sorry we missed you — are you looking for a repair, replacement, or inspection?", now - timedelta(hours=2)),
                ("inbound", "repair", now - timedelta(hours=1, minutes=58)),
                ("outbound", "What postal code is the property in?", now - timedelta(hours=1, minutes=57)),
                ("inbound", "T1H 5S3", now - timedelta(hours=1, minutes=55)),
                ("outbound", "What’s your name?", now - timedelta(hours=1, minutes=54)),
                ("inbound", "Ahmed Ezeldin", now - timedelta(hours=1, minutes=53)),
                ("outbound", "How urgent is it — low, medium, or emergency?", now - timedelta(hours=1, minutes=52)),
                ("inbound", "emergency", now - timedelta(hours=1, minutes=50)),
                ("outbound", "Is this related to an insurance claim? yes or no", now - timedelta(hours=1, minutes=49)),
                ("inbound", "yes", now - timedelta(hours=1, minutes=48)),
                ("outbound", "Thanks — your request has been captured. A roofing specialist will follow up shortly.", now - timedelta(hours=1, minutes=47)),
            ],
        )

        create_lead(
            db,
            phone_number="+14035550101",
            customer_name="Sarah Thompson",
            source="sms_inbound",
            status="qualified",
            crm_status="contacted",
            conversation_state="done",
            job_type="replacement",
            postal_code="T2N 1N4",
            urgency="medium",
            priority="medium",
            insurance_claim="yes",
            notes="Possible full roof replacement after hail damage. Sent follow-up and awaiting inspection date.",
            created_at=now - timedelta(days=1, hours=3),
            updated_at=now - timedelta(hours=4),
            messages=[
                ("inbound", "replacement", now - timedelta(days=1, hours=3)),
                ("outbound", "What postal code is the property in?", now - timedelta(days=1, hours=2, minutes=58)),
                ("inbound", "T2N 1N4", now - timedelta(days=1, hours=2, minutes=56)),
                ("outbound", "What’s your name?", now - timedelta(days=1, hours=2, minutes=55)),
                ("inbound", "Sarah Thompson", now - timedelta(days=1, hours=2, minutes=54)),
                ("outbound", "How urgent is it — low, medium, or emergency?", now - timedelta(days=1, hours=2, minutes=53)),
                ("inbound", "medium", now - timedelta(days=1, hours=2, minutes=52)),
                ("outbound", "Is this related to an insurance claim? yes or no", now - timedelta(days=1, hours=2, minutes=51)),
                ("inbound", "yes", now - timedelta(days=1, hours=2, minutes=50)),
                ("outbound", "Thanks — your request has been captured. A roofing specialist will follow up shortly.", now - timedelta(days=1, hours=2, minutes=49)),
            ],
        )

        create_lead(
            db,
            phone_number="+14035550102",
            customer_name="Michael Reyes",
            source="missed_call",
            status="qualified",
            crm_status="booked",
            conversation_state="done",
            job_type="inspection",
            postal_code="T3A 0Z9",
            urgency="low",
            priority="low",
            insurance_claim="no",
            notes="Inspection booked for tomorrow at 2:00 PM.",
            created_at=now - timedelta(days=2),
            updated_at=now - timedelta(hours=6),
            messages=[
                ("outbound", "Hey, thanks for calling ABC Roofing. Sorry we missed you — are you looking for a repair, replacement, or inspection?", now - timedelta(days=2)),
                ("inbound", "inspection", now - timedelta(days=2, minutes=-2)),
                ("outbound", "What postal code is the property in?", now - timedelta(days=2, minutes=-3)),
                ("inbound", "T3A 0Z9", now - timedelta(days=2, minutes=-5)),
                ("outbound", "What’s your name?", now - timedelta(days=2, minutes=-6)),
                ("inbound", "Michael Reyes", now - timedelta(days=2, minutes=-7)),
                ("outbound", "How urgent is it — low, medium, or emergency?", now - timedelta(days=2, minutes=-8)),
                ("inbound", "low", now - timedelta(days=2, minutes=-9)),
                ("outbound", "Is this related to an insurance claim? yes or no", now - timedelta(days=2, minutes=-10)),
                ("inbound", "no", now - timedelta(days=2, minutes=-11)),
            ],
        )

        create_lead(
            db,
            phone_number="+14035550103",
            customer_name="Olivia Chen",
            source="missed_call",
            status="qualified",
            crm_status="closed",
            conversation_state="done",
            job_type="replacement",
            postal_code="T2V 3K1",
            urgency="medium",
            priority="medium",
            insurance_claim="no",
            notes="Signed and won. Install next week.",
            created_at=now - timedelta(days=5),
            updated_at=now - timedelta(days=1),
            messages=[
                ("outbound", "Hey, thanks for calling ABC Roofing. Sorry we missed you — are you looking for a repair, replacement, or inspection?", now - timedelta(days=5)),
                ("inbound", "replacement", now - timedelta(days=5, minutes=-2)),
                ("outbound", "Thanks — your request has been captured. A roofing specialist will follow up shortly.", now - timedelta(days=5, minutes=-3)),
            ],
        )

        create_lead(
            db,
            phone_number="+14035550104",
            customer_name="Daniel Brooks",
            source="sms_inbound",
            status="qualified",
            crm_status="lost",
            conversation_state="done",
            job_type="repair",
            postal_code="T2K 2P2",
            urgency="low",
            priority="low",
            insurance_claim="no",
            notes="Customer chose another contractor.",
            created_at=now - timedelta(days=7),
            updated_at=now - timedelta(days=3),
            messages=[
                ("inbound", "repair", now - timedelta(days=7)),
                ("outbound", "Thanks — your request has been captured. A roofing specialist will follow up shortly.", now - timedelta(days=7, minutes=-2)),
            ],
        )

        create_lead(
            db,
            phone_number="+14035550105",
            customer_name="Priya Patel",
            source="sms_inbound",
            status="new",
            crm_status="new",
            conversation_state="ask_postal_code",
            job_type="repair",
            postal_code=None,
            urgency=None,
            priority=None,
            insurance_claim=None,
            notes="Customer stopped after first reply. Needs re-engagement.",
            created_at=now - timedelta(minutes=35),
            updated_at=now - timedelta(minutes=20),
            messages=[
                ("inbound", "repair", now - timedelta(minutes=35)),
                ("outbound", "What postal code is the property in?", now - timedelta(minutes=34)),
            ],
        )

        print("Demo data seeded successfully.")


if __name__ == "__main__":
    main()