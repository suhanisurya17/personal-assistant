from datetime import date, datetime
from enum import StrEnum
from decimal import Decimal

from pydantic import BaseModel, Field


class EmailSummary(BaseModel):
    id: str
    thread_id: str | None = None
    sender: str = ""
    subject: str = ""
    snippet: str = ""
    received_at: datetime | None = None
    labels: list[str] = Field(default_factory=list)


class EmailAttachment(BaseModel):
    filename: str
    mime_type: str
    size_bytes: int = 0


class EmailMessage(BaseModel):
    id: str
    thread_id: str | None = None
    sender: str = ""
    to: str = ""
    subject: str = ""
    received_at: datetime | None = None
    body: str = ""
    attachments: list[EmailAttachment] = Field(default_factory=list)


class SubscriptionStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class BillingCycle(StrEnum):
    MONTHLY = "monthly"
    YEARLY = "yearly"
    UNKNOWN = "unknown"


class SubscriptionRecord(BaseModel):
    id: str
    merchant: str
    amount: Decimal | None = None
    currency: str | None = None
    billing_cycle: BillingCycle = BillingCycle.UNKNOWN
    status: SubscriptionStatus = SubscriptionStatus.UNKNOWN
    next_due_date: date | None = None
    last_payment_date: date | None = None
    due_date_is_estimate: bool = True
    source_email_ids: list[str] = Field(default_factory=list)
    evidence: str
    confidence: str = "low"
    observed_at: datetime


class SubscriptionScanResult(BaseModel):
    scanned_emails: int
    matched_emails: int
    truncated: bool = False
    subscriptions: list[SubscriptionRecord] = Field(default_factory=list)


class SubscriptionTrackerView(BaseModel):
    scan_completed: bool = False
    last_scan_at: datetime | None = None
    last_scanned_emails: int = 0
    subscriptions: list[SubscriptionRecord] = Field(default_factory=list)


class EmailDraft(BaseModel):
    to: list[str] = Field(min_length=1)
    subject: str = ""
    body: str
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)


class CalendarEvent(BaseModel):
    id: str | None = None
    summary: str
    description: str | None = None
    start: datetime
    end: datetime
    location: str | None = None
    html_link: str | None = None


class EventInput(BaseModel):
    summary: str
    start: datetime
    end: datetime
    description: str | None = None
    location: str | None = None
    calendar_id: str = "primary"


class InboxSummary(BaseModel):
    count: int
    unread_count: int
    emails: list[EmailSummary] = Field(default_factory=list)
    overview: str = ""


class CalendarSummary(BaseModel):
    day: date
    count: int
    events: list[CalendarEvent] = Field(default_factory=list)
