import json
from pathlib import Path
from typing import Callable

from agents import Agent, function_tool, set_default_openai_key
from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import HttpError

from config import get_settings
from models.schemas import EmailDraft, EventInput
from tools.calendar import create_event, delete_event, summarize_day
from tools.gmail import check_inbox, draft_email, read_email, summarize_inbox
from tools.subscriptions import (
    find_tracked_subscriptions,
    get_subscription_tracker,
    scan_subscription_inbox,
)
from tools.google_auth import GoogleReauthorizationRequired


def _run_google_operation(operation: Callable[[], str]) -> str:
    try:
        return operation()
    except GoogleReauthorizationRequired as error:
        return json.dumps(
            {
                "error": "google_reauthorization_required",
                "message": str(error),
            }
        )
    except GoogleAuthError as error:
        return json.dumps(
            {
                "error": "google_authentication_error",
                "message": f"Google authentication failed ({type(error).__name__}).",
            }
        )
    except HttpError as error:
        status = error.resp.status
        return json.dumps(
            {
                "error": "google_api_error",
                "status": status,
                "message": f"Google API returned HTTP {status}.",
            }
        )


@function_tool
def inbox(max_results: int = 20) -> str:
    """List recent inbox messages."""
    return _run_google_operation(lambda: summarize_inbox(max_results=max_results).model_dump_json())


@function_tool
def email(message_id: str) -> str:
    """Read a Gmail message by ID, including readable body text and attachment details."""
    return _run_google_operation(lambda: read_email(message_id).model_dump_json())


@function_tool
def create_gmail_draft(to: list[str], subject: str, body: str) -> str:
    """Create, but never send, a Gmail draft."""
    return _run_google_operation(
        lambda: str(draft_email(EmailDraft(to=to, subject=subject, body=body)))
    )


@function_tool
def today_calendar(day: str) -> str:
    """Summarize events for an ISO date."""
    from datetime import date
    return _run_google_operation(
        lambda: summarize_day(date.fromisoformat(day)).model_dump_json()
    )


@function_tool
def add_calendar_event(summary: str, start: str, end: str, description: str | None = None) -> str:
    """Create a calendar event from ISO datetimes."""
    from datetime import datetime
    return _run_google_operation(
        lambda: create_event(
            EventInput(
                summary=summary,
                start=datetime.fromisoformat(start),
                end=datetime.fromisoformat(end),
                description=description,
            )
        ).model_dump_json()
    )


@function_tool
def remove_calendar_event(event_id: str) -> str:
    """Delete a calendar event after the user explicitly confirms."""
    def delete() -> str:
        delete_event(event_id)
        return f"Deleted calendar event {event_id}."

    return _run_google_operation(delete)


@function_tool
def find_subscription(merchant: str) -> str:
    """Find saved tracker records for a merchant. This does not access Gmail or start a scan."""
    records = find_tracked_subscriptions(merchant)
    return json.dumps(
        {
            "merchant": merchant,
            "found": bool(records),
            "subscriptions": [record.model_dump(mode="json") for record in records],
        }
    )


@function_tool
def scan_subscriptions(max_results: int = 100, merchant: str | None = None) -> str:
    """Scan relevant inbox messages and save extracted subscription records locally. Set merchant to search for one named provider. Status and due dates may be uncertain; report evidence and estimates clearly."""
    return _run_google_operation(
        lambda: scan_subscription_inbox(
            max_results=max_results,
            merchant=merchant,
        ).model_dump_json()
    )


@function_tool
def show_subscriptions() -> str:
    """Show whether the tracker has scanned the inbox and list its saved subscription records."""
    return get_subscription_tracker().model_dump_json()


def build_agent() -> Agent:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is missing. Add it to .env or export it in your shell."
        )
    set_default_openai_key(settings.openai_api_key)
    prompt_path = Path(__file__).parent / "prompts" / "system_prompt.txt"
    return Agent(name="Personal Assistant", instructions=prompt_path.read_text(),
                 model=settings.openai_model,
                 tools=[inbox, email, create_gmail_draft, today_calendar, add_calendar_event,
                        remove_calendar_event, find_subscription, scan_subscriptions,
                        show_subscriptions])
