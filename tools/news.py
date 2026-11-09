"""Top stories from the latest newsletter issue in Gmail (TLDR by Wealthsimple / tldr.tech).

tldr.tech issues have a fixed "(N minute read)" format and are parsed for free.
Free-form issues (TLDR by Wealthsimple) are summarized by the model once per
issue and cached, so each issue costs a single small request.
"""
import re
from typing import Any

from pydantic import BaseModel, Field

from tools.ai import AIUnavailable, Completer, cache_get, cache_set, complete_json
from tools.gmail import check_inbox, read_email

_READ_TIME = re.compile(r"\((\d+)\s*minute\s*read\)", re.IGNORECASE)
_LINK_REF = re.compile(r"\[(\d+)\]")
_LINK_LINE = re.compile(r"^\[(\d+)\]\s*(https?://\S+)")
_SPONSOR = re.compile(r"\(sponsor\)|sponsored", re.IGNORECASE)
_INLINE_URL = re.compile(r"\(?\s*https?://\S+\s*\)?")
_INVISIBLE = re.compile(r"[​-‏⁠﻿͏­]")
_ACRONYMS = {
    "AI", "API", "APIS", "AR", "AWS", "CEO", "CPU", "EU", "EV", "FTC", "GPU", "GPUS", "IPO",
    "IOS", "LLM", "LLMS", "ML", "NASA", "OS", "SEC", "SDK", "UI", "UK", "US", "USA", "VR",
    "VC", "XAI", "AGI", "SAAS",
}
NEWS_CACHE_VERSION = "news:v1:"
MAX_BODY_CHARS = 9000


class NewsStory(BaseModel):
    title: str
    summary: str = ""
    section: str = ""
    read_minutes: int | None = None
    url: str | None = None


class NewsDigest(BaseModel):
    source: str = ""
    issue_subject: str = ""
    issue_id: str | None = None
    stories: list[NewsStory] = Field(default_factory=list)
    error: str | None = None


def sender_query(senders: list[str]) -> str:
    """Gmail OR-group for the newsletter senders, e.g. {from:a from:"b c"}."""
    return "{" + " ".join(f"from:{sender}" for sender in senders) + "}"


def _display_title(raw: str) -> str:
    """tldr.tech shouts its headlines; turn ALL CAPS into sentence case but keep acronyms."""
    if raw.upper() != raw:
        return raw
    words = []
    for index, word in enumerate(raw.split()):
        core = re.sub(r"[^A-Za-z]", "", word)
        if core.upper() in _ACRONYMS or (core and not re.search(r"[AEIOUY]", core.upper())):
            words.append(word)
        elif index == 0:
            words.append(word.capitalize())
        else:
            words.append(word.lower())
    return " ".join(words)


def parse_tldr(body: str, limit: int = 3) -> list[NewsStory]:
    links = {}
    for line in body.splitlines():
        match = _LINK_LINE.match(line.strip())
        if match:
            links[match.group(1)] = match.group(2)

    merged: list[str] = []
    for line in (raw.strip() for raw in body.splitlines()):
        # HTML-derived text sometimes puts "(3 minute read)" on its own line; glue it back on.
        if merged and _READ_TIME.fullmatch(line) and merged[-1]:
            merged[-1] = f"{merged[-1]} {line}"
        else:
            merged.append(line)

    stories: list[NewsStory] = []
    section = ""
    index = 0
    while index < len(merged) and len(stories) < limit:
        line = merged[index]
        read_time = _READ_TIME.search(line)
        if not read_time:
            if line and len(line) < 40 and not _LINK_LINE.match(line) and not line.endswith("."):
                section = line
            index += 1
            continue
        if _SPONSOR.search(line):
            index += 1
            continue
        ref = _LINK_REF.search(line[read_time.end():])
        title = _LINK_REF.sub("", line[: read_time.start()]).strip(" -–—:")
        summary_lines: list[str] = []
        cursor = index + 1
        while cursor < len(merged) and not merged[cursor]:
            cursor += 1
        while cursor < len(merged) and merged[cursor] and not _READ_TIME.search(merged[cursor]):
            summary_lines.append(merged[cursor])
            cursor += 1
        summary = _LINK_REF.sub("", " ".join(summary_lines)).strip()
        if title:
            stories.append(
                NewsStory(
                    title=_display_title(title),
                    summary=summary[:320] + ("…" if len(summary) > 320 else ""),
                    section=section,
                    read_minutes=int(read_time.group(1)),
                    url=links.get(ref.group(1)) if ref else None,
                )
            )
        index = cursor
    return stories


def clean_newsletter_text(body: str) -> str:
    text = _INVISIBLE.sub("", body)
    text = _INLINE_URL.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()[:MAX_BODY_CHARS]


_EXTRACT_PROMPT = (
    "You pick the biggest news from a newsletter issue for a busy university student. "
    "Use only the text given. Skip ads, sponsor blurbs, referral pitches, and sign-off chatter. "
    "Return JSON: {\"stories\": [{\"title\": max 10 words, plain and specific, "
    "\"summary\": one or two sentences explaining what happened and why it matters}]} "
    "with the {limit} most important stories, most important first. No emojis."
)


def extract_stories_ai(body: str, limit: int = 3, complete: Completer | None = None) -> list[NewsStory]:
    data = complete_json(_EXTRACT_PROMPT.replace("{limit}", str(limit)), clean_newsletter_text(body),
                         max_tokens=450, complete=complete)
    stories = []
    for item in data.get("stories", [])[:limit]:
        if isinstance(item, dict) and str(item.get("title", "")).strip():
            stories.append(NewsStory(title=str(item["title"]).strip(), summary=str(item.get("summary", "")).strip()))
    return stories


def latest_newsletter(
    senders: list[str],
    limit: int = 3,
    service: Any | None = None,
    complete: Completer | None = None,
) -> NewsDigest:
    issues = check_inbox(max_results=1, query=f"{sender_query(senders)} newer_than:8d", service=service)
    if not issues:
        return NewsDigest()
    summary = issues[0]
    cache_key = f"{NEWS_CACHE_VERSION}{summary.id}:{limit}"
    cached = cache_get(cache_key)
    if cached:
        return NewsDigest.model_validate(cached)

    issue = read_email(summary.id, service=service)
    digest = NewsDigest(source=issue.sender, issue_subject=issue.subject, issue_id=issue.id)
    digest.stories = parse_tldr(issue.body, limit=limit)
    if not digest.stories:
        try:
            digest.stories = extract_stories_ai(issue.body, limit=limit, complete=complete)
        except AIUnavailable as error:
            digest.error = str(error)
            return digest  # don't cache failures; try again next load
    cache_set(cache_key, digest.model_dump(mode="json"))
    return digest
