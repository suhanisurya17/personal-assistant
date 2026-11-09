import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import server
import tools.dashboard as dashboard
import tools.news as news
from models.schemas import CalendarEvent, CalendarSummary, EmailMessage, EmailSummary
from tools.ai import AIUnavailable
from tools.dashboard import (
    AttentionItem,
    AttentionView,
    ScheduleItem,
    ScheduleView,
    attention_view,
    build_brief,
    cached_brief,
    fallback_briefing,
    free_blocks,
)
from tools.news import clean_newsletter_text, latest_newsletter, parse_tldr, sender_query

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 9, 9, 0, tzinfo=TZ)

TLDR_BODY = """TLDR 2026-11-09

Big Tech & Startups

APPLE SHIPS NEW AI CHIP FOR MACS (3 MINUTE READ) [1]

Apple announced a chip focused on on-device models.
It ships next month.

BUILD FASTER WITH ACME (SPONSOR) [2]

Ad copy here.

Science & Futuristic Technology

NASA CONFIRMS WATER ON MOON SITE (5 MINUTE READ) [3]

Researchers found ice deposits.

Links:
[1] https://example.com/apple
[2] https://example.com/ad
[3] https://example.com/nasa
"""


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Every test gets its own SQLite cache."""
    monkeypatch.setattr(server.get_settings(), "database_path", str(tmp_path / "test.db"))


def _event(title, start_h, end_h, start_m=0, end_m=0):
    return ScheduleItem(
        title=title,
        start=datetime(2026, 11, 9, start_h, start_m, tzinfo=TZ),
        end=datetime(2026, 11, 9, end_h, end_m, tzinfo=TZ),
    )


class Counter:
    """Fake model: records calls and returns a canned JSON reply."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def __call__(self, system, user, max_tokens):
        self.calls.append(json.loads(user) if user.startswith("{") else user)
        return json.dumps(self.reply(self.calls[-1]) if callable(self.reply) else self.reply)


# ---------- news ----------

def test_parse_tldr_extracts_top_stories_and_skips_sponsors():
    stories = parse_tldr(TLDR_BODY)
    assert [s.title for s in stories] == ["Apple ships new AI chip for macs", "NASA confirms water on moon site"]
    assert stories[0].url == "https://example.com/apple"
    assert stories[0].summary == "Apple announced a chip focused on on-device models. It ships next month."


def test_parse_tldr_returns_nothing_for_free_form_prose():
    assert parse_tldr("Wall Street loves a gory metaphor.\nThe silver lining? So far.") == []


def test_clean_newsletter_text_strips_tracking_links_and_invisible_chars():
    raw = "Founders‌ ‌\nA sell-off \n( https://clicks.example.com/f/a/xyz~~/abc\n, then a quake."
    assert clean_newsletter_text(raw) == "Founders \nA sell-off \n , then a quake."


def test_sender_query_builds_gmail_or_group():
    assert sender_query(['"TLDR by Wealthsimple"', "tldrnewsletter.com"]) == \
        '{from:"TLDR by Wealthsimple" from:tldrnewsletter.com}'


def _fake_gmail(monkeypatch, body):
    issue = EmailSummary(id="issue1", sender="TLDR by Wealthsimple <n@ws.com>", subject="Sell-off")
    monkeypatch.setattr(news, "check_inbox", lambda **kwargs: [issue])
    reads = []

    def read(message_id, service=None):
        reads.append(message_id)
        return EmailMessage(id=message_id, sender=issue.sender, subject=issue.subject, body=body)

    monkeypatch.setattr(news, "read_email", read)
    return reads


def test_newsletter_uses_ai_once_per_issue_then_cache(monkeypatch):
    reads = _fake_gmail(monkeypatch, "Long free-form newsletter prose about markets.")
    model = Counter({"stories": [{"title": "AI stocks fall", "summary": "Big drop."}, {"title": ""}]})

    first = latest_newsletter(["x"], complete=model)
    second = latest_newsletter(["x"], complete=model)

    assert [s.title for s in first.stories] == ["AI stocks fall"]
    assert second == first
    assert len(model.calls) == 1 and reads == ["issue1"]


def test_newsletter_ai_failure_is_reported_and_not_cached(monkeypatch):
    _fake_gmail(monkeypatch, "Prose.")

    def broken(system, user, max_tokens):
        raise RuntimeError("quota")

    assert latest_newsletter(["x"], complete=broken).error == "Model call failed (RuntimeError)."
    model = Counter({"stories": [{"title": "Recovered", "summary": ""}]})
    assert latest_newsletter(["x"], complete=model).stories[0].title == "Recovered"


# ---------- schedule ----------

def test_free_blocks_finds_gaps_and_skips_past_time():
    events = [_event("Lecture", 9, 10, end_m=30), _event("Lab", 13, 15), _event("Club", 18, 19)]
    blocks = free_blocks(events, date(2026, 11, 9), TZ, datetime(2026, 11, 9, 11, tzinfo=TZ))
    assert [(b.start.hour, b.end.hour, b.minutes) for b in blocks] == [(11, 13, 120), (15, 18, 180), (19, 22, 180)]


def test_free_blocks_ignores_short_gaps_and_overlaps():
    events = [_event("A", 8, 10), _event("B", 9, 12), _event("C", 12, 22, start_m=20)]
    assert free_blocks(events, date(2026, 11, 9), TZ, datetime(2026, 11, 9, 7, tzinfo=TZ)) == []


# ---------- needs attention ----------

EMAILS = [
    EmailSummary(id="e1", thread_id="t1", sender="Prof. Lee <lee@uni.edu>", subject="PS4 due Tuesday"),
    EmailSummary(id="e2", sender="Sam <sam@example.com>", subject="Coffee chat?"),
    EmailSummary(id="e3", sender="Store <no-reply@store.com>", subject="Your receipt"),
    EmailSummary(id="e4", sender="Old <o@uni.edu>", subject="Form due last week"),
]
VERDICTS = {"items": [
    {"id": "e1", "kind": "deadline", "reason": "Submit problem set 4", "due": "2026-11-10"},
    {"id": "e2", "kind": "meeting", "reason": "Pick a time for coffee", "due": "2026-11-12"},
    {"id": "e3", "kind": "none", "reason": "", "due": None},
    {"id": "e4", "kind": "deadline", "reason": "Old form", "due": "2026-11-01"},
]}


@pytest.fixture
def fake_google(monkeypatch):
    monkeypatch.setattr(dashboard, "check_inbox", lambda **kwargs: list(EMAILS))

    def day(d, service=None):
        events = []
        if d == date(2026, 11, 10):
            events = [CalendarEvent(summary="Lab", start=datetime(2026, 11, 10, 18, tzinfo=TZ),
                                    end=datetime(2026, 11, 10, 20, tzinfo=TZ))]
        return CalendarSummary(day=d, count=len(events), events=events)

    monkeypatch.setattr(dashboard, "summarize_day", day)


def test_attention_classifies_links_calendar_and_flags_urgent(fake_google):
    view = attention_view(now=NOW, complete=Counter(VERDICTS))
    assert [i.id for i in view.items] == ["e1", "e2"]  # receipt and past deadline dropped
    first, second = view.items
    assert first.urgent and first.kind == "deadline" and first.link.endswith("#inbox/t1")
    assert [e.title for e in first.day_events] == ["Lab"]
    assert not second.urgent and second.day_events == []
    assert view.checked == 4 and view.error is None


def test_attention_never_reclassifies_cached_emails(fake_google):
    model = Counter(VERDICTS)
    attention_view(now=NOW, complete=model)
    attention_view(now=NOW, complete=model)
    assert len(model.calls) == 1
    assert [e["id"] for e in model.calls[0]["emails"]] == ["e1", "e2", "e3", "e4"]


def test_attention_reports_ai_failure_without_caching(fake_google):
    def broken(system, user, max_tokens):
        raise RuntimeError("down")

    view = attention_view(now=NOW, complete=broken)
    assert view.items == [] and view.error
    assert len(attention_view(now=NOW, complete=Counter(VERDICTS)).items) == 2


# ---------- brief ----------

def _views():
    schedule = ScheduleView(day=date(2026, 11, 9), timezone="America/New_York", events=[_event("Lecture", 14, 15)])
    attention = AttentionView(checked=1, items=[AttentionItem(
        id="e1", kind="deadline", reason="Submit PS4", due=date(2026, 11, 10), urgent=True,
        sender_name="Prof. Lee", subject="PS4", link="https://mail.google.com/")])
    return schedule, attention


def test_brief_is_generated_once_and_cached_for_the_day():
    schedule, attention = _views()
    model = Counter({"headline": "Lecture at 2", "summary": "Do PS4 first.", "focus": ["Submit PS4", ""]})
    brief = build_brief(schedule, attention, None, NOW, complete=model)
    assert brief.ai and brief.focus == ["Submit PS4"]
    assert model.calls[0]["needs_attention"][0]["urgent"] is True
    assert cached_brief(date(2026, 11, 9)) == brief
    assert cached_brief(date(2026, 11, 10)) is None


def test_brief_falls_back_without_caching_when_ai_fails():
    schedule, attention = _views()
    brief = build_brief(schedule, attention, None, NOW, complete=lambda s, u, m: "not json")
    assert not brief.ai and brief.headline == "Next up: Lecture at 2 PM" and brief.focus == ["Submit PS4"]
    assert cached_brief(date(2026, 11, 9)) is None


# ---------- server ----------

@pytest.fixture
def client():
    return TestClient(server.app)


def test_index_and_layout_round_trip(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/layout").json() == {"layout": None}
    layout = [{"id": "news", "size": 3, "hidden": False}]
    assert client.put("/api/layout", json={"layout": layout}).json() == {"ok": True}
    assert client.get("/api/layout").json() == {"layout": layout}


def test_brief_endpoint_serves_cache_without_touching_google_or_model(client, monkeypatch):
    schedule, attention = _views()
    build_brief(schedule, attention, None, NOW, complete=Counter({"headline": "Cached", "summary": "", "focus": []}))
    monkeypatch.setattr(server, "_now", lambda: NOW)

    def must_not_run(*args, **kwargs):
        raise AssertionError("brief should come from cache")

    monkeypatch.setattr(server, "schedule_view", must_not_run)
    monkeypatch.setattr(server, "build_brief", must_not_run)
    assert client.get("/api/brief").json()["headline"] == "Cached"


def test_google_reauth_error_becomes_readable_json(client, monkeypatch):
    from tools.google_auth import GoogleReauthorizationRequired

    def boom():
        raise GoogleReauthorizationRequired("Sign in again.")

    monkeypatch.setattr(server, "attention_view", boom)
    response = client.get("/api/attention")
    assert response.status_code == 401
    assert response.json()["detail"] == {"code": "google_reauthorization_required", "message": "Sign in again."}
