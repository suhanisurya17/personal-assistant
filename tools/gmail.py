import base64
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from models.schemas import EmailAttachment, EmailDraft, EmailMessage as GmailEmailMessage
from models.schemas import EmailSummary, InboxSummary
from tools.google_auth import build_google_service


class _ReadableHtml(HTMLParser):
    _block_tags = {"br", "div", "li", "p", "tr"}
    _ignored_tags = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._ignored_tags:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in self._block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._ignored_tags and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in self._block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _service(service: Any | None) -> Any:
    return service or build_google_service("gmail", "v1")


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


def _decode_body_data(data: str) -> str:
    decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return decoded.decode("utf-8", errors="replace")


def _part_text(part: dict[str, Any], message_id: str, service: Any) -> str:
    data = part.get("body", {}).get("data")
    if data is None and part.get("body", {}).get("attachmentId"):
        response = (
            service.users()
            .messages()
            .attachments()
            .get(
                userId="me",
                messageId=message_id,
                id=part["body"]["attachmentId"],
            )
            .execute()
        )
        data = response.get("data")
    if data is None:
        return ""
    return _decode_body_data(data)


def _readable_html(html: str) -> str:
    parser = _ReadableHtml()
    parser.feed(html)
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def _message_content(payload: dict[str, Any], message_id: str, service: Any) -> tuple[str, list[EmailAttachment]]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[EmailAttachment] = []

    def visit(part: dict[str, Any]) -> None:
        children = part.get("parts", [])
        if children:
            for child in children:
                visit(child)
            return

        mime_type = part.get("mimeType", "application/octet-stream")
        part_headers = _headers(part)
        filename = part.get("filename", "")
        disposition = part_headers.get("content-disposition", "")
        body_metadata = part.get("body", {})
        is_attachment = bool(filename or "attachment" in disposition.lower())

        if is_attachment:
            attachments.append(
                EmailAttachment(
                    filename=filename or "Unnamed attachment",
                    mime_type=mime_type,
                    size_bytes=body_metadata.get("size", 0),
                )
            )
        elif mime_type in {"text/plain", "text/html"}:
            text = _part_text(part, message_id, service)
            if mime_type == "text/plain":
                plain_parts.append(text)
            else:
                html_parts.append(text)

    visit(payload)
    plain_body = "\n".join(part for part in plain_parts if part.strip()).strip()
    if plain_body:
        return plain_body, attachments
    html_body = "\n".join(part for part in html_parts if part.strip())
    return _readable_html(html_body), attachments


def _summary(message: dict[str, Any]) -> EmailSummary:
    payload = message.get("payload", {})
    headers = _headers(payload)
    received = None
    if headers.get("date"):
        try:
            received = parsedate_to_datetime(headers["date"])
        except (TypeError, ValueError, OverflowError):
            received = None
    return EmailSummary(
        id=message["id"],
        thread_id=message.get("threadId"),
        sender=headers.get("from", ""),
        subject=headers.get("subject", ""),
        snippet=message.get("snippet", ""),
        received_at=received,
        labels=message.get("labelIds", []),
    )


def check_inbox(max_results: int = 20, query: str = "in:anywhere", service: Any | None = None) -> list[EmailSummary]:
    if max_results < 1:
        raise ValueError("max_results must be positive")
    gmail = _service(service)
    response = gmail.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    return [_summary(gmail.users().messages().get(userId="me", id=item["id"], format="metadata").execute())
            for item in response.get("messages", [])]


def read_email(message_id: str, service: Any | None = None) -> GmailEmailMessage:
    if not message_id.strip():
        raise ValueError("message_id is required")
    gmail = _service(service)
    message = gmail.users().messages().get(userId="me", id=message_id, format="full").execute()
    payload = message.get("payload", {})
    headers = _headers(payload)
    received = None
    if headers.get("date"):
        try:
            received = parsedate_to_datetime(headers["date"])
        except (TypeError, ValueError, OverflowError):
            received = None
    body, attachments = _message_content(payload, message_id, gmail)
    return GmailEmailMessage(
        id=message["id"],
        thread_id=message.get("threadId"),
        sender=headers.get("from", ""),
        to=headers.get("to", ""),
        subject=headers.get("subject", ""),
        received_at=received,
        body=body,
        attachments=attachments,
    )


def draft_email(draft: EmailDraft, service: Any | None = None) -> dict[str, Any]:
    message = EmailMessage()
    message["To"] = ", ".join(draft.to)
    message["Subject"] = draft.subject
    if draft.cc:
        message["Cc"] = ", ".join(draft.cc)
    if draft.bcc:
        message["Bcc"] = ", ".join(draft.bcc)
    message.set_content(draft.body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return _service(service).users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()


def summarize_inbox(max_results: int = 20, service: Any | None = None) -> InboxSummary:
    emails = check_inbox(max_results=max_results, service=service)
    unread_count = sum("UNREAD" in email.labels for email in emails)
    return InboxSummary(
        count=len(emails),
        unread_count=unread_count,
        emails=emails,
        overview=f"{len(emails)} messages loaded; {unread_count} unread.",
    )
