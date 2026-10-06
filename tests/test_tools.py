import base64
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

import agent
from models.schemas import EmailDraft, EventInput
from tools.calendar import create_event, delete_event, summarize_day
from tools.gmail import draft_email, read_email, summarize_inbox
from tools.subscriptions import get_subscription_tracker, scan_subscription_inbox


class Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class GmailMessageReader:
    def __init__(self, message):
        self.message = message

    def get(self, **kwargs):
        return Request(self.message)

    def list(self, **kwargs):
        return Request({"messages": [{"id": "1"}]})

    def attachments(self):
        return GmailAttachmentReader()


class GmailAttachmentReader:
    def get(self, **kwargs):
        return Request({"data": base64.urlsafe_b64encode(b"Large body text").decode().rstrip("=")})


class SubscriptionMessageReader:
    def __init__(self, messages, has_more=False):
        self.messages_by_id = {message["id"]: message for message in messages}
        self.has_more = has_more

    def list(self, **kwargs):
        response = {"messages": [{"id": key} for key in self.messages_by_id]}
        if self.has_more:
            response["nextPageToken"] = "next-page"
        return Request(response)

    def get(self, **kwargs):
        return Request(self.messages_by_id[kwargs["id"]])


class SubscriptionGmailService:
    def __init__(self, messages, has_more=False):
        self.message_reader = SubscriptionMessageReader(messages, has_more=has_more)

    def users(self):
        return self

    def messages(self):
        return self.message_reader


def _encoded(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _subscription_message(message_id, subject, body, sender, date_header):
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": date_header},
            ],
            "mimeType": "text/plain",
            "body": {"data": _encoded(body)},
        },
    }


class GmailDrafts:
    def create(self, **kwargs):
        return Request({"id": "draft-1"})


class GmailService:
    def __init__(self, message=None):
        self.message = message or {
            "id": "1",
            "threadId": "t1",
            "snippet": "Hello from email",
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "a@example.com"},
                    {"name": "To", "value": "b@example.com"},
                    {"name": "Subject", "value": "Hi"},
                ],
                "mimeType": "text/plain",
                "body": {"data": base64.urlsafe_b64encode(b"Hello from email").decode().rstrip("=")},
            },
        }

    def users(self):
        return self

    def messages(self):
        return GmailMessageReader(self.message)

    def drafts(self):
        return GmailDrafts()


class CalendarEvents:
    def __init__(self):
        self.list_kwargs = None
        self.insert_kwargs = None

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return Request({"id": "event-1", "summary": "Lunch",
                        "start": {"dateTime": "2026-09-23T12:00:00+00:00"},
                        "end": {"dateTime": "2026-09-23T13:00:00+00:00"}})

    def list(self, **kwargs):
        self.list_kwargs = kwargs
        return Request({"items": [{"id": "event-1", "summary": "Lunch",
                                   "start": {"dateTime": "2026-09-23T12:00:00+00:00"},
                                   "end": {"dateTime": "2026-09-23T13:00:00+00:00"}}]})


class CalendarService:
    def __init__(self):
        self.event_list = CalendarEvents()

    def events(self):
        return self.event_list


def test_gmail_tools():
    service = GmailService()
    summary = summarize_inbox(service=service)
    assert summary.unread_count == 1
    assert summary.emails[0].subject == "Hi"
    assert draft_email(EmailDraft(to=["b@example.com"], body="Hello"), service=service)["id"] == "draft-1"


def test_read_email_decodes_plain_text_body_and_headers():
    email = read_email("1", service=GmailService())

    assert email.sender == "a@example.com"
    assert email.to == "b@example.com"
    assert email.subject == "Hi"
    assert email.body == "Hello from email"
    assert email.attachments == []


def test_read_email_prefers_plain_text_and_lists_attachments():
    plain = base64.urlsafe_b64encode(b"Readable plain body").decode().rstrip("=")
    html = base64.urlsafe_b64encode(b"<p>HTML body</p>").decode().rstrip("=")
    service = GmailService({
        "id": "1",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [],
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": plain}},
                        {"mimeType": "text/html", "body": {"data": html}},
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "agenda.pdf",
                    "body": {"size": 2048, "attachmentId": "attachment-1"},
                },
            ],
        },
    })

    email = read_email("1", service=service)

    assert email.body == "Readable plain body"
    assert len(email.attachments) == 1
    assert email.attachments[0].filename == "agenda.pdf"
    assert email.attachments[0].mime_type == "application/pdf"
    assert email.attachments[0].size_bytes == 2048


def test_read_email_uses_cleaned_html_when_plain_text_is_missing():
    html = base64.urlsafe_b64encode(
        b"<html><style>.hidden{}</style><body><p>Hello<br>world</p><script>ignore()</script></body></html>"
    ).decode().rstrip("=")
    service = GmailService({
        "id": "1",
        "payload": {"mimeType": "text/html", "headers": [],
                    "body": {"data": html}},
    })

    email = read_email("1", service=service)

    assert email.body == "Hello\nworld"


def test_read_email_fetches_body_stored_as_gmail_attachment():
    service = GmailService({
        "id": "1",
        "payload": {"mimeType": "text/plain", "headers": [],
                    "body": {"attachmentId": "body-attachment"}},
    })
    email = read_email("1", service=service)

    assert email.body == "Large body text"


def test_subscription_scan_extracts_payment_cycle_and_persists(tmp_path):
    service = SubscriptionGmailService([
        _subscription_message(
            "sub-1",
            "Your monthly subscription payment was received",
            "Your $12.99 monthly plan payment was successful. Next billing date: November 5, 2026.",
            "StreamCo <billing@streamco.example>",
            "Mon, 5 Oct 2026 09:00:00 -0400",
        )
    ])
    database_path = str(tmp_path / "assistant.db")

    result = scan_subscription_inbox(
        service=service,
        database_path=database_path,
        today=date(2026, 10, 5),
    )

    assert result.scanned_emails == 1
    assert result.matched_emails == 1
    assert len(result.subscriptions) == 1
    subscription = result.subscriptions[0]
    assert subscription.merchant == "StreamCo"
    assert subscription.amount == Decimal("12.99")
    assert subscription.currency == "USD"
    assert subscription.billing_cycle.value == "monthly"
    assert subscription.status.value == "active"
    assert subscription.next_due_date == date(2026, 11, 5)
    assert subscription.due_date_is_estimate is False
    assert subscription.source_email_ids == ["sub-1"]


def test_subscription_tracker_distinguishes_not_scanned_from_empty_scan(tmp_path):
    database_path = str(tmp_path / "assistant.db")

    initial_view = get_subscription_tracker(database_path)
    assert initial_view.scan_completed is False
    assert initial_view.subscriptions == []

    result = scan_subscription_inbox(
        service=SubscriptionGmailService([]),
        database_path=database_path,
        today=date(2026, 10, 5),
    )
    completed_view = get_subscription_tracker(database_path)

    assert result.scanned_emails == 0
    assert completed_view.scan_completed is True
    assert completed_view.last_scan_at is not None
    assert completed_view.last_scanned_emails == 0
    assert completed_view.subscriptions == []


def test_subscription_scan_flags_cancellation_and_does_not_infer_due_date(tmp_path):
    service = SubscriptionGmailService([
        _subscription_message(
            "sub-cancel",
            "Your subscription was cancelled",
            "Your yearly membership was cancelled and will not renew.",
            "Example Service <support@example.test>",
            "Mon, 5 Oct 2026 09:00:00 -0400",
        )
    ])

    result = scan_subscription_inbox(
        service=service,
        database_path=str(tmp_path / "assistant.db"),
        today=date(2026, 10, 5),
    )

    subscription = result.subscriptions[0]
    assert subscription.status.value == "inactive"
    assert subscription.billing_cycle.value == "yearly"
    assert subscription.next_due_date is None


def test_subscription_tracker_does_not_call_cancellation_policy_inactive(tmp_path):
    service = SubscriptionGmailService([
        _subscription_message(
            "sub-policy",
            "Monthly subscription details",
            "Read our cancellation policy before selecting a plan.",
            "Example Service <support@example.test>",
            "Mon, 5 Oct 2026 09:00:00 -0400",
        )
    ])

    result = scan_subscription_inbox(
        service=service,
        database_path=str(tmp_path / "assistant.db"),
        today=date(2026, 10, 5),
    )

    assert result.subscriptions[0].status.value == "unknown"
    assert result.subscriptions[0].next_due_date is None


def test_subscription_tracker_extracts_explicit_due_date_without_marking_payment(tmp_path):
    service = SubscriptionGmailService([
        _subscription_message(
            "sub-due",
            "Your monthly subscription renews soon",
            "Your subscription is active and the next billing date is November 5, 2026.",
            "StreamCo <billing@streamco.example>",
            "Mon, 5 Oct 2026 09:00:00 -0400",
        )
    ])

    result = scan_subscription_inbox(
        service=service,
        database_path=str(tmp_path / "assistant.db"),
        today=date(2026, 10, 5),
    )

    subscription = result.subscriptions[0]
    assert subscription.status.value == "active"
    assert subscription.last_payment_date is None
    assert subscription.next_due_date == date(2026, 11, 5)
    assert subscription.due_date_is_estimate is False


def test_subscription_scan_is_repeatable_without_duplicate_records(tmp_path):
    database_path = str(tmp_path / "assistant.db")
    service = SubscriptionGmailService([
        _subscription_message(
            "sub-repeat",
            "Monthly subscription payment received",
            "Your $9.99 monthly subscription payment was successful.",
            "StreamCo <billing@streamco.example>",
            "Mon, 5 Oct 2026 09:00:00 -0400",
        )
    ])

    first = scan_subscription_inbox(service=service, database_path=database_path, today=date(2026, 10, 5))
    second = scan_subscription_inbox(service=service, database_path=database_path, today=date(2026, 10, 5))

    assert first.matched_emails == 1
    assert len(second.subscriptions) == 1
    assert second.subscriptions[0].source_email_ids == ["sub-repeat"]


def test_subscription_scan_reports_when_result_limit_truncates_search(tmp_path):
    service = SubscriptionGmailService(
        [
            _subscription_message(
                "sub-limited",
                "Monthly subscription",
                "Your monthly membership is active.",
                "Example Service <support@example.test>",
                "Mon, 5 Oct 2026 09:00:00 -0400",
            )
        ],
        has_more=True,
    )

    result = scan_subscription_inbox(
        max_results=1,
        service=service,
        database_path=str(tmp_path / "assistant.db"),
        today=date(2026, 10, 5),
    )

    assert result.scanned_emails == 1
    assert result.truncated is True


def test_subscription_scan_estimates_next_due_date_from_payment(tmp_path):
    service = SubscriptionGmailService([
        _subscription_message(
            "sub-year",
            "Annual membership renewed",
            "Payment successful: EUR 120.00 for your annual membership.",
            "Example Service <billing@example.test>",
            "Thu, 5 Oct 2023 09:00:00 -0400",
        )
    ])

    result = scan_subscription_inbox(
        service=service,
        database_path=str(tmp_path / "assistant.db"),
        today=date(2026, 10, 5),
    )

    subscription = result.subscriptions[0]
    assert subscription.billing_cycle.value == "yearly"
    assert subscription.next_due_date == date(2026, 10, 5)
    assert subscription.due_date_is_estimate is True
    assert subscription.status.value == "unknown"



def test_calendar_tools():
    service = CalendarService()
    event = create_event(EventInput(summary="Lunch",
                                    start=datetime(2026, 9, 23, 12, tzinfo=timezone.utc),
                                    end=datetime(2026, 9, 23, 13, tzinfo=timezone.utc)), service=service)
    assert event.id == "event-1"
    assert summarize_day(date(2026, 9, 23), service=service,
                         timezone_name="America/New_York").count == 1


def test_summarize_day_uses_eastern_local_day_boundaries():
    service = CalendarService()

    summarize_day(date(2026, 3, 8), service=service, timezone_name="America/New_York")

    assert service.event_list.list_kwargs["timeMin"] == "2026-03-08T00:00:00-05:00"
    assert service.event_list.list_kwargs["timeMax"] == "2026-03-09T00:00:00-04:00"


def test_create_event_interprets_naive_times_in_configured_timezone(monkeypatch):
    monkeypatch.setattr(
        "tools.calendar.get_settings",
        lambda: type("Settings", (), {"google_timezone": "America/New_York"})(),
    )
    service = CalendarService()
    event = EventInput(
        summary="Lunch",
        start=datetime(2026, 9, 23, 12),
        end=datetime(2026, 9, 23, 13),
    )

    create_event(event, service=service)

    body = service.event_list.insert_kwargs["body"]
    assert body["start"]["dateTime"] == "2026-09-23T12:00:00-04:00"
    assert body["start"]["timeZone"] == "America/New_York"
    assert body["end"]["dateTime"] == "2026-09-23T13:00:00-04:00"


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2026, 9, 23, 13, tzinfo=timezone.utc),
         datetime(2026, 9, 23, 12, tzinfo=timezone.utc)),
        (datetime(2026, 9, 23, 12, tzinfo=timezone.utc),
         datetime(2026, 9, 23, 12, tzinfo=timezone.utc)),
    ],
)
def test_create_event_rejects_non_positive_duration(start, end):
    event = EventInput(summary="Invalid event", start=start, end=end)

    with pytest.raises(ValueError, match="event end must be after event start"):
        create_event(event, service=CalendarService())


def test_delete_event_requires_an_event_id():
    with pytest.raises(ValueError, match="event_id is required"):
        delete_event("  ", service=CalendarService())


def test_build_agent_configures_openai_sdk_key(monkeypatch):
    configured_keys = []
    monkeypatch.setattr(
        agent,
        "get_settings",
        lambda: type("Settings", (), {
            "openai_api_key": "test-key",
            "openai_model": "test-model",
        })(),
    )
    monkeypatch.setattr(agent, "set_default_openai_key", configured_keys.append)

    assistant = agent.build_agent()

    assert assistant.name == "Personal Assistant"
    assert configured_keys == ["test-key"]


def test_build_agent_requires_openai_key(monkeypatch):
    monkeypatch.setattr(
        agent,
        "get_settings",
        lambda: type("Settings", (), {
            "openai_api_key": None,
            "openai_model": "test-model",
        })(),
    )

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is missing"):
        agent.build_agent()


@pytest.mark.asyncio
async def test_cli_preserves_conversation_history(monkeypatch):
    import app

    calls = []
    messages = iter(["Show my subscriptions", "yes", "exit"])

    class Result:
        def __init__(self, final_output, history):
            self.final_output = final_output
            self.history = history

        def to_input_list(self):
            return self.history

    async def fake_run(assistant, input_items):
        calls.append(input_items)
        current_user_message = input_items[-1]["content"]
        return Result(
            "Assistant response",
            input_items + [{"role": "assistant", "content": f"Response to {current_user_message}"}],
        )

    monkeypatch.setattr(app, "build_agent", lambda: object())
    monkeypatch.setattr(app.Runner, "run", fake_run)
    monkeypatch.setattr("builtins.input", lambda _: next(messages))
    monkeypatch.setattr("builtins.print", lambda *args: None)

    await app.main()

    assert calls[1][0] == {"role": "user", "content": "Show my subscriptions"}
    assert calls[1][1] == {
        "role": "assistant",
        "content": "Response to Show my subscriptions",
    }
    assert calls[1][2] == {"role": "user", "content": "yes"}
