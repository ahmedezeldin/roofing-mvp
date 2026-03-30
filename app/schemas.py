from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, ConfigDict


class LeadCreate(BaseModel):
    phone_number: str
    source: str = "missed_call"


class InboundMessageCreate(BaseModel):
    lead_id: int
    body: str


class BusinessSettingsUpdate(BaseModel):
    business_name: str


class MessageOut(BaseModel):
    id: int
    direction: str
    body: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LeadOut(BaseModel):
    id: int
    phone_number: str
    source: str
    conversation_state: str
    status: str
    job_type: Optional[str]
    postal_code: Optional[str]
    customer_name: Optional[str]
    urgency: Optional[str]
    priority: Optional[str]
    insurance_claim: Optional[str]
    crm_status: Optional[str]
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime
    messages: List[MessageOut] = []

    model_config = ConfigDict(from_attributes=True)


class BusinessSettingsOut(BaseModel):
    id: int
    business_name: str
    first_message_template: str

    model_config = ConfigDict(from_attributes=True)