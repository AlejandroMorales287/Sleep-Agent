"""Calendar access for the agent.

Two backends:
  - GoogleCalendar: real Google Calendar (needs credentials.json, see README)
  - DemoCalendar:   reads/writes demo_calendar.json, so you can demo with no OAuth

get_calendar() picks Google if credentials.json exists (and USE_DEMO_CALENDAR
isn't set), otherwise the demo calendar.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(HERE, "credentials.json")
TOKEN_PATH = os.path.join(HERE, "token.json")
DEMO_PATH = os.path.join(HERE, os.getenv("DEMO_CALENDAR_FILE", "demo_calendar.json"))
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def now_local() -> datetime:
    return datetime.now().astimezone()


def _parse(dt_str: str) -> datetime:
    """Parse an ISO string; all-day dates become local midnight."""
    if len(dt_str) == 10:  # YYYY-MM-DD
        return datetime.fromisoformat(dt_str).astimezone()
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.astimezone()


# ---------------------------------------------------------------------- #
class GoogleCalendar:
    name = "google"

    def __init__(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if os.path.exists(TOKEN_PATH):
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
                creds = flow.run_local_server(port=0)  # opens a browser once
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
        self.service = build("calendar", "v3", credentials=creds)

    def upcoming_events(self, days_ahead: int = 3) -> list[dict]:
        start = now_local()
        end = start + timedelta(days=days_ahead)
        resp = self.service.events().list(
            calendarId="primary",
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()
        events = []
        for e in resp.get("items", []):
            s = e["start"].get("dateTime", e["start"].get("date"))
            en = e["end"].get("dateTime", e["end"].get("date"))
            events.append({
                "title": e.get("summary", "(no title)"),
                "start": s,
                "end": en,
                "description": (e.get("description") or "")[:300],
            })
        return events

    def create_event(self, title: str, start: str, end: str, description: str = "") -> dict:
        body = {
            "summary": title,
            "description": description,
            "start": {"dateTime": _parse(start).isoformat()},
            "end": {"dateTime": _parse(end).isoformat()},
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 0}]},
        }
        created = self.service.events().insert(calendarId="primary", body=body).execute()
        return {"created": True, "title": title, "start": start, "end": end,
                "event_id": created.get("id"), "link": created.get("htmlLink")}

    def delete_event(self, event_id: str) -> bool:
        self.service.events().delete(calendarId="primary", eventId=event_id).execute()
        return True


# ---------------------------------------------------------------------- #
class DemoCalendar:
    """JSON-file calendar. Event times can be absolute ISO strings, or relative
    like {"in_hours": 14, "duration_hours": 2} so the demo always looks current."""

    name = "demo"

    def __init__(self, path: str = DEMO_PATH):
        self.path = path
        with open(path) as f:
            self.raw = json.load(f)
        base = now_local().replace(minute=0, second=0, microsecond=0)
        self.events = []
        for e in self.raw.get("events", []):
            if "in_hours" in e:
                s = base + timedelta(hours=e["in_hours"])
                en = s + timedelta(hours=e.get("duration_hours", 1))
            else:
                s, en = _parse(e["start"]), _parse(e["end"])
            self.events.append({
                "id": e.get("id"),
                "title": e["title"],
                "start": s.isoformat(timespec="minutes"),
                "end": en.isoformat(timespec="minutes"),
                "description": e.get("description", ""),
            })

    def upcoming_events(self, days_ahead: int = 3) -> list[dict]:
        start, end = now_local(), now_local() + timedelta(days=days_ahead)
        return sorted(
            (e for e in self.events if start <= _parse(e["end"]) and _parse(e["start"]) <= end),
            key=lambda e: e["start"],
        )

    def create_event(self, title: str, start: str, end: str, description: str = "") -> dict:
        event_id = f"demo-{int(now_local().timestamp() * 1000)}"
        event = {"id": event_id, "title": title, "start": start, "end": end, "description": description}
        self.events.append(event)
        self.raw.setdefault("events", []).append(event)
        self._save()
        return {"created": True, "title": title, "start": start, "end": end,
                "event_id": event_id, "calendar": "demo"}

    def delete_event(self, event_id: str) -> bool:
        before = len(self.raw.get("events", []))
        self.raw["events"] = [e for e in self.raw.get("events", []) if e.get("id") != event_id]
        self.events = [e for e in self.events if e.get("id") != event_id]
        self._save()
        return len(self.raw["events"]) < before

    def _save(self):
        with open(self.path, "w") as f:
            json.dump(self.raw, f, indent=2)


def get_calendar():
    use_demo = os.getenv("USE_DEMO_CALENDAR", "").lower() in ("1", "true", "yes")
    if not use_demo and os.path.exists(CREDENTIALS_PATH):
        try:
            return GoogleCalendar()
        except Exception as e:  # don't let OAuth trouble kill the demo
            print(f"[calendar] Google Calendar failed ({e}); using demo calendar.")
    return DemoCalendar()
