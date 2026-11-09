"""Small helpers shared by dashboard features that call the model.

Every model result is cached in SQLite (via MemoryStore) so page refreshes and
server restarts never re-call OpenAI for something already computed.
"""
import json
from typing import Any, Callable

from config import get_settings
from tools.memory import MemoryStore

# (system prompt, user content, max tokens) -> raw JSON text
Completer = Callable[[str, str, int], str]


class AIUnavailable(RuntimeError):
    """No API key configured, or the model call failed."""


def cache_get(key: str) -> Any | None:
    store = MemoryStore(get_settings().database_path)
    try:
        raw = store.get(key)
    finally:
        store.close()
    return json.loads(raw) if raw else None


def cache_set(key: str, value: Any) -> None:
    store = MemoryStore(get_settings().database_path)
    try:
        store.set(key, json.dumps(value))
    finally:
        store.close()


def openai_completer() -> Completer:
    settings = get_settings()
    if not settings.openai_api_key:
        raise AIUnavailable("OPENAI_API_KEY is not set.")
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)

    def complete(system: str, user: str, max_tokens: int) -> str:
        response = client.chat.completions.create(
            model=settings.openai_model,
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
            temperature=0.2,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return response.choices[0].message.content or "{}"

    return complete


def complete_json(system: str, payload: Any, max_tokens: int = 400, complete: Completer | None = None) -> dict[str, Any]:
    try:
        completer = complete or openai_completer()
        data = json.loads(completer(system, payload if isinstance(payload, str) else json.dumps(payload), max_tokens))
    except AIUnavailable:
        raise
    except Exception as error:  # network, auth, quota, or malformed JSON
        raise AIUnavailable(f"Model call failed ({type(error).__name__}).") from error
    if not isinstance(data, dict):
        raise AIUnavailable("Model returned unexpected JSON.")
    return data
