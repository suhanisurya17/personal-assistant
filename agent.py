from pathlib import Path

from agents import Agent, function_tool, set_default_openai_key

from config import get_settings
from models.schemas import EmailDraft, EventInput
from tools.calendar import create_event, delete_event, summarize_day
from tools.gmail import check_inbox, draft_email, read_email, summarize_inbox
from tools.subscriptions import get_subscription_tracker, scan_subscription_inbox


@function_tool
def inbox(max_results: int = 20) -> str:
    """List recent inbox messages."""
    return summarize_inbox(max_results=max_results).model_dump_json()


@function_tool
def email(message_id: str) -> str:
    """Read a Gmail message by ID, including readable body text and attachment details."""
    return read_email(message_id).model_dump_json()


@function_tool
def create_gmail_draft(to: list[str], subject: str, body: str) -> str:
    """Create, but never send, a Gmail draft."""
    return str(draft_email(EmailDraft(to=to, subject=subject, body=body)))


@function_tool
def today_calendar(day: str) -> str:
    """Summarize events for an ISO date."""
    from datetime import date
    return summarize_day(date.fromisoformat(day)).model_dump_json()


@function_tool
def add_calendar_event(summary: str, start: str, end: str, description: str | None = None) -> str:
    """Create a calendar event from ISO datetimes."""
    from datetime import datetime
    return create_event(EventInput(summary=summary, start=datetime.fromisoformat(start),
                                   end=datetime.fromisoformat(end), description=description)).model_dump_json()


@function_tool
def remove_calendar_event(event_id: str) -> str:
    """Delete a calendar event after the user explicitly confirms."""
    delete_event(event_id)
    return f"Deleted calendar event {event_id}."


@function_tool
def scan_subscriptions(max_results: int = 100) -> str:
    """Scan relevant inbox messages for subscriptions and save extracted tracker records locally. Status and due dates may be uncertain; report evidence and estimates clearly."""
    return scan_subscription_inbox(max_results=max_results).model_dump_json()


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
                        remove_calendar_event, scan_subscriptions, show_subscriptions])
