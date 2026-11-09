from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from config import get_settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]


class GoogleReauthorizationRequired(RuntimeError):
    """Raised when Google credentials cannot be refreshed or reauthorized."""


def _is_invalid_grant(error: RefreshError) -> bool:
    return any(
        isinstance(argument, dict) and argument.get("error") == "invalid_grant"
        for argument in error.args
    )


def _authorize(credentials_path: Path) -> Credentials:
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    return flow.run_local_server(port=0, prompt="consent")


def get_google_credentials() -> Credentials:
    settings = get_settings()
    token_path = Path(settings.google_token_file)
    credentials_path = Path(settings.google_credentials_file)
    credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES) if token_path.exists() else None
    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError as error:
            if not _is_invalid_grant(error):
                raise GoogleReauthorizationRequired(
                    "Google rejected the token refresh. Check the network and Google OAuth configuration."
                ) from error
            if not credentials_path.exists():
                raise GoogleReauthorizationRequired(
                    "Google rejected the saved token, and the OAuth client file is missing. "
                    "Restore GOOGLE_CREDENTIALS_FILE and authorize again."
                ) from error
            credentials = _authorize(credentials_path)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(credentials.to_json())
        else:
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(credentials.to_json())
    if not credentials or not credentials.valid:
        if not credentials_path.exists():
            raise FileNotFoundError(
                f"Google OAuth client file not found at {credentials_path}. "
                "Download it from Google Cloud Console and set GOOGLE_CREDENTIALS_FILE."
            )
        credentials = _authorize(credentials_path)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json())
    return credentials


def build_google_service(api: str, version: str) -> Any:
    from googleapiclient.discovery import build

    return build(api, version, credentials=get_google_credentials())
