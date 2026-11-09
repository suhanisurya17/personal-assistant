import json

import pytest
from fastapi.testclient import TestClient

import server
import tools.photos as photos
from tools.photos import PhotosClient, load_photos, photo_path


class Response:
    def __init__(self, data=None, content=b"", status=200):
        self.data, self.status_code = data, status
        self.content = content or (json.dumps(data).encode() if data is not None else b"")

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(response=self)


class FakeGoogle:
    """Stands in for an AuthorizedSession talking to the Photos Picker API."""

    def __init__(self, items, done=True):
        self.items, self.done = items, done
        self.calls = []

    def post(self, url, json=None):
        self.calls.append(("POST", url, json))
        return Response({"id": "sess1", "pickerUri": "https://photos.google.com/picker/abc",
                         "pollingConfig": {"pollInterval": "5s"}})

    def get(self, url, params=None):
        self.calls.append(("GET", url, params))
        if url.endswith("/sessions/sess1"):
            return Response({"mediaItemsSet": self.done})
        if url.endswith("/mediaItems"):
            if not params.get("pageToken"):
                return Response({"mediaItems": self.items[:1], "nextPageToken": "p2"})
            return Response({"mediaItems": self.items[1:]})
        return Response(content=b"IMAGE:" + url.encode())  # a photo download

    def delete(self, url):
        self.calls.append(("DELETE", url, None))
        return Response({})


ITEMS = [
    {"id": "A1", "type": "PHOTO", "createTime": "2025-06-01T10:00:00Z",
     "mediaFile": {"baseUrl": "https://lh3.example/a", "mimeType": "image/jpeg",
                   "mediaFileMetadata": {"width": 4000, "height": 3000}}},
    {"id": "V1", "type": "VIDEO", "mediaFile": {"baseUrl": "https://lh3.example/v", "mimeType": "video/mp4"}},
    {"id": "B/2", "type": "PHOTO", "mediaFile": {"baseUrl": "https://lh3.example/b", "mimeType": "image/png"}},
]


@pytest.fixture(autouse=True)
def temp_paths(tmp_path, monkeypatch):
    settings = server.get_settings()
    monkeypatch.setattr(settings, "photos_dir", str(tmp_path / "data" / "photos"))
    monkeypatch.setattr(settings, "photos_token_file", str(tmp_path / "photos_token.json"))
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "test.db"))


def test_create_session_adds_autoclose_and_limits_items():
    google = FakeGoogle(ITEMS)
    session = PhotosClient(google).create_session()
    assert session.picker_url == "https://photos.google.com/picker/abc/autoclose"
    assert session.poll_seconds == 5
    assert google.calls[0][2] == {"pickingConfig": {"maxItemCount": "100"}}


def test_import_downloads_photos_only_across_pages_and_cleans_up():
    google = FakeGoogle(ITEMS)
    result = PhotosClient(google).import_session("sess1")

    assert [p.file for p in result.photos] == ["000_A1.jpg", "002_B2.png"]  # video skipped, id sanitized
    assert result.photos[0].width == 4000 and result.photos[0].created == "2025-06-01T10:00:00Z"
    assert photo_path("000_A1.jpg").read_bytes() == b"IMAGE:https://lh3.example/a=w2048-h2048"
    assert ("DELETE", f"{photos.API}/sessions/sess1", None) in google.calls
    assert [p.file for p in load_photos().photos] == ["000_A1.jpg", "002_B2.png"]


def test_picking_again_replaces_the_previous_set():
    PhotosClient(FakeGoogle(ITEMS)).import_session("sess1")
    PhotosClient(FakeGoogle(ITEMS[2:])).import_session("sess1")
    assert [p.file for p in load_photos().photos] == ["000_B2.png"]
    assert photo_path("000_A1.jpg") is None


def test_photo_path_rejects_traversal_and_unknown_files():
    assert photo_path("../photos_token.json") is None
    assert photo_path("manifest.json") is None
    assert photo_path("missing.jpg") is None


def test_status_without_photos_sign_in_is_a_clear_error():
    response = TestClient(server.app).get("/api/photos/session/sess1")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "photos_not_connected"


def test_routes_list_and_serve_imported_photos(monkeypatch):
    PhotosClient(FakeGoogle(ITEMS)).import_session("sess1")
    client = TestClient(server.app)
    listing = client.get("/api/photos").json()
    assert [p["file"] for p in listing["photos"]] == ["000_A1.jpg", "002_B2.png"]
    assert client.get("/api/photos/file/000_A1.jpg").content.startswith(b"IMAGE:")
    assert client.get("/api/photos/file/..%2Fphotos_token.json").status_code == 404
    assert client.get("/api/photos/session/bad id!").status_code == 400


def test_disabled_api_gives_actionable_message(monkeypatch):
    class Disabled(FakeGoogle):
        def get(self, url, params=None):
            return Response({"error": "forbidden"}, status=403)

    monkeypatch.setattr(PhotosClient, "connect", classmethod(lambda cls, interactive=False: cls(Disabled([]))))
    response = TestClient(server.app).get("/api/photos/session/sess1")
    assert response.status_code == 403
    assert "Photos Picker API" in response.json()["detail"]["message"]
