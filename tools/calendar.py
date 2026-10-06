from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from config import get_settings
from models.schemas import CalendarEvent, CalendarSummary, EventInput
from tools.google_auth import build_google_service


def _service(service: Any | None) -> Any:
    return service or build_google_service("calendar", "v3")


def _event(item: dict[str, Any]) -> CalendarEvent:
    start = item.get("start", {})
    end = item.get("end", {})
    start_value = start.get("dateTime") or f"{start['date']}T00:00:00+00:00"
    end_value = end.get("dateTime") or f"{end['date']}T00:00:00+00:00"
    return CalendarEvent(
        id=item.get("id"), summary=item.get("summary", "(untitled)"),
        description=item.get("description"), start=datetime.fromisoformat(start_value.replace("Z", "+00:00")),
        end=datetime.fromisoformat(end_value.replace("Z", "+00:00")), location=item.get("location"),
        html_link=item.get("htmlLink"),
    )


def create_event(event: EventInput, service: Any | None = None) -> CalendarEvent:
    configured_timezone = ZoneInfo(get_settings().google_timezone)
    start = event.start if event.start.tzinfo else event.start.replace(tzinfo=configured_timezone)
    end = event.end if event.end.tzinfo else event.end.replace(tzinfo=configured_timezone)
    if end <= start:
        raise ValueError("event end must be after event start")
    body = {"summary": event.summary, "description": event.description, "location": event.location,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()}}
    if isinstance(start.tzinfo, ZoneInfo):
        body["start"]["timeZone"] = start.tzinfo.key
    if isinstance(end.tzinfo, ZoneInfo):
        body["end"]["timeZone"] = end.tzinfo.key
    result = _service(service).events().insert(calendarId=event.calendar_id, body=body).execute()
    return _event(result)


def delete_event(event_id: str, calendar_id: str = "primary", service: Any | None = None) -> None:
    if not event_id.strip():
        raise ValueError("event_id is required")
    _service(service).events().delete(calendarId=calendar_id, eventId=event_id).execute()


def summarize_day(
    day: date,
    calendar_id: str = "primary",
    service: Any | None = None,
    timezone_name: str | None = None,
) -> CalendarSummary:
    local_timezone = ZoneInfo(timezone_name or get_settings().google_timezone)
    start = datetime.combine(day, time.min, tzinfo=local_timezone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=local_timezone)
    response = _service(service).events().list(calendarId=calendar_id, timeMin=start.isoformat(),
                                                timeMax=end.isoformat(), singleEvents=True,
                                                orderBy="startTime").execute()
    events = [_event(item) for item in response.get("items", [])]
    return CalendarSummary(day=day, count=len(events), events=events)
