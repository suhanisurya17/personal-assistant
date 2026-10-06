from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from config import get_settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]


def get_google_credentials() -> Credentials:
    settings = get_settings()
    token_path = Path(settings.google_token_file)
    credentials_path = Path(settings.google_credentials_file)
    credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES) if token_path.exists() else None
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        if not credentials_path.exists():
            raise FileNotFoundError(
                f"Google OAuth client file not found at {credentials_path}. "
                "Download it from Google Cloud Console and set GOOGLE_CREDENTIALS_FILE."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        credentials = flow.run_local_server(port=0)
        token_path.write_text(credentials.to_json())
    return credentials


def build_google_service(api: str, version: str) -> Any:
    from googleapiclient.discovery import build

    return build(api, version, credentials=get_google_credentials())
