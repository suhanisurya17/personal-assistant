"""Google Photos via the Photos Picker API.

Since 2025 apps can't read a user's library directly; the user picks photos in
Google's own picker, and the app downloads the picked items. We keep local
copies (so the widget never re-downloads) in PHOTOS_DIR with a manifest.

Uses a separate token file so the Gmail/Calendar sign-in is never affected.
"""
import json
import re
import shutil
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from pydantic import BaseModel, Field

from config import get_settings
from tools.google_auth import GoogleReauthorizationRequired

SCOPES = ["https://www.googleapis.com/auth/photospicker.mediaitems.readonly"]
API = "https://photospicker.googleapis.com/v1"
MAX_PICK = 100
DOWNLOAD_SIZE = "=w2048-h2048"  # longest edge ≤ 2048px; plenty for a widget
SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,200}\.(jpg|png|webp|gif|heic)$")
_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/heic": "heic"}


class Photo(BaseModel):
    file: str
    width: int | None = None
    height: int | None = None
    created: str | None = None


class PhotoSet(BaseModel):
    photos: list[Photo] = Field(default_factory=list)
    connected: bool = False


class PickerSession(BaseModel):
    id: str
    picker_url: str
    poll_seconds: float = 4.0


class PhotosNotConnected(RuntimeError):
    """No Photos sign-in yet; the user needs to start one from the widget."""


def photos_dir() -> Path:
    return Path(get_settings().photos_dir)


def _token_path() -> Path:
    return Path(get_settings().photos_token_file)


def _save(credentials: Credentials) -> None:
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(credentials.to_json())


def get_photos_credentials(interactive: bool = False) -> Credentials:
    """Load the Photos token; if `interactive`, open the browser consent screen when needed."""
    token_path = _token_path()
    credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES) if token_path.exists() else None
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            _save(credentials)
        except RefreshError:
            credentials = None  # revoked or expired; fall through to a fresh sign-in
    if credentials and credentials.valid:
        return credentials
    if not interactive:
        raise PhotosNotConnected("Google Photos isn't connected yet.")
    client_file = Path(get_settings().google_credentials_file)
    if not client_file.exists():
        raise GoogleReauthorizationRequired(f"Google OAuth client file not found at {client_file}.")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
    credentials = flow.run_local_server(port=0, prompt="consent")
    _save(credentials)
    return credentials


def is_connected() -> bool:
    return _token_path().exists()


class PhotosClient:
    def __init__(self, session: Any) -> None:
        self.session = session  # requests-like, already authorized

    @classmethod
    def connect(cls, interactive: bool = False) -> "PhotosClient":
        return cls(AuthorizedSession(get_photos_credentials(interactive=interactive)))

    def _json(self, response: Any) -> dict[str, Any]:
        response.raise_for_status()
        return response.json() if response.content else {}

    def create_session(self) -> PickerSession:
        data = self._json(self.session.post(f"{API}/sessions", json={"pickingConfig": {"maxItemCount": str(MAX_PICK)}}))
        poll = str(data.get("pollingConfig", {}).get("pollInterval", "4s")).rstrip("s")
        try:
            poll_seconds = min(max(float(poll), 2.0), 10.0)
        except ValueError:
            poll_seconds = 4.0
        # "/autoclose" makes Google close the picker tab once the user is done.
        return PickerSession(id=data["id"], picker_url=data["pickerUri"].rstrip("/") + "/autoclose", poll_seconds=poll_seconds)

    def is_done(self, session_id: str) -> bool:
        return bool(self._json(self.session.get(f"{API}/sessions/{session_id}")).get("mediaItemsSet"))

    def picked_items(self, session_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        token = None
        while True:
            params = {"sessionId": session_id, "pageSize": 100}
            if token:
                params["pageToken"] = token
            data = self._json(self.session.get(f"{API}/mediaItems", params=params))
            items.extend(data.get("mediaItems", []))
            token = data.get("nextPageToken")
            if not token:
                return items

    def import_session(self, session_id: str) -> PhotoSet:
        """Download picked photos (videos are skipped), replacing the previous set."""
        target = photos_dir()
        staging = target.with_name(target.name + ".importing")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        photos: list[Photo] = []
        for index, item in enumerate(self.picked_items(session_id)):
            media = item.get("mediaFile", {})
            if item.get("type") not in (None, "PHOTO") or not media.get("baseUrl"):
                continue
            extension = _EXTENSIONS.get(media.get("mimeType", ""), "jpg")
            name = f"{index:03d}_{re.sub(r'[^A-Za-z0-9_-]', '', str(item.get('id', '')))[:40] or 'photo'}.{extension}"
            response = self.session.get(media["baseUrl"] + DOWNLOAD_SIZE)
            response.raise_for_status()
            (staging / name).write_bytes(response.content)
            meta = media.get("mediaFileMetadata", {})
            photos.append(Photo(file=name, width=meta.get("width"), height=meta.get("height"), created=item.get("createTime")))
        (staging / "manifest.json").write_text(json.dumps([p.model_dump() for p in photos]))
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
        try:
            self.session.delete(f"{API}/sessions/{session_id}")
        except Exception:
            pass  # sessions expire on their own
        return PhotoSet(photos=photos, connected=True)


def load_photos() -> PhotoSet:
    manifest = photos_dir() / "manifest.json"
    photos = [Photo.model_validate(p) for p in json.loads(manifest.read_text())] if manifest.exists() else []
    return PhotoSet(photos=photos, connected=is_connected())


def photo_path(name: str) -> Path | None:
    if not SAFE_NAME.match(name):
        return None
    path = photos_dir() / name
    return path if path.is_file() else None
