"""Data shaping for the web dashboard. Wraps the existing Gmail/Calendar tools.

OpenAI usage is kept low on purpose:
- Each email is triaged once; the verdict is cached by message ID forever.
- The daily brief is generated once per day and cached; only an explicit
  "rewrite" asks the model again.
"""
from datetime import date, datetime, time, timedelta
from email.utils import parseaddr
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from config import get_settings
from models.schemas import CalendarEvent, EmailSummary
from tools.ai import AIUnavailable, Completer, cache_get, cache_set, complete_json
from tools.calendar import summarize_day
from tools.gmail import check_inbox
from tools.news import NewsDigest, sender_query

TRIAGE_CACHE = "triage:v1:"
BRIEF_CACHE = "brief:v1:"
TRIAGE_BATCH = 25
ATTENTION_LOOKBACK_DAYS = 14
LINKED_DAYS_MAX = 5


# ---------- schedule ----------

class ScheduleItem(BaseModel):
    id: str | None = None
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str | None = None
    link: str | None = None


class FreeBlock(BaseModel):
    start: datetime
    end: datetime
    minutes: int


class ScheduleView(BaseModel):
    day: date
    timezone: str
    day_start_hour: int = 8
    day_end_hour: int = 22
    events: list[ScheduleItem] = Field(default_factory=list)
    tomorrow: list[ScheduleItem] = Field(default_factory=list)
    free: list[FreeBlock] = Field(default_factory=list)


def _is_all_day(event: CalendarEvent) -> bool:
    span = event.end - event.start
    return (
        event.start.utcoffset() == timedelta(0)
        and event.start.time() == time.min
        and span >= timedelta(days=1)
        and span % timedelta(days=1) == timedelta(0)
    )


def _item(event: CalendarEvent, tz: ZoneInfo) -> ScheduleItem:
    all_day = _is_all_day(event)
    return ScheduleItem(
        id=event.id,
        title=event.summary,
        start=event.start if all_day else event.start.astimezone(tz),
        end=event.end if all_day else event.end.astimezone(tz),
        all_day=all_day,
        location=event.location,
        link=event.html_link,
    )


def free_blocks(
    events: list[ScheduleItem],
    day: date,
    tz: ZoneInfo,
    now: datetime,
    day_start_hour: int = 8,
    day_end_hour: int = 22,
    min_minutes: int = 30,
) -> list[FreeBlock]:
    window_start = datetime.combine(day, time(day_start_hour), tzinfo=tz)
    window_end = datetime.combine(day, time(day_end_hour), tzinfo=tz)
    cursor = max(window_start, now.astimezone(tz))
    blocks: list[FreeBlock] = []

    def add(start: datetime, end: datetime) -> None:
        if end - start >= timedelta(minutes=min_minutes):
            blocks.append(FreeBlock(start=start, end=end, minutes=int((end - start).total_seconds() // 60)))

    for event in sorted((e for e in events if not e.all_day), key=lambda e: e.start):
        if cursor >= window_end:
            break
        if event.end <= cursor:
            continue
        if event.start > cursor:
            add(cursor, min(event.start, window_end))
        cursor = max(cursor, event.end)
    if cursor < window_end:
        add(cursor, window_end)
    return blocks


def schedule_view(service: Any | None = None, now: datetime | None = None) -> ScheduleView:
    settings = get_settings()
    tz = ZoneInfo(settings.google_timezone)
    now = now or datetime.now(tz)
    today = now.astimezone(tz).date()
    events = [_item(e, tz) for e in summarize_day(today, service=service).events]
    tomorrow = [_item(e, tz) for e in summarize_day(today + timedelta(days=1), service=service).events]
    return ScheduleView(
        day=today,
        timezone=settings.google_timezone,
        day_start_hour=settings.day_start_hour,
        day_end_hour=settings.day_end_hour,
        events=events,
        tomorrow=tomorrow,
        free=free_blocks(events, today, tz, now, settings.day_start_hour, settings.day_end_hour),
    )


# ---------- needs attention ----------

Kind = Literal["deadline", "meeting", "reply"]
_KINDS = {"deadline", "meeting", "reply"}


class DayEvent(BaseModel):
    title: str
    start: datetime | None = None  # None = all day


class AttentionItem(BaseModel):
    id: str
    kind: Kind
    reason: str
    due: date | None = None
    urgent: bool = False
    sender_name: str
    subject: str
    received_at: datetime | None = None
    link: str
    day_events: list[DayEvent] = Field(default_factory=list)


class AttentionView(BaseModel):
    checked: int
    items: list[AttentionItem] = Field(default_factory=list)
    error: str | None = None


_TRIAGE_PROMPT = (
    "You triage a university student's inbox. Decide for every email whether it needs the student's attention. "
    "kind is one of: "
    "\"deadline\" (something must be done, submitted, paid, or registered for by a date), "
    "\"meeting\" (an invitation, a request to meet or call, or a scheduling question), "
    "\"reply\" (a real person asked a question or is clearly waiting on a response), "
    "\"none\" (newsletters, receipts, automated notifications, marketing, FYI-only). "
    "When unsure, use \"none\". "
    "reason: at most 12 words saying what the student needs to do, specific to the email. "
    "due: the ISO date (YYYY-MM-DD) of the deadline or meeting if stated or clearly implied relative to the "
    "email's received date (e.g. \"by Friday\"), otherwise null. Never guess a date. "
    "Return JSON: {\"items\": [{\"id\": ..., \"kind\": ..., \"reason\": ..., \"due\": ...}]} with one entry per email."
)


def _sender_name(raw: str) -> str:
    name, address = parseaddr(raw)
    return name or address.split("@")[0] or "Unknown sender"


def triage_emails(emails: list[EmailSummary], today: date, complete: Completer | None = None) -> dict[str, dict[str, Any]]:
    payload = {
        "today": today.isoformat(),
        "emails": [
            {
                "id": e.id,
                "from": e.sender,
                "subject": e.subject,
                "snippet": e.snippet[:240],
                "received": e.received_at.date().isoformat() if e.received_at else None,
            }
            for e in emails
        ],
    }
    data = complete_json(_TRIAGE_PROMPT, payload, max_tokens=80 + 45 * len(emails), complete=complete)
    by_id = {str(item.get("id")): item for item in data.get("items", []) if isinstance(item, dict)}
    verdicts = {}
    for email in emails:
        item = by_id.get(email.id, {})
        kind = item.get("kind") if item.get("kind") in _KINDS else "none"
        verdicts[email.id] = {"kind": kind, "reason": str(item.get("reason") or "")[:140], "due": item.get("due")}
    return verdicts


def _parse_due(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _day_events(day: date, tz: ZoneInfo, service: Any | None) -> list[DayEvent]:
    events = []
    for event in summarize_day(day, service=service).events:
        item = _item(event, tz)
        events.append(DayEvent(title=item.title, start=None if item.all_day else item.start))
    return events


def attention_view(
    service: Any | None = None,
    calendar_service: Any | None = None,
    now: datetime | None = None,
    complete: Completer | None = None,
) -> AttentionView:
    settings = get_settings()
    tz = ZoneInfo(settings.google_timezone)
    now = now or datetime.now(tz)
    today = now.astimezone(tz).date()
    query = (
        f"in:inbox newer_than:{ATTENTION_LOOKBACK_DAYS}d -category:promotions -category:social "
        f"-{sender_query(settings.news_senders)}"
    )
    emails = check_inbox(max_results=TRIAGE_BATCH, query=query, service=service)

    verdicts: dict[str, dict[str, Any]] = {}
    pending = []
    for email in emails:
        cached = cache_get(TRIAGE_CACHE + email.id)
        if cached is None:
            pending.append(email)
        else:
            verdicts[email.id] = cached
    error = None
    if pending:
        try:
            fresh = triage_emails(pending, today, complete=complete)
        except AIUnavailable as exc:
            error = str(exc)
        else:
            for message_id, verdict in fresh.items():
                cache_set(TRIAGE_CACHE + message_id, verdict)
            verdicts.update(fresh)

    items: list[AttentionItem] = []
    for email in emails:
        verdict = verdicts.get(email.id)
        if not verdict or verdict.get("kind") not in _KINDS:
            continue
        due = _parse_due(verdict.get("due"))
        if due and due < today and verdict["kind"] in {"deadline", "meeting"}:
            continue  # already passed; no longer actionable
        items.append(
            AttentionItem(
                id=email.id,
                kind=verdict["kind"],
                reason=verdict.get("reason") or email.subject,
                due=due,
                urgent=bool(due and due <= today + timedelta(days=1)),
                sender_name=_sender_name(email.sender),
                subject=email.subject or "(no subject)",
                received_at=email.received_at,
                link=f"https://mail.google.com/mail/u/0/#inbox/{email.thread_id or email.id}",
            )
        )

    # Link each item to what's already on the calendar that day.
    horizon = today + timedelta(days=ATTENTION_LOOKBACK_DAYS)
    days = sorted({i.due for i in items if i.due and today <= i.due <= horizon})[:LINKED_DAYS_MAX]
    calendar = {day: _day_events(day, tz, calendar_service) for day in days}
    for item in items:
        item.day_events = calendar.get(item.due, []) if item.due else []

    items.sort(key=lambda i: (not i.urgent, i.due or date.max, -(i.received_at.timestamp() if i.received_at else 0)))
    return AttentionView(checked=len(emails), items=items, error=error)


# ---------- daily brief ----------

class Briefing(BaseModel):
    day: date
    headline: str
    summary: str
    focus: list[str] = Field(default_factory=list)
    generated_at: datetime
    ai: bool


def _fmt_time(value: datetime) -> str:
    return value.strftime("%-I:%M %p").replace(":00 ", " ")


def fallback_briefing(schedule: ScheduleView, attention: AttentionView, now: datetime) -> Briefing:
    timed = [e for e in schedule.events if not e.all_day and e.end > now]
    if timed:
        headline = f"Next up: {timed[0].title} at {_fmt_time(timed[0].start)}"
    elif schedule.events:
        headline = "Nothing else on the calendar today"
    else:
        headline = "A clear calendar today"
    free_hours = sum(block.minutes for block in schedule.free) / 60
    count = len(attention.items)
    summary = (
        f"{len(schedule.events)} event{'s' if len(schedule.events) != 1 else ''} today, "
        f"{count} email{'s' if count != 1 else ''} needing attention, "
        f"and about {free_hours:.0f}h of open time."
    )
    focus = [item.reason for item in attention.items[:3]]
    return Briefing(day=schedule.day, headline=headline, summary=summary, focus=focus, generated_at=now, ai=False)


_BRIEF_PROMPT = (
    "You write a calm, useful morning brief for a busy university student. "
    "Use only the JSON data given; never invent events, emails, or deadlines. "
    "Connect the calendar and the inbox: e.g. point out a free block that fits an urgent reply, "
    "or a deadline that lands on a busy day. "
    "Return JSON with keys: headline (max 12 words, the single most important thing about today), "
    "summary (2 short sentences), focus (1-3 short concrete to-dos in imperative voice). "
    "Plain language, no emojis, no filler."
)


def _brief_context(schedule: ScheduleView, attention: AttentionView, news: NewsDigest | None, now: datetime) -> dict[str, Any]:
    return {
        "now": now.strftime("%A %B %-d, %-I:%M %p"),
        "today_events": [
            {"title": e.title, "start": "all day" if e.all_day else _fmt_time(e.start),
             "end": None if e.all_day else _fmt_time(e.end)}
            for e in schedule.events
        ],
        "free_blocks": [f"{_fmt_time(b.start)}–{_fmt_time(b.end)}" for b in schedule.free],
        "tomorrow": [
            {"title": e.title, "start": "all day" if e.all_day else _fmt_time(e.start)} for e in schedule.tomorrow[:3]
        ],
        "needs_attention": [
            {"kind": i.kind, "what": i.reason, "from": i.sender_name,
             "due": i.due.isoformat() if i.due else None, "urgent": i.urgent}
            for i in attention.items[:8]
        ],
        "news_headlines": [story.title for story in (news.stories if news else [])][:3],
    }


def cached_brief(day: date) -> Briefing | None:
    cached = cache_get(f"{BRIEF_CACHE}{day.isoformat()}")
    return Briefing.model_validate(cached) if cached else None


def build_brief(
    schedule: ScheduleView,
    attention: AttentionView,
    news: NewsDigest | None,
    now: datetime,
    complete: Completer | None = None,
) -> Briefing:
    """Ask the model for today's brief and cache it. Falls back to a plain summary (uncached) on failure."""
    try:
        data = complete_json(_BRIEF_PROMPT, _brief_context(schedule, attention, news, now), max_tokens=300, complete=complete)
        brief = Briefing(
            day=schedule.day,
            headline=str(data["headline"]),
            summary=str(data.get("summary", "")),
            focus=[str(item) for item in data.get("focus", []) if str(item).strip()][:3],
            generated_at=now,
            ai=True,
        )
    except (AIUnavailable, KeyError):
        return fallback_briefing(schedule, attention, now)
    cache_set(f"{BRIEF_CACHE}{schedule.day.isoformat()}", brief.model_dump(mode="json"))
    return brief
