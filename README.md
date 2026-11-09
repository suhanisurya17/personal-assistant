# Personal Assistant

A small Python assistant for Gmail and Google Calendar, using the OpenAI Agents SDK for orchestration and SQLite for local memory.

## Setup

1. Install [uv](https://docs.astral.sh/uv/) and dependencies:

   ```bash
   uv sync
   ```

2. Create a Google Cloud OAuth desktop application. Enable the Gmail and Google Calendar APIs, download the client JSON, and save it as `credentials.json` (or set `GOOGLE_CREDENTIALS_FILE`).
3. Copy `.env.example` to `.env` and set `OPENAI_API_KEY`.
4. Start the interactive assistant:

   ```bash
   uv run python app.py
   ```

The first Google operation opens a browser for OAuth consent and stores the refresh token in `token.json`. If Google rejects an expired or revoked refresh token, the app opens a fresh consent flow and saves the replacement token. Keep both credential files private. OAuth apps left in Google's Testing status can have refresh tokens expire after seven days for these scopes, so development use may require periodic reauthorization.

Set `GOOGLE_TIMEZONE` to an IANA timezone such as `America/New_York`. Calendar day summaries use that timezone for local midnight boundaries and account for daylight-saving changes.

## Available operations

- Gmail: check the inbox, read an email by ID, create a draft, and summarize the inbox. Email reading prefers plain-text content, falls back to readable text extracted from HTML, and lists attachment filenames, types, and sizes. Attachment files are not downloaded or opened.
- Calendar: create and delete an event, and summarize events for a day.
- Subscription tracker: scan up to 100 relevant inbox messages by default (or request up to 500), locally extract merchant, amount, billing cycle, evidence-based status, and next due date, then store structured records in SQLite. Monthly/yearly due dates calculated from a payment message are estimates; status is based on the latest matching message found, not a live account check. The scan is read-only and does not cancel subscriptions or download email attachments. The scan sends extracted tracker records (not full email bodies) to the agent for its response.
- Memory: a small SQLite key/value store in `assistant.db` for future preferences and conversation state.

The assistant creates drafts rather than sending mail. It should ask for explicit confirmation before destructive calendar operations.

To use the subscription tracker, ask "Show my subscriptions" to check scan status and existing records, or ask "Scan my inbox for subscriptions" to start a scan. It distinguishes an inbox that has not been scanned from a completed scan with no matching records. The tracker searches Gmail messages matching subscription, renewal, recurring, membership, trial, and billing terms; it does not inspect every inbox message or parse attached invoices. Verify uncertain results against their source email IDs. Currently records are grouped by merchant, so separate plans from the same sender may need manual review.

For a provider-specific question such as "Canva?", the assistant first checks saved records. If none match, it can offer a targeted Gmail search for that provider; it should only perform that search after the user agrees. Tool errors report the Google error category/status rather than guessing at a token issue.

The CLI keeps conversation history in memory while it is running, so short follow-ups such as "yes" retain their context. History is discarded when the CLI exits.

## Dashboard

A local web dashboard sits on top of the same Gmail and Calendar tools:

```bash
python server.py        # or: uv run python server.py
```

Then open http://127.0.0.1:8000. It only listens on your own machine.

Cards:

- **Daily summary:** a short brief that connects today's calendar with what's waiting in your inbox.
- **Next up:** a countdown to your next event.
- **Today:** your schedule, with free blocks of 30 minutes or more shown between events.
- **Needs attention:** emails with deadlines, meeting requests, or replies owed. Each one is linked to that day's calendar, and anything due today or tomorrow is marked in red.
- **News:** the top 3 stories from your latest TLDR email (TLDR by Wealthsimple or tldr.tech), read from Gmail.

Click **Customize** to drag cards around, resize them (S/M/L), or hide and show them. The layout is saved in `assistant.db`.

### How OpenAI usage stays low

Every model result is saved in `assistant.db`, so refreshing the page or restarting the server doesn't call the model again.

- The daily summary is written once per day. Only the **Rewrite** link asks for a new one.
- Each email is classified once, by message ID. Later loads only send emails that arrived since then.
- Each newsletter issue is summarized once. tldr.tech issues are parsed without the model at all.

If the model is unavailable, the summary falls back to a plain count of events and emails, and the other cards say what couldn't be checked.

Optional settings in `.env`: `NEWS_SENDERS` (a JSON list of Gmail `from:` values), plus `DAY_START_HOUR` and `DAY_END_HOUR` for the free-time window.

## Tests

```bash
uv run pytest
```

The tests use fake Google service objects and do not require credentials or network access.

## Layout

See the requested `app.py`, `agent.py`, `tools/`, `prompts/`, `models/`, `tests/`, `config.py`, `logs/`, and this README. FastAPI is included in the dependencies so an HTTP layer can be added without changing the tool boundaries.
