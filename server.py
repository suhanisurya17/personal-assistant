"""Local web dashboard. Run with:  python server.py  (then open http://127.0.0.1:8000)."""
import re
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import HttpError

from config import get_settings
from tools.ai import cache_get, cache_set
from tools.dashboard import (
    AttentionView,
    Briefing,
    ScheduleView,
    attention_view,
    build_brief,
    cached_brief,
    schedule_view,
)
from tools.google_auth import GoogleReauthorizationRequired
from tools.news import NewsDigest, latest_newsletter
from tools.photos import PhotoSet, PhotosClient, PhotosNotConnected, PickerSession, load_photos, photo_path

STATIC_DIR = Path(__file__).parent / "static"
LAYOUT_KEY = "dashboard.layout"

app = FastAPI(title="Personal Assistant Dashboard")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

T = TypeVar("T")
# Serialize anything that may call the model, so simultaneous requests
# (e.g. the brief and the attention card on first load) never pay twice.
_attention_lock, _news_lock, _brief_lock = Lock(), Lock(), Lock()


def _google(operation: Callable[[], T]) -> T:
    """Turn Google failures into readable JSON errors instead of 500s."""
    try:
        return operation()
    except GoogleReauthorizationRequired as error:
        raise HTTPException(401, {"code": "google_reauthorization_required", "message": str(error)})
    except FileNotFoundError as error:
        raise HTTPException(500, {"code": "google_credentials_missing", "message": str(error)})
    except GoogleAuthError as error:
        raise HTTPException(401, {"code": "google_authentication_error",
                                  "message": f"Google authentication failed ({type(error).__name__})."})
    except HttpError as error:
        raise HTTPException(502, {"code": "google_api_error",
                                  "message": f"Google API returned HTTP {error.resp.status}."})


def _now() -> datetime:
    return datetime.now(ZoneInfo(get_settings().google_timezone))


def _attention() -> AttentionView:
    with _attention_lock:
        return attention_view()


def _news() -> NewsDigest:
    with _news_lock:
        return latest_newsletter(get_settings().news_senders, limit=3)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/schedule")
def schedule() -> ScheduleView:
    return _google(schedule_view)


@app.get("/api/attention")
def attention() -> AttentionView:
    return _google(_attention)


@app.get("/api/news")
def news() -> NewsDigest:
    return _google(_news)


@app.get("/api/brief")
def brief(refresh: bool = False) -> Briefing:
    """Today's brief. Served from cache unless refresh=true (the "Rewrite" button)."""
    with _brief_lock:
        now = _now()
        if not refresh and (cached := cached_brief(now.date())):
            return cached
        schedule_data = _google(schedule_view)
        attention_data = _google(_attention)
        try:
            news_data = _news()
        except Exception:
            news_data = None
        return build_brief(schedule_data, attention_data, news_data, now)


@app.get("/api/layout")
def get_layout() -> dict[str, Any]:
    return {"layout": cache_get(LAYOUT_KEY)}


@app.put("/api/layout")
def put_layout(layout: list[dict[str, Any]] = Body(..., embed=True)) -> dict[str, bool]:
    cache_set(LAYOUT_KEY, layout)
    return {"ok": True}


# ---------- Google Photos (Picker API) ----------

_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


def _photos(operation: Callable[[], T]) -> T:
    import requests

    try:
        return operation()
    except PhotosNotConnected as error:
        raise HTTPException(401, {"code": "photos_not_connected", "message": str(error)})
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else 502
        if status == 403:
            raise HTTPException(403, {"code": "photos_api_disabled",
                                      "message": "Google refused the request. Make sure the Photos Picker API "
                                                 "is enabled in Google Cloud Console."})
        raise HTTPException(502, {"code": "photos_api_error", "message": f"Google Photos returned HTTP {status}."})
    except requests.RequestException as error:
        raise HTTPException(502, {"code": "photos_network_error", "message": f"Couldn't reach Google Photos ({type(error).__name__})."})


def _check_session_id(session_id: str) -> None:
    if not _SESSION_ID.match(session_id):
        raise HTTPException(400, {"code": "bad_session", "message": "Invalid picker session."})


@app.get("/api/photos")
def photos() -> PhotoSet:
    return load_photos()


@app.post("/api/photos/session")
def photos_session() -> PickerSession:
    """Start a picker session. Opens Google's consent page in the browser the first time."""
    return _google(lambda: _photos(lambda: PhotosClient.connect(interactive=True).create_session()))


@app.get("/api/photos/session/{session_id}")
def photos_session_status(session_id: str) -> dict[str, bool]:
    _check_session_id(session_id)
    return {"done": _photos(lambda: PhotosClient.connect().is_done(session_id))}


@app.post("/api/photos/session/{session_id}/import")
def photos_import(session_id: str) -> PhotoSet:
    _check_session_id(session_id)
    return _photos(lambda: PhotosClient.connect().import_session(session_id))


@app.get("/api/photos/file/{name}")
def photo_file(name: str) -> FileResponse:
    path = photo_path(name)
    if path is None:
        raise HTTPException(404, {"code": "not_found", "message": "Photo not found."})
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
