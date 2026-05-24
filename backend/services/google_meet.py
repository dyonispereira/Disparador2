"""
Google Calendar + Meet integration.

Prerequisites:
  1. Enable Google Calendar API in Google Cloud Console
  2. Create OAuth 2.0 Desktop credentials → download as credentials.json
  3. Place credentials.json in the backend/ folder
  4. Run: python setup_google_auth.py  (once, to generate token.json)
"""

import os
from datetime import datetime, timedelta

SCOPES = ["https://www.googleapis.com/auth/calendar"]
BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))


def _credentials_path() -> str:
    return os.path.join(BACKEND_DIR, "credentials.json")


def _token_path() -> str:
    return os.path.join(BACKEND_DIR, "token.json")


def is_configured() -> bool:
    return os.path.exists(_credentials_path())


def is_authenticated() -> bool:
    return os.path.exists(_token_path())


def get_service():
    """Returns an authenticated Google Calendar service object."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        raise RuntimeError(
            "Instale as bibliotecas Google: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
        )

    creds = None
    token_path = _token_path()

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_path, "w") as f:
                f.write(creds.to_json())
        else:
            raise RuntimeError(
                "Google não autenticado. Execute: python setup_google_auth.py"
            )

    return build("calendar", "v3", credentials=creds)


def create_meet_event(
    summary: str,
    date_str: str,
    time_str: str,
    attendee_emails: list[str] | None = None,
    calendar_id: str = "primary",
) -> dict:
    """
    Creates a Google Calendar event with a Google Meet link.

    Args:
        summary:         Event title (e.g. "Reunião Empresa × João")
        date_str:        Date in YYYY-MM-DD format
        time_str:        Start time in HH:MM format (Brasília / America/Sao_Paulo)
        attendee_emails: Optional list of individual email addresses to invite
        calendar_id:     Target calendar ID (default "primary"). Pass the shared
                         calendar ID to create the event directly in it.

    Returns:
        {"event_id": str, "meet_link": str | None, "html_link": str}
    """
    service = get_service()

    start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(hours=1)

    event = {
        "summary": summary,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Sao_Paulo"},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "America/Sao_Paulo"},
        "conferenceData": {
            "createRequest": {
                "requestId": f"meet-{start_dt.strftime('%Y%m%d%H%M%S')}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "attendees": [{"email": e} for e in (attendee_emails or [])],
    }

    created = service.events().insert(
        calendarId=calendar_id,
        body=event,
        conferenceDataVersion=1,
        sendUpdates="all" if attendee_emails else "none",
    ).execute()

    meet_link = None
    for ep in created.get("conferenceData", {}).get("entryPoints", []):
        if ep.get("entryPointType") == "video":
            meet_link = ep.get("uri")
            break

    return {
        "event_id": created["id"],
        "meet_link": meet_link,
        "html_link": created.get("htmlLink"),
    }
