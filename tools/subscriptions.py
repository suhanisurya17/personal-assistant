import calendar
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from email.utils import parseaddr
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import get_settings
from models.schemas import (
    BillingCycle,
    EmailMessage,
    SubscriptionRecord,
    SubscriptionScanResult,
    SubscriptionStatus,
    SubscriptionTrackerView,
)
from tools.gmail import read_email
from tools.google_auth import build_google_service

SUBSCRIPTION_QUERY = (
    "in:inbox {subscription subscriptions renewal renewed recurring "
    "membership member trial billing billed}"
)
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}
_AMOUNT_PATTERN = re.compile(
    r"(?:(USD|EUR|GBP|CAD|AUD|[$€£])\s*)"
    r"(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"|(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s*(USD|EUR|GBP|CAD|AUD)",
    re.IGNORECASE,
)
_INACTIVE_PATTERN = re.compile(
    r"\b(?:(?:subscriptions?|membership|plan).{0,40}(?:cancelled|canceled|expired|ended)|"
    r"(?:cancelled|canceled|expired|ended).{0,40}(?:subscriptions?|membership|plan)|"
    r"will not renew|won't renew|auto[- ]renew(?:al)? (?:is )?off|no longer active)\b",
    re.IGNORECASE,
)
_ACTIVE_PATTERN = re.compile(
    r"\b(payment (?:was )?(?:received|successful|processed)|payment confirmation|"
    r"subscription renewed|renewal (?:confirmed|successful)|subscription is active|"
    r"next billing|next payment|will renew|has been renewed|charged)\b",
    re.IGNORECASE,
)
_PAYMENT_PATTERN = re.compile(
    r"\b(payment (?:was )?(?:received|successful|processed)|payment confirmation|"
    r"subscription renewed|renewal (?:confirmed|successful)|has been renewed|charged)\b",
    re.IGNORECASE,
)
_SUBSCRIPTION_PATTERN = re.compile(
    r"\b(subscriptions?|recurring|membership|monthly plan|annual plan|yearly plan|"
    r"monthly|annual|yearly|renews?|renewal|auto[- ]renew)\b",
    re.IGNORECASE,
)
_MONTHLY_PATTERN = re.compile(r"\b(monthly|per month|each month|every month|/month)\b", re.IGNORECASE)
_YEARLY_PATTERN = re.compile(
    r"\b(annual(?:ly)?|yearly|per year|each year|every year|/year)\b", re.IGNORECASE
)
_EXPLICIT_DUE_PATTERN = re.compile(
    r"(?:next billing date|next payment date|renews? on|renewal date|next renewal)\s*[:\-]?\s*"
    r"(?:is\s+)?"
    r"([A-Za-z]{3,9}\s+\d{1,2}(?:,\s*\d{4})?)",
    re.IGNORECASE,
)


def _service(service: Any | None) -> Any:
    return service if service is not None else build_google_service("gmail", "v1")


def _merchant(message: EmailMessage) -> str:
    display_name, address = parseaddr(message.sender)
    if display_name.strip():
        return display_name.strip()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    name = domain.split(".")[0] if domain else (message.sender or "Unknown sender")
    return name.replace("-", " ").replace("_", " ").title()


def _billing_cycle(text: str) -> BillingCycle:
    monthly = _MONTHLY_PATTERN.search(text)
    yearly = _YEARLY_PATTERN.search(text)
    if monthly and yearly:
        return BillingCycle.UNKNOWN
    if monthly:
        return BillingCycle.MONTHLY
    if yearly:
        return BillingCycle.YEARLY
    return BillingCycle.UNKNOWN


def _amount(text: str) -> tuple[Decimal | None, str | None, bool]:
    matches = list(_AMOUNT_PATTERN.finditer(text))
    if not matches:
        return None, None, False
    currencies: list[tuple[Decimal, str]] = []
    for match in matches:
        if match.group(1):
            currency = match.group(1).upper()
            number = match.group(2)
        else:
            number = match.group(3)
            currency = match.group(4).upper()
        currency = _CURRENCY_SYMBOLS.get(currency, currency)
        currencies.append((Decimal(number.replace(",", "")), currency))
    distinct = set(currencies)
    if len(distinct) != 1:
        return None, None, True
    amount, currency = currencies[0]
    return amount, currency, False


def _explicit_due_date(text: str, message_date: date) -> date | None:
    match = _EXPLICIT_DUE_PATTERN.search(text)
    if not match:
        return None
    value = match.group(1)
    for date_format in ("%B %d, %Y", "%b %d, %Y", "%B %d", "%b %d"):
        try:
            parsed = datetime.strptime(value, date_format).date()
            if "%Y" not in date_format:
                parsed = parsed.replace(year=message_date.year)
                if parsed < message_date:
                    parsed = parsed.replace(year=parsed.year + 1)
            return parsed
        except ValueError:
            continue
    return None


def _advance_date(value: date, cycle: BillingCycle) -> date | None:
    if cycle == BillingCycle.MONTHLY:
        month_index = value.month
        year = value.year
        if month_index == 12:
            year += 1
            month = 1
        else:
            month = month_index + 1
        return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))
    if cycle == BillingCycle.YEARLY:
        year = value.year + 1
        return date(year, value.month, min(value.day, calendar.monthrange(year, value.month)[1]))
    return None


def _next_due_date(
    last_payment_date: date | None,
    cycle: BillingCycle,
    today: date,
    explicit_due_date: date | None,
) -> tuple[date | None, bool]:
    if explicit_due_date is not None:
        return explicit_due_date, False
    if last_payment_date is None:
        return None, True
    due_date = _advance_date(last_payment_date, cycle)
    while due_date is not None and due_date < today:
        due_date = _advance_date(due_date, cycle)
    return due_date, True


def _extract_record(message: EmailMessage, today: date) -> SubscriptionRecord | None:
    text = f"{message.subject}\n{message.body}"
    if not _SUBSCRIPTION_PATTERN.search(text):
        return None

    status_matches = [
        (match.start(), SubscriptionStatus.INACTIVE, match.group(0))
        for match in _INACTIVE_PATTERN.finditer(text)
    ] + [
        (match.start(), SubscriptionStatus.ACTIVE, match.group(0))
        for match in _ACTIVE_PATTERN.finditer(text)
    ]
    latest_status = max(status_matches, default=None, key=lambda match: match[0])
    if latest_status and latest_status[1] == SubscriptionStatus.INACTIVE:
        status = SubscriptionStatus.INACTIVE
        status_evidence = latest_status[2]
    elif latest_status and latest_status[1] == SubscriptionStatus.ACTIVE:
        status = SubscriptionStatus.ACTIVE
        status_evidence = latest_status[2]
    else:
        status = SubscriptionStatus.UNKNOWN
        status_evidence = "Subscription-related message; no clear active or inactive status found."

    received_at = message.received_at or datetime.now(timezone.utc)
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    message_date = received_at.date()
    cycle = _billing_cycle(text)
    amount, currency, ambiguous_amount = _amount(text)
    is_payment = bool(_PAYMENT_PATTERN.search(text))
    last_payment_date = message_date if is_payment else None
    explicit_due_date = _explicit_due_date(text, message_date)
    next_due_date, due_is_estimate = _next_due_date(
        last_payment_date, cycle, today, explicit_due_date
    )
    expected_due = explicit_due_date or _advance_date(last_payment_date or message_date, cycle)
    if status == SubscriptionStatus.ACTIVE and expected_due is not None and expected_due < today:
        status = SubscriptionStatus.UNKNOWN
        status_evidence += " Billing evidence is overdue; current status is unknown."
    if status == SubscriptionStatus.INACTIVE:
        next_due_date = explicit_due_date
        due_is_estimate = explicit_due_date is None

    evidence = f"{message.subject or '(no subject)'}: {status_evidence}"
    if ambiguous_amount:
        evidence += " Multiple different amounts found; amount left blank."
    confidence = "low"
    if (
        status != SubscriptionStatus.UNKNOWN
        and cycle != BillingCycle.UNKNOWN
        and amount is not None
        and not ambiguous_amount
    ):
        confidence = "high"
    elif status != SubscriptionStatus.UNKNOWN and not ambiguous_amount and (cycle != BillingCycle.UNKNOWN or amount is not None):
        confidence = "medium"
    merchant = _merchant(message)
    record_id = re.sub(r"[^a-z0-9]+", "-", merchant.lower()).strip("-") or "unknown"
    return SubscriptionRecord(
        id=record_id,
        merchant=merchant,
        amount=amount,
        currency=currency,
        billing_cycle=cycle,
        status=status,
        next_due_date=next_due_date,
        last_payment_date=last_payment_date,
        due_date_is_estimate=due_is_estimate,
        source_email_ids=[message.id],
        evidence=evidence,
        confidence=confidence,
        observed_at=received_at,
    )


class SubscriptionStore:
    def __init__(self, database_path: str = "assistant.db") -> None:
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS subscriptions (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                observed_at TEXT NOT NULL
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS subscription_tracker_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_scan_at TEXT NOT NULL,
                last_scanned_emails INTEGER NOT NULL
            )"""
        )
        self.connection.commit()

    def upsert(self, record: SubscriptionRecord) -> SubscriptionRecord:
        row = self.connection.execute(
            "SELECT data, observed_at FROM subscriptions WHERE id = ?", (record.id,)
        ).fetchone()
        if row:
            existing = SubscriptionRecord.model_validate_json(row["data"])
            if existing.observed_at > record.observed_at:
                record = existing.model_copy(
                    update={
                        "source_email_ids": list(dict.fromkeys(existing.source_email_ids + record.source_email_ids))
                    }
                )
            else:
                changes: dict[str, Any] = {
                    "source_email_ids": list(dict.fromkeys(existing.source_email_ids + record.source_email_ids))
                }
                for field in ("amount", "currency", "last_payment_date"):
                    if getattr(record, field) is None and getattr(existing, field) is not None:
                        changes[field] = getattr(existing, field)
                if record.billing_cycle == BillingCycle.UNKNOWN:
                    changes["billing_cycle"] = existing.billing_cycle
                if record.next_due_date is None and record.status != SubscriptionStatus.INACTIVE:
                    changes["next_due_date"] = existing.next_due_date
                    changes["due_date_is_estimate"] = existing.due_date_is_estimate
                record = record.model_copy(update=changes)
        self.connection.execute(
            "INSERT OR REPLACE INTO subscriptions (id, data, observed_at) VALUES (?, ?, ?)",
            (record.id, record.model_dump_json(), record.observed_at.isoformat()),
        )
        self.connection.commit()
        return record

    def list_all(self) -> list[SubscriptionRecord]:
        rows = self.connection.execute(
            "SELECT data FROM subscriptions ORDER BY json_extract(data, '$.next_due_date') IS NULL, "
            "json_extract(data, '$.next_due_date'), json_extract(data, '$.merchant')"
        ).fetchall()
        return [SubscriptionRecord.model_validate_json(row["data"]) for row in rows]

    def mark_scan_completed(self, scanned_emails: int) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO subscription_tracker_state
               (id, last_scan_at, last_scanned_emails) VALUES (1, ?, ?)""",
            (datetime.now(timezone.utc).isoformat(), scanned_emails),
        )
        self.connection.commit()

    def tracker_view(self) -> SubscriptionTrackerView:
        state = self.connection.execute(
            "SELECT last_scan_at, last_scanned_emails FROM subscription_tracker_state WHERE id = 1"
        ).fetchone()
        subscriptions = self.list_all()
        return SubscriptionTrackerView(
            scan_completed=state is not None or bool(subscriptions),
            last_scan_at=datetime.fromisoformat(state["last_scan_at"]) if state else None,
            last_scanned_emails=state["last_scanned_emails"] if state else 0,
            subscriptions=subscriptions,
        )

    def close(self) -> None:
        self.connection.close()


def scan_subscription_inbox(
    max_results: int = 100,
    service: Any | None = None,
    database_path: str | None = None,
    today: date | None = None,
) -> SubscriptionScanResult:
    if not 1 <= max_results <= 500:
        raise ValueError("max_results must be between 1 and 500")

    gmail = _service(service)
    settings = get_settings()
    local_today = today or datetime.now(ZoneInfo(settings.google_timezone)).date()
    store = SubscriptionStore(database_path or settings.database_path)
    scanned = 0
    matched = 0
    page_token = None
    records_by_id: dict[str, SubscriptionRecord] = {}

    try:
        while scanned < max_results:
            page_size = min(100, max_results - scanned)
            response = (
                gmail.users()
                .messages()
                .list(
                    userId="me",
                    q=SUBSCRIPTION_QUERY,
                    maxResults=page_size,
                    pageToken=page_token,
                )
                .execute()
            )
            messages = response.get("messages", [])
            if not messages:
                break
            for item in messages:
                if scanned >= max_results:
                    break
                message = read_email(item["id"], service=gmail)
                scanned += 1
                record = _extract_record(message, local_today)
                if record is None:
                    continue
                matched += 1
                existing_batch = records_by_id.get(record.id)
                if existing_batch is not None:
                    merged_ids = list(dict.fromkeys(existing_batch.source_email_ids + record.source_email_ids))
                    if existing_batch.observed_at > record.observed_at:
                        record = existing_batch.model_copy(update={"source_email_ids": merged_ids})
                    else:
                        changes: dict[str, Any] = {"source_email_ids": merged_ids}
                        if record.amount is None:
                            changes["amount"] = existing_batch.amount
                            changes["currency"] = existing_batch.currency
                        if record.billing_cycle == BillingCycle.UNKNOWN:
                            changes["billing_cycle"] = existing_batch.billing_cycle
                        if record.next_due_date is None and record.status != SubscriptionStatus.INACTIVE:
                            changes["next_due_date"] = existing_batch.next_due_date
                            changes["due_date_is_estimate"] = existing_batch.due_date_is_estimate
                        record = record.model_copy(update=changes)
                records_by_id[record.id] = record

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        for record in records_by_id.values():
            store.upsert(record)
        store.mark_scan_completed(scanned)
        return SubscriptionScanResult(
            scanned_emails=scanned,
            matched_emails=matched,
            truncated=page_token is not None,
            subscriptions=store.list_all(),
        )
    finally:
        store.close()


def get_subscription_tracker(database_path: str | None = None) -> SubscriptionTrackerView:
    store = SubscriptionStore(database_path or get_settings().database_path)
    try:
        return store.tracker_view()
    finally:
        store.close()
