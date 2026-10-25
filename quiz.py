"""Wake-up quiz: 3 quick questions on what you were studying.

What it knows about your studying:
  - the topic you typed at startup (optional)
  - titles of windows you were recently in, e.g. "CSE214_Lecture7_Heaps.pdf - Adobe Acrobat"
    (read with one Windows call every few seconds; no screen capture)

Nothing is sent anywhere until a quiz is generated, after a desk nap or quick reset.
"""

from __future__ import annotations

import json
import os

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
NUM_QUESTIONS = 3

# Windows that say nothing about what you're studying
JUNK_TITLES = {
    "", "program manager", "task switching", "start", "search", "task view", "new tab",
    "notification center", "action center", "windows powershell", "command prompt",
    "windows input experience", "settings", "file explorer",
}


def foreground_window_title() -> str:
    """Title of the window you're currently in (Windows only; '' elsewhere)."""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value.strip()
    except Exception:
        return ""


APP_SUFFIXES = (" - google chrome", " - microsoft edge", " - mozilla firefox", " - brave", " - opera",
                " - google search", " - bing", " - duckduckgo", " - adobe acrobat reader", " - adobe acrobat",
                " - microsoft word", " - word", " - powerpoint", " - google docs", " - google slides", " - youtube")
TERMINAL_HINTS = ("cmd.exe", "powershell", "windows terminal", "python main.py", "python  main.py", "command prompt")


def clean_title(title: str) -> str:
    """'what is 5+3 - Google Search - Google Chrome' -> 'what is 5+3'"""
    t = title.strip()
    changed = True
    while changed:
        changed = False
        for suf in APP_SUFFIXES:
            if t.lower().endswith(suf):
                t = t[: -len(suf)].strip()
                changed = True
    return t


def useful_title(title: str, own_window: str) -> bool:
    t = title.strip()
    low = t.lower()
    if not t or t.startswith(own_window) or any(h in low for h in TERMINAL_HINTS):
        return False
    return clean_title(t).lower() not in JUNK_TITLES


SYSTEM = f"""You write a quick wake-up quiz for a student who just took a short break from studying.

Write {NUM_QUESTIONS} multiple-choice questions with 4 options each and exactly one correct answer.
Base them on what the student is studying. You get:
- window titles they had open, most recent first (file names like "CSE214_Lecture7_Heaps.pdf",
  page titles, search queries like "what is the square root of 81")
- optionally, a topic they typed at the start of the night

The MOST RECENT title is what they were looking at right before the break: if it's study
material, center the quiz on it (at least 2 of the questions), even if it differs from the typed
topic. Use older titles and the typed topic for context or to fill the remaining questions.
Make topic_label describe what the questions actually cover.

Rules:
- Recall-focused, answerable in about 15 seconds by someone who studied it. Start easy.
- Question under 110 characters. Each option under 45 characters. Explanation under 120 characters.
- Plausible wrong options, no "all of the above".
- If you can't tell what academic subject they're studying (games, social media, unclear titles),
  return an empty topic_label and no questions.
Answer only by calling give_quiz."""

TOOL = {
    "name": "give_quiz",
    "description": "Return the quiz.",
    "input_schema": {
        "type": "object",
        "properties": {
            "topic_label": {"type": "string", "description": "Short subject name, e.g. 'Heaps (CSE 214)'"},
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "options": {"type": "array", "items": {"type": "string"}, "minItems": 4, "maxItems": 4},
                        "answer_index": {"type": "integer", "minimum": 0, "maximum": 3},
                        "explanation": {"type": "string"},
                    },
                    "required": ["question", "options", "answer_index", "explanation"],
                },
            },
        },
        "required": ["topic_label", "questions"],
    },
}


LAST_RAW: dict = {}   # last unusable answer, for troubleshooting


class QuizError(Exception):
    """Why no quiz could be made (shown to the user)."""


def _as_list(v):
    """Models sometimes send a list as a JSON string; accept both."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v if isinstance(v, list) else []


def _clean(raw: dict) -> dict | None:
    """Keep only well-formed questions; None if nothing usable."""
    questions = []
    for q in _as_list((raw or {}).get("questions")):
        if isinstance(q, str):
            try:
                q = json.loads(q)
            except ValueError:
                continue
        try:
            opts = [str(o).strip() for o in _as_list(q["options"])]
            idx = int(q["answer_index"])
            text = str(q["question"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if text and len(opts) == 4 and all(opts) and 0 <= idx < 4:
            questions.append({"question": text, "options": opts, "answer_index": idx,
                              "explanation": str(q.get("explanation", "")).strip()})
    if not questions:
        return None
    label = str(raw.get("topic_label") or "").strip() or "What you were studying"
    return {"topic_label": label, "questions": questions[:NUM_QUESTIONS]}


def make_quiz(client, topic: str, titles: list[str]) -> dict:
    """One Claude call. Returns {"topic_label", "questions": [...]}; raises QuizError otherwise."""
    if not topic.strip() and not titles:
        raise QuizError("nothing to go on (press T to set a topic)")
    lines = ["Recent window titles (most recent first):"]
    lines += [f"- {clean_title(t)}" for t in titles] or ["- (none)"]
    lines.append(f"Typed topic for the night: {topic.strip() or '(none)'}")
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        system=SYSTEM,
        tools=[TOOL],   # no forced tool_choice: not every model supports it; the prompt asks for the tool
        messages=[{"role": "user", "content": "\n".join(lines)}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "give_quiz":
            raw = dict(block.input)
            q = _clean(raw)
            if q:
                return q
            if not _as_list(raw.get("questions")):
                raise QuizError("Claude couldn't tell what subject you're studying")
            print(f"[quiz] unusable answer from Claude: {json.dumps(raw)[:800]}")
            LAST_RAW["raw"] = raw
            raise QuizError("Claude's questions came back malformed")
    # Fallback: Claude answered in text. Look for the quiz as JSON in it.
    text = " ".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            q = _clean(json.loads(text[start:end + 1]))
            if q:
                return q
        except ValueError:
            pass
    print(f"[quiz] Claude replied without a quiz: {text[:300]!r}")
    raise QuizError("Claude replied without a quiz")
