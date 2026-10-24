"""The Claude agent that decides what a drowsy student should do.

It gets a fatigue summary (microsleeps, yawns, how long they've been studying),
looks at the calendar with tools, optionally books a nap/sleep block, and ends
by calling `give_recommendation` with a structured answer.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import anthropic
from dotenv import load_dotenv

load_dotenv()  # before reading CLAUDE_MODEL etc.

from calendar_tool import get_calendar, now_local

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
MAX_TURNS = 6

SYSTEM_PROMPT = """You are Sleep Agent, a caring but practical study coach.
A webcam has detected that the user is nodding off (microsleeps = eyes closed for
about a second or more while facing the screen). Studying through microsleeps is
very inefficient: they are not absorbing material.

Your job: decide the best next action and say it in a short, warm, direct way.

Process:
1. Call get_upcoming_events to see what's coming up (default 3 days).
2. Decide one action:
   - "sleep": nothing important in the next ~24h, or it's very late and the deadline
     can survive a full night. Sleep beats cramming for memory consolidation.
   - "nap": something important within ~24h AND there's time for a nap. Prefer 20 min
     (no grogginess) or 90 min (one full cycle). Avoid 45-60 min naps (sleep inertia).
   - "quick_reset": deadline/exam is within a few hours and there's truly no time:
     cold water on face, glass of water, 5 min walk or jumping jacks, bright light.
3. If you chose sleep or nap, you MAY call create_calendar_event to block it.
   Give a duration in minutes and how many minutes from now it starts (usually 0).
   The tool returns the exact booked times; trust those, not your own arithmetic.
   Book at most one event. Don't book for quick_reset. Never modify or delete other events.
4. Finish by calling give_recommendation. Exactly once.
   Do NOT write clock times (like "until 1:30 AM") in your message; the app adds
   the booked times itself. Refer to durations instead ("a 90 minute nap").

Message style: 1-3 sentences, mention the specific deadline you're reasoning about,
no lecturing, no medical claims. Be honest if they're too tired to learn effectively."""

TOOLS = [
    {
        "name": "get_upcoming_events",
        "description": "List the user's calendar events from now through the next N days, sorted by start time.",
        "input_schema": {
            "type": "object",
            "properties": {"days_ahead": {"type": "integer", "minimum": 1, "maximum": 14, "default": 3}},
        },
    },
    {
        "name": "create_calendar_event",
        "description": "Book a nap or sleep block on the user's calendar. The app computes the exact times "
                       "and returns them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "e.g. 'Nap - Sleep Agent' or 'Sleep - Sleep Agent'"},
                "duration_minutes": {"type": "integer", "minimum": 5, "maximum": 720},
                "start_in_minutes": {"type": "integer", "minimum": 0, "maximum": 240, "default": 0,
                                     "description": "Minutes from now until it starts (0 = right now)"},
                "description": {"type": "string"},
            },
            "required": ["title", "duration_minutes"],
        },
    },
    {
        "name": "give_recommendation",
        "description": "Final answer shown to the user. Call exactly once, at the end.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["sleep", "nap", "quick_reset"]},
                "minutes": {"type": "integer", "description": "Nap/sleep length or reset length in minutes"},
                "message": {"type": "string", "description": "1-3 sentences for the user"},
                "reason": {"type": "string", "description": "Short reasoning, e.g. which deadline drove it"},
            },
            "required": ["action", "message"],
        },
    },
]


def _fmt(dt: datetime) -> str:
    """e.g. 'Sat 12:20 AM' (Windows-safe, no %-I)."""
    return dt.strftime("%a %I:%M %p").replace(" 0", " ")


class SleepAgent:
    def __init__(self, calendar=None):
        self.booked = None
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        self.calendar = calendar or get_calendar()
        print(f"[agent] model={MODEL} calendar={self.calendar.name}")

    def _run_tool(self, name: str, args: dict):
        if name == "get_upcoming_events":
            return self.calendar.upcoming_events(int(args.get("days_ahead", 3)))
        if name == "create_calendar_event":
            if self.booked:
                return {"error": "An event was already booked this session; don't book another.",
                        "already_booked": self.booked}
            start = now_local().replace(second=0, microsecond=0) + timedelta(
                minutes=int(args.get("start_in_minutes", 0)))
            end = start + timedelta(minutes=int(args["duration_minutes"]))
            result = self.calendar.create_event(
                args["title"], start.isoformat(), end.isoformat(), args.get("description", "")
            )
            self.booked = {
                "title": args["title"],
                "start": start.isoformat(),
                "end": end.isoformat(),
                "start_local": _fmt(start),
                "end_local": _fmt(end),
                "minutes": int(args["duration_minutes"]),
                "event_id": result.get("event_id"),
            }
            return {**result, **self.booked}
        return {"error": f"unknown tool {name}"}

    def undo_booking(self, booked: dict) -> bool:
        """Delete an event this agent booked (used by the app's false-alarm button)."""
        if not booked or not booked.get("event_id"):
            return False
        return self.calendar.delete_event(booked["event_id"])

    def _finalize(self, rec: dict) -> dict:
        """The calendar event is the source of truth: copy its times into the answer."""
        if self.booked:
            b = self.booked
            rec["minutes"] = b["minutes"]
            rec["booked"] = b
            rec["message"] = (f"{rec.get('message', '').rstrip()} "
                              f"Blocked on your calendar: {b['start_local']} to {b['end_local']}.").strip()
        return rec

    def decide(self, fatigue: dict, log=print) -> dict:
        """fatigue: e.g. {"microsleeps_last_10_min": 3, "yawns_last_10_min": 2,
        "longest_eye_closure_s": 1.8, "session_minutes": 95}"""
        now = now_local()
        user_msg = (
            f"Current local time: {now.strftime('%A %Y-%m-%d %H:%M')} ({now.isoformat(timespec='minutes')}).\n"
            f"Fatigue signals: {json.dumps(fatigue)}\n"
            "What should I do?"
        )
        messages = [{"role": "user", "content": user_msg}]
        self.booked = None

        for _ in range(MAX_TURNS):
            resp = self.client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            tool_results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                if block.name == "give_recommendation":
                    return self._finalize(dict(block.input))
                log(f"[agent] -> {block.name}({json.dumps(block.input)})")
                try:
                    result = self._run_tool(block.name, block.input)
                except Exception as e:
                    result = {"error": str(e)}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                })

            if not tool_results:
                # Model answered in plain text instead of the tool; use that text.
                text = " ".join(b.text for b in resp.content if b.type == "text").strip()
                return {"action": "quick_reset", "message": text or "Take a short break and drink some water."}
            messages.append({"role": "user", "content": tool_results})

        return {"action": "quick_reset", "message": "You're nodding off. Stand up, drink some water, and take a 5 minute walk."}


if __name__ == "__main__":
    # Quick test without the webcam:  python agent.py
    agent = SleepAgent()
    print(agent.decide({"microsleeps_last_10_min": 3, "yawns_last_10_min": 2,
                        "longest_eye_closure_s": 1.6, "session_minutes": 120}))
