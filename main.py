"""Sleep Agent: watches for microsleeps and asks Claude what you should do.

Run:  python main.py

Flow:
  1. Fatigue detected -> popup with a 15 s countdown. Nothing is sent to Claude yet.
       F / "False alarm"         cancel + snooze, no Claude call, nothing booked
       Enter / "I'm tired"       skip the countdown
  2. Countdown ends -> Claude checks your calendar, may book a nap/sleep block.
  3. Recommendation panel:
       Enter / "Quit & rest"           nap/sleep advice: close the app
       Enter / "Done, back to work"    quick-reset advice: keep watching
       F / "Undo - false alarm"        delete the event it booked, snooze

Other keys (click the video window first):
  s  snooze 5 min (press again to add 5 more, max 60)    u  resume now
  c  recalibrate     a  ask the agent now (demo)          d  debug overlay     q  quit
"""

from __future__ import annotations

import os
import textwrap
import threading
import time
from collections import deque
from datetime import datetime

import cv2
import numpy as np
from dotenv import load_dotenv

load_dotenv()

import alerts
import quiz
import report
from datetime import timedelta
from agent import SleepAgent
from detector import LEFT_EYE, RIGHT_EYE, FatigueDetector, FrameStats

CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "0"))
WINDOW_MIN = float(os.getenv("FATIGUE_WINDOW_MINUTES", "10"))
AGENT_TRIGGER_SCORE = float(os.getenv("AGENT_TRIGGER_SCORE", "2"))
# Points per event: a microsleep starts at 1; if the eyes stay shut past LONG_MICROSLEEP_SECONDS
# the same closure is upgraded to LONG_MICROSLEEP_POINTS (2 = enough to open the popup alone).
MICROSLEEP_POINTS = float(os.getenv("MICROSLEEP_POINTS", "1"))
LONG_MICROSLEEP_POINTS = float(os.getenv("LONG_MICROSLEEP_POINTS", "2"))
YAWN_POINTS = float(os.getenv("YAWN_POINTS", "0.5"))
STILL_POINTS = float(os.getenv("STILL_POINTS", "2"))   # no movement for STILL_SECONDS
AGENT_COOLDOWN_S = float(os.getenv("AGENT_COOLDOWN_SECONDS", "300"))
CONFIRM_S = float(os.getenv("CONFIRM_SECONDS", "15"))
SPEAK = os.getenv("SPEAK_RECOMMENDATIONS", "true").lower() in ("1", "true", "yes")
SNOOZE_MIN = float(os.getenv("SNOOZE_MINUTES", "5"))
SNOOZE_MAX_MIN = 60

WINDOW = "Sleep Agent"
DISPLAY_SCALE = float(os.getenv("DISPLAY_SCALE", "1.5"))  # window size only; detection still uses camera frames
NAP_ALARM_MAX_MIN = float(os.getenv("NAP_ALARM_MAX_MINUTES", "20"))  # naps this short get an in-app alarm
WAKE_REPEAT_S = float(os.getenv("WAKE_REPEAT_SECONDS", "60"))       # replay the wake alarm until "I'm up"
QUIZ_ENABLED = os.getenv("QUIZ_ENABLED", "true").lower() in ("1", "true", "yes")
ASK_TOPIC = os.getenv("ASK_TOPIC_AT_START", "true").lower() in ("1", "true", "yes")
TITLE_POLL_S = 3.0      # how often to note which window you're in (for the quiz topic)
QUIZ_WAIT_MAX_S = 25.0  # give up waiting for a quiz that's still generating
REPORT_ON_QUIT = os.getenv("REPORT_ON_QUIT", "true").lower() in ("1", "true", "yes")
FOCUS_GRACE_S = 1.0   # ignore keys this long after grabbing focus (stray typing from another app)
KEY_ENTER = (13, 10)
KEY_SPACE = 32

ACTION_COLORS = {"sleep": (200, 120, 60), "nap": (60, 160, 230), "quick_reset": (60, 60, 230)}
ACTION_LABELS = {"sleep": "GO TO BED", "nap": "TAKE A NAP", "quick_reset": "QUICK RESET"}
FONT = cv2.FONT_HERSHEY_SIMPLEX


TITLE_BAR_PX = 40   # room for the window's title bar and borders
SCREEN_MARGIN_PX = 20


def _work_area():
    """(left, top, width, height) of the screen minus the taskbar, or None if unknown."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        return None


def fit_window(frame_w: int, frame_h: int, want_scale: float, area):
    """Largest scale <= want_scale that fits the screen, and the centered (x, y) position."""
    if not area:
        return want_scale, None
    left, top, aw, ah = area
    fit = min(want_scale, (aw - 2 * SCREEN_MARGIN_PX) / frame_w,
              (ah - 2 * SCREEN_MARGIN_PX - TITLE_BAR_PX) / frame_h)
    scale = max(0.5, fit)
    dw, dh = int(frame_w * scale), int(frame_h * scale) + TITLE_BAR_PX
    x = left + max(0, (aw - dw) // 2)
    y = top + max(0, (ah - dh) // 2)
    return scale, (x, y)


def _focus_window_windows(title: str):
    """Windows only lets the foreground app hand out focus. Briefly attach our input
    to the foreground app's thread so SetForegroundWindow is allowed; fall back to a
    synthetic Alt tap (another accepted way to unlock it)."""
    try:
        import ctypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE, in case it was minimized
        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            return
        fg_thread = user32.GetWindowThreadProcessId(fg, None)
        our_thread = kernel32.GetCurrentThreadId()
        attached = bool(fg_thread) and fg_thread != our_thread and user32.AttachThreadInput(our_thread, fg_thread, True)
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(our_thread, fg_thread, False)
        if user32.GetForegroundWindow() != hwnd:
            user32.keybd_event(0x12, 0, 0, 0)  # Alt down
            user32.keybd_event(0x12, 0, 2, 0)  # Alt up
            user32.SetForegroundWindow(hwnd)
    except Exception as e:
        print(f"[focus] couldn't focus window: {e}")


def _work_area() -> tuple[int, int, int, int] | None:
    """Usable screen area (left, top, right, bottom), excluding the taskbar. Windows only."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
        return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        return None


def center_window(title: str, frame_w: int, frame_h: int):
    """Place the window in the middle of the usable screen (never off the edges)."""
    area = _work_area()
    if area is None:
        cv2.moveWindow(title, 40, 40)
        return
    left, top, right, bottom = area
    win_w, win_h = frame_w + 16, frame_h + 40   # rough border + title bar allowance
    x = left + max(0, (right - left - win_w) // 2)
    y = top + max(0, (bottom - top - win_h) // 2)
    cv2.moveWindow(title, x, y)


class App:
    def __init__(self):
        self.detector = FatigueDetector(
            microsleep_seconds=float(os.getenv("MICROSLEEP_SECONDS", "0.8")),
            long_microsleep_seconds=float(os.getenv("LONG_MICROSLEEP_SECONDS", "10")),
            ear_closed_fraction=float(os.getenv("EAR_CLOSED_FRACTION", "0.50")),
            ear_deep_fraction=float(os.getenv("EAR_DEEP_FRACTION", "0.30")),
            look_down_threshold=float(os.getenv("LOOK_DOWN_THRESHOLD", "0.45")),
            commit_seconds=float(os.getenv("NOD_COMMIT_SECONDS", "0.4")),
            head_down_factor=float(os.getenv("HEAD_DOWN_FACTOR", "1.60")),
            still_seconds=float(os.getenv("STILL_SECONDS", "60")),
        )
        self.agent = None
        try:
            self.agent = SleepAgent()
        except Exception as e:
            print(f"[agent] disabled: {e}  (set ANTHROPIC_API_KEY in .env)")
        self.events = deque()
        self.longest_closure = 0.0
        self.session_start = time.time()
        self.agent_busy = False
        self.last_agent_call = 0.0
        self.pending_since: float | None = None   # confirm popup open since
        self.recommendation: dict | None = None
        self.banner: tuple[str, float] | None = None  # (text, expires_at)
        self.debug = os.getenv("DEBUG_OVERLAY", "false").lower() in ("1", "true", "yes")
        self.snooze_until = 0.0
        self.buttons: list[tuple[tuple[int, int, int, int], callable]] = []
        self.quit_requested = False
        self.focus_grace_until = 0.0
        self.nap_until: float | None = None      # desk nap running: wake-up time (epoch)
        self.waking_since: float | None = None   # nap over, alarm ringing
        self.last_wake_alarm = 0.0
        self.log = report.SessionLog()
        self.morning = self.log.pending_morning()   # "when did you get up?" from last night
        self.morning_choice = self.morning["default"] if self.morning else None
        # quiz
        self.topic = ""
        self.topic_editing = QUIZ_ENABLED and ASK_TOPIC and self.agent is not None
        self.topic_buffer = ""
        self.topic_startup = self.topic_editing   # calibrate after this, not while you type
        self.recent_titles: deque[str] = deque(maxlen=5)
        self.last_title_poll = 0.0
        self.prepared_quiz: dict | None = None
        self.quiz_generating = False
        self.quiz_active: dict | None = None
        self.quiz_idx = 0
        self.quiz_choice: int | None = None
        self.quiz_score = 0
        self.quiz_waiting_since: float | None = None
        self.quiz_after = None
        self.quiz_error = ""
        self.display_scale = DISPLAY_SCALE
        self.window_placed = False

    # ---------------------------------------------------------------- fatigue
    def _prune(self):
        cutoff = time.time() - WINDOW_MIN * 60
        while self.events and self.events[0].timestamp < cutoff:
            self.events.popleft()

    def fatigue_summary(self) -> dict:
        self._prune()
        ms = [e for e in self.events if e.kind == "microsleep"]
        ys = [e for e in self.events if e.kind == "yawn"]
        return {
            f"microsleeps_last_{int(WINDOW_MIN)}_min": len(ms),
            f"yawns_last_{int(WINDOW_MIN)}_min": len(ys),
            f"motionless_spells_last_{int(WINDOW_MIN)}_min": sum(1 for e in self.events if e.kind == "stillness"),
            "longest_eye_closure_s": round(max([e.duration for e in ms], default=self.longest_closure), 1),
            "session_minutes": int((time.time() - self.session_start) / 60),
            **({"studying": self.topic} if self.topic else {}),
        }

    def score(self) -> float:
        self._prune()
        return sum(getattr(e, "points", 0.0) for e in self.events)

    def show_banner(self, text: str, seconds: float = 4):
        self.banner = (text, time.time() + seconds)

    @property
    def cooldown_left(self) -> float:
        """Quiet period after the agent actually gave advice (not after false alarms)."""
        return max(0.0, self.last_agent_call + AGENT_COOLDOWN_S - time.time())

    def should_trigger(self) -> bool:
        return (not self.busy and not self.snooze_left and not self.cooldown_left
                and self.score() >= AGENT_TRIGGER_SCORE)

    @property
    def tracking_paused(self) -> bool:
        """Snoozed, napping or waking up: camera is turned off."""
        return (bool(self.snooze_left) or self.nap_until is not None or self.waking_since is not None
                or self.quiz_on_screen)

    @property
    def quiz_on_screen(self) -> bool:
        return self.quiz_active is not None or self.quiz_waiting_since is not None

    @property
    def busy(self) -> bool:
        """A popup, agent call or recommendation is already on screen."""
        return (self.pending_since is not None or self.agent_busy or self.recommendation is not None
                or self.nap_until is not None or self.waking_since is not None or self.morning is not None
                or self.topic_editing or self.quiz_on_screen)

    # ---------------------------------------------------------------- snooze
    @property
    def snooze_left(self) -> float:
        return max(0.0, self.snooze_until - time.time())

    def snooze(self, minutes: float = SNOOZE_MIN):
        now = time.time()
        self.snooze_until = min(max(now, self.snooze_until) + minutes * 60, now + SNOOZE_MAX_MIN * 60)
        self.events.clear()          # whatever triggered this was probably a false alarm
        self.pending_since = None    # snoozing also cancels a pending popup
        self.banner = None
        print(f"[snooze] paused for {self.snooze_left / 60:.0f} min")

    def unsnooze(self):
        if self.snooze_left:
            self.snooze_until = 0.0
            print("[snooze] resumed")
            self.show_banner("Watching again", 2)

    # ---------------------------------------------------------------- flow
    def on_event(self, ev):
        if self.snooze_left:
            print(f"[snooze] ignored {ev.kind}")
            return
        if self.busy:
            return  # already handling it
        if ev.kind == "long_microsleep":
            self.longest_closure = max(self.longest_closure, ev.duration)
            last = self.events[-1] if self.events else None
            if last is not None and last.kind == "microsleep" and ev.timestamp - last.timestamp < 30:
                last.points = LONG_MICROSLEEP_POINTS      # same closure: upgrade, don't double count
                last.duration = ev.duration
            else:
                ev.kind, ev.points = "microsleep", LONG_MICROSLEEP_POINTS
                self.events.append(ev)
                self.log.fatigue("microsleep")
            self.log.fatigue("long_microsleep")
            print(f"[detector] long microsleep ({ev.duration:.1f}s) -> {LONG_MICROSLEEP_POINTS:g} pts")
            if self.should_trigger():
                self.open_confirm()
            return

        ev.points = {"microsleep": MICROSLEEP_POINTS, "yawn": YAWN_POINTS,
                     "stillness": STILL_POINTS}.get(ev.kind, 0.0)
        self.events.append(ev)
        self.log.fatigue(ev.kind)
        if ev.kind == "microsleep":
            self.longest_closure = max(self.longest_closure, ev.duration)
            print(f"[detector] microsleep ({ev.duration:.1f}s)")
        elif ev.kind == "stillness":
            print(f"[detector] no movement for {ev.duration:.0f}s -> {STILL_POINTS:g} pts")
        else:
            print("[detector] yawn")

        if self.should_trigger():
            self.open_confirm()
        elif ev.kind == "microsleep":
            alerts.beep("nudge")
            self.show_banner("Eyes closed - stay with me!")

    def open_confirm(self):
        """Step 1: popup + countdown. No Claude call, nothing booked yet."""
        self.pending_since = time.time()
        self.banner = None
        alerts.alarm()
        self.bring_to_front()
        print(f"[confirm] waiting {CONFIRM_S:.0f}s for a false alarm")

    def bring_to_front(self):
        """Pop the video window above other apps AND give it keyboard focus, so the
        popup's keys work without clicking the window first."""
        try:
            cv2.setWindowProperty(WINDOW, cv2.WND_PROP_TOPMOST, 1)
        except Exception:
            pass
        if os.name == "nt":
            _focus_window_windows(WINDOW)
        self.focus_grace_until = time.time() + FOCUS_GRACE_S

    @staticmethod
    def release_front():
        try:
            cv2.setWindowProperty(WINDOW, cv2.WND_PROP_TOPMOST, 0)
        except Exception:
            pass

    def tick(self):
        now = time.time()
        if QUIZ_ENABLED and not self.tracking_paused and now - self.last_title_poll >= TITLE_POLL_S:
            self.last_title_poll = now
            title = quiz.foreground_window_title()
            if quiz.useful_title(title, WINDOW):
                if title in self.recent_titles:
                    self.recent_titles.remove(title)
                self.recent_titles.append(title)
        if self.quiz_waiting_since is not None:
            if not self.quiz_generating:
                self.quiz_waiting_since = None
                if self.prepared_quiz:
                    self._start_quiz(self.prepared_quiz)
                else:
                    self._finish_quiz(None)
            elif now - self.quiz_waiting_since > QUIZ_WAIT_MAX_S:
                self.quiz_waiting_since = None
                self.quiz_error = "Claude took too long to answer"
                self.log.debug("quiz_timeout", f"waited {QUIZ_WAIT_MAX_S:.0f}s")
                self._finish_quiz(None)
        if self.nap_until is not None and now >= self.nap_until:
            self.wake_up()
        elif self.waking_since is not None and now - self.last_wake_alarm >= WAKE_REPEAT_S:
            self.last_wake_alarm = now
            alerts.wake_alarm()
            self.bring_to_front()
        if self.pending_since is not None and time.time() - self.pending_since >= CONFIRM_S:
            self.go_now()
        elif self.should_trigger():
            self.open_confirm()

    def go_now(self):
        if self.pending_since is None:
            return
        self.pending_since = None
        alerts.stop_alarm()
        self.log.popup("agent")
        self.ask_agent()

    def false_alarm(self):
        alerts.stop_alarm()
        self.release_front()
        if self.pending_since is not None:
            self.log.popup("false_alarm")
            self.snooze()
            self.show_banner(f"False alarm - nothing booked. Snoozed {SNOOZE_MIN:g} min.", 4)
            print("[confirm] false alarm, cancelled before calling Claude")
            return
        if self.recommendation is not None:
            booked = self.recommendation.get("booked")
            self.recommendation = None
            self.log.undo()
            self.last_agent_call = 0.0   # it was a false alarm, so no cooldown
            self.snooze()
            if booked and self.agent:
                self.show_banner("False alarm - removing the calendar block...", 6)

                def _undo():
                    try:
                        ok = self.agent.undo_booking(booked)
                        msg = "Removed it from your calendar." if ok else "Couldn't find that event to remove."
                    except Exception as e:
                        msg = f"Couldn't remove the event ({e})."
                    print(f"[undo] {msg}")
                    self.show_banner(f"{msg} Snoozed {SNOOZE_MIN:g} min.", 5)

                threading.Thread(target=_undo, daemon=True).start()
            else:
                self.show_banner(f"Dismissed. Snoozed {SNOOZE_MIN:g} min.", 4)

    def dismiss(self):
        self.release_front()
        if self.recommendation is not None:
            self.recommendation = None
            self.events.clear()  # fresh start after acting on advice

    def is_rest(self) -> bool:
        return bool(self.recommendation) and self.recommendation.get("action") in ("sleep", "nap")

    def is_desk_nap(self) -> bool:
        """Short nap: stays at the desk, app times it and rings the alarm."""
        rec = self.recommendation
        if not rec or rec.get("action") != "nap":
            return False
        minutes = (rec.get("booked") or {}).get("minutes") or rec.get("minutes") or 0
        return 0 < minutes <= NAP_ALARM_MAX_MIN

    def accept(self):
        """Primary button: short nap -> start timer; other rest -> quit; quick reset -> back to work."""
        if self.is_desk_nap():
            self.start_nap()
        elif self.is_rest():
            self.quit_and_rest()
        else:
            self.dismiss()
            self.show_quiz(after=None)

    def start_nap(self):
        rec = self.recommendation
        booked = rec.get("booked") or {}
        minutes = booked.get("minutes") or rec.get("minutes") or 20
        end = time.time() + minutes * 60
        try:  # ring at the calendar block's end so app and calendar agree
            cal_end = datetime.fromisoformat(booked["end"]).timestamp()
            if cal_end > time.time() + 60:
                end = cal_end
        except Exception:
            pass
        self.nap_until = end
        self.recommendation = None
        self.log.nap_start(minutes)
        self.request_quiz()   # generate while you nap
        self.events.clear()
        self.release_front()
        alerts.stop_alarm()
        print(f"[nap] {minutes} min, alarm at {datetime.fromtimestamp(end):%I:%M %p}")

    def end_nap_early(self):
        if self.nap_until is not None:
            self.nap_until = None
            self.log.nap_end()
            self.show_quiz(after=lambda: self.after_nap("Nap ended early."))

    def wake_up(self):
        self.nap_until = None
        self.log.nap_end()   # asleep until the alarm; time spent waking up isn't sleep
        self.waking_since = self.last_wake_alarm = time.time()
        print("[nap] time's up - wake up!")
        alerts.wake_alarm()
        alerts.notify("Sleep Agent: nap's over", "Time to get back to it.")
        self.bring_to_front()

    def im_up(self):
        if self.waking_since is not None:
            self.waking_since = None
            alerts.stop_alarm()
            self.release_front()
            self.show_quiz(after=lambda: self.after_nap("Welcome back!"))

    # ---------------------------------------------------------------- quiz
    def request_quiz(self):
        """Start generating a quiz in the background (one Claude call)."""
        if not QUIZ_ENABLED or not self.agent or self.quiz_generating:
            self.log.debug("quiz_request_ignored", f"enabled={QUIZ_ENABLED} agent={self.agent is not None} "
                                                   f"generating={self.quiz_generating}")
            return
        titles = list(reversed(self.recent_titles))   # most recent first
        if not self.topic and not titles:
            self.quiz_error = "nothing to go on (press T to set a topic)"
            return
        self.prepared_quiz = None
        self.quiz_error = ""
        self.quiz_generating = True
        self.log.debug("quiz_request", f"model={quiz.MODEL} topic={self.topic!r} titles={titles}")
        topic, client = self.topic, getattr(self.agent, "client", None)
        if client is None:
            self.quiz_generating = False
            return

        def _run():
            try:
                q = quiz.make_quiz(client, topic, titles)
                print(f"[quiz] ready: {q['topic_label']} ({len(q['questions'])} questions)")
                self.log.debug("quiz_ready", q["topic_label"])
            except Exception as e:
                q = None
                self.quiz_error = str(e) if isinstance(e, quiz.QuizError) else f"API error: {e}"
                print(f"[quiz] couldn't make a quiz: {self.quiz_error}")
                import traceback
                raw = quiz.LAST_RAW.pop("raw", None)
                self.log.debug("quiz_error", f"{type(e).__name__}: {e}\nraw={raw}\n{traceback.format_exc()}")
            self.prepared_quiz = q
            self.quiz_generating = False

        threading.Thread(target=_run, daemon=True).start()

    def show_quiz(self, after=None):
        """Show the quiz if there is one; `after` runs when it's done or skipped."""
        self.quiz_after = after
        if self.prepared_quiz:
            self._start_quiz(self.prepared_quiz)
        elif self.quiz_generating:
            self.quiz_waiting_since = time.time()
        else:
            self._finish_quiz(None)

    def _start_quiz(self, q: dict):
        self.quiz_active, self.prepared_quiz = q, None
        self.quiz_idx, self.quiz_choice, self.quiz_score = 0, None, 0
        self.quiz_waiting_since = None

    def quiz_answer(self, i: int):
        q = self.quiz_active
        if q and self.quiz_idx < len(q["questions"]) and self.quiz_choice is None:
            self.quiz_choice = i
            if i == q["questions"][self.quiz_idx]["answer_index"]:
                self.quiz_score += 1

    def quiz_next(self):
        q = self.quiz_active
        if not q:
            return
        if self.quiz_idx >= len(q["questions"]):
            self._finish_quiz(False)          # results screen -> done
        elif self.quiz_choice is not None:
            self.quiz_idx += 1
            self.quiz_choice = None

    def quiz_skip(self):
        self._finish_quiz(True)

    def _finish_quiz(self, skipped):
        """skipped: True/False for a shown quiz, None if there was no quiz at all."""
        q = self.quiz_active
        if q is not None:
            answered = self.quiz_idx + (1 if self.quiz_choice is not None else 0)
            self.log.quiz_result(q["topic_label"], self.quiz_score, min(answered, len(q["questions"])),
                                 len(q["questions"]), bool(skipped))
        elif skipped is None and QUIZ_ENABLED and self.agent is not None and self.quiz_error:
            print(f"[quiz] no quiz: {self.quiz_error}")
            self.show_banner(f"No quiz this time: {self.quiz_error}", 6)
        self.quiz_active = None
        self.quiz_waiting_since = None
        after, self.quiz_after = self.quiz_after, None
        if after:
            after()
            if skipped is None and self.quiz_error:   # after() may replace the banner; keep the reason visible
                self.show_banner(f"No quiz this time: {self.quiz_error}", 6)

    # ---------------------------------------------------------------- study topic
    def start_topic_edit(self):
        self.topic_editing = True
        self.topic_buffer = self.topic

    def topic_key(self, key: int):
        if key in KEY_ENTER:
            self.topic = self.topic_buffer.strip()[:80]
            self.topic_editing = False
            self.log.set_topic(self.topic)
            self.show_banner(f"Studying: {self.topic}" if self.topic else "No topic: the quiz will use your window titles.", 3)
            self._topic_closed()
        elif key == 27:
            self.topic_editing = False
            self._topic_closed()
        elif key == 8:
            self.topic_buffer = self.topic_buffer[:-1]
        elif 32 <= key <= 126 and len(self.topic_buffer) < 80:
            self.topic_buffer += chr(key)

    def _topic_closed(self):
        if self.topic_startup:
            self.topic_startup = False
            self.detector.recalibrate()   # you were looking at the keyboard while typing

    def topic_save(self):
        self.topic_key(13)

    def topic_skip(self):
        self.topic_key(27)

    def after_nap(self, msg: str):
        self.detector.recalibrate()          # you've probably moved; runs when the camera comes back
        self.snooze()                        # regular snooze while you wake up (camera stays off)
        self.show_banner(f"{msg} Snoozed {SNOOZE_MIN:g} min while you wake up.", 5)

    def quit_and_rest(self):
        rec = self.recommendation or {}
        booked = rec.get("booked") or {}
        planned = None
        try:
            planned = datetime.fromisoformat(booked["end"])
        except Exception:
            if rec.get("minutes"):
                planned = report.now() + timedelta(minutes=rec["minutes"])
        self.log.to_bed(planned)
        print("[app] quitting - get some rest!")
        self.quit_requested = True

    # ---------------------------------------------------------------- morning question
    def morning_adjust(self, minutes: int):
        if self.morning:
            lo = self.morning["bed_start"] + timedelta(minutes=5)
            hi = self.morning["bed_start"] + timedelta(hours=16)
            self.morning_choice = min(max(self.morning_choice + timedelta(minutes=minutes), lo), hi)

    def morning_confirm(self):
        if self.morning:
            self.log.confirm_wake(self.morning, self.morning_choice)
            slept = (self.morning_choice - self.morning["bed_start"]).total_seconds()
            self.morning = None
            report.write_report()
            self.show_banner(f"Got it: {report.fmt_dur(slept)} in bed. Report updated (R to view).", 5)

    def morning_skip(self):
        if self.morning:
            self.morning = None
            self.show_banner("Kept the calendar estimate.", 3)

    def user_snooze(self):
        self.log.snooze()
        self.snooze()

    def open_report(self):
        path = report.write_report(open_browser=True)
        self.show_banner("Opened your report in the browser." if path else "Couldn't write the report.", 3)

    def ask_agent(self):
        if self.agent_busy:
            return
        if not self.agent:
            self.show_banner("Agent off: add ANTHROPIC_API_KEY to .env", 6)
            return
        self.agent_busy = True
        self.last_agent_call = time.time()
        self.show_banner("Checking your calendar...", 60)

        def _run():
            try:
                rec = self.agent.decide(self.fatigue_summary())
            except Exception as e:
                rec = {"action": "quick_reset", "message": f"(agent error: {e}) Get up and drink some water."}
            print(f"[agent] {rec}")
            self.log.advice(rec)
            if rec.get("action") == "quick_reset":
                self.request_quiz()   # ready by the time you're back
            self.recommendation = rec
            self.banner = None
            label = ACTION_LABELS.get(rec.get("action"), "TAKE A BREAK")
            alerts.notify(f"Sleep Agent: {label}", rec.get("message", ""))
            if SPEAK:
                alerts.speak(rec.get("message", ""))
            self.agent_busy = False

        threading.Thread(target=_run, daemon=True).start()

    # ---------------------------------------------------------------- input
    def on_mouse(self, event, x, y, flags, param):
        if event != cv2.EVENT_LBUTTONDOWN:
            return
        for (x1, y1, x2, y2), action in reversed(self.buttons):  # topmost first
            if x1 <= x <= x2 and y1 <= y <= y2:
                action()
                return

    def on_key(self, key: int):
        if time.time() < self.focus_grace_until:
            return  # probably typing meant for another app when we took focus
        if self.topic_editing and self.morning is None:
            self.topic_key(key)   # typing: every key is a letter, even q
            return
        if key == ord("q"):
            self.quit_requested = True
        elif self.morning is not None:                  # "when did you get up?"
            if key in KEY_ENTER:
                self.morning_confirm()
            elif key == 27:
                self.morning_skip()
            elif key in (ord("-"), ord("_")):
                self.morning_adjust(-15)
            elif key in (ord("="), ord("+")):
                self.morning_adjust(15)
        elif self.quiz_on_screen:                       # quiz
            q = self.quiz_active
            if key == 27:
                self.quiz_skip()
            elif q and self.quiz_idx < len(q["questions"]) and ord("1") <= key <= ord("4"):
                self.quiz_answer(key - ord("1"))
            elif q and key in KEY_ENTER:
                self.quiz_next()
        elif self.waking_since is not None:             # wake-up alarm
            if key in KEY_ENTER or key in (KEY_SPACE, ord("f"), 27):
                self.im_up()
        elif self.nap_until is not None:                # nap running
            if key in KEY_ENTER:
                self.end_nap_early()
        elif self.pending_since is not None:            # confirm popup
            if key == ord("f"):
                self.false_alarm()
            elif key in KEY_ENTER:
                self.go_now()
            elif key == ord("s"):
                self.false_alarm()
        elif self.recommendation is not None:           # recommendation panel
            if key == ord("f"):
                self.false_alarm()
            elif key in KEY_ENTER or key == KEY_SPACE:
                self.accept()
        elif key == ord("s"):
            self.user_snooze()
        elif key == ord("z") and QUIZ_ENABLED and self.agent is not None:
            self.request_quiz()        # quiz me now (testing / demo)
            self.show_quiz(after=None)
        elif key == ord("t") and QUIZ_ENABLED and self.agent is not None:
            self.start_topic_edit()
        elif key == ord("r"):
            self.open_report()
        elif key == ord("u"):
            self.unsnooze()
        elif key == ord("c"):
            self.detector.recalibrate()
        elif key == ord("a"):
            self.ask_agent()
        elif key == ord("d"):
            self.debug = not self.debug

    # ---------------------------------------------------------------- drawing
    def button(self, frame, rect, label, action, bg=(70, 70, 70), border=(230, 230, 230), scale=0.55):
        x1, y1, x2, y2 = rect
        cv2.rectangle(frame, (x1, y1), (x2, y2), bg, -1)
        cv2.rectangle(frame, (x1, y1), (x2, y2), border, 1)
        (tw, th), _ = cv2.getTextSize(label, FONT, scale, 1)
        while tw > (x2 - x1) - 12 and scale > 0.3:   # shrink to fit narrow buttons
            scale -= 0.03
            (tw, th), _ = cv2.getTextSize(label, FONT, scale, 1)
        cv2.putText(frame, label, (x1 + (x2 - x1 - tw) // 2, y1 + (y2 - y1 + th) // 2), FONT, scale,
                    (255, 255, 255), 1, cv2.LINE_AA)
        self.buttons.append((rect, action))

    def draw(self, frame, stats):
        self.buttons = []
        h, w = frame.shape[:2]

        # status line
        if self.quiz_on_screen:
            status, color = "Quick quiz - not watching", (255, 200, 0)
        elif self.waking_since is not None:
            status, color = "NAP'S OVER - WAKE UP!", (0, 0, 255)
        elif self.nap_until is not None:
            m, sec = divmod(int(max(0, self.nap_until - time.time())), 60)
            status, color = f"NAPPING {m}:{sec:02d} left - not watching", (255, 200, 0)
        elif self.snooze_left:
            m, sec = divmod(int(self.snooze_left), 60)
            status, color = f"SNOOZED {m}:{sec:02d} - not watching (u = resume)", (255, 200, 0)
        elif stats.nodding:
            status, color = f"NODDING OFF {stats.closed_for:.1f}s", (0, 0, 255)
        elif not stats.face_found:
            status, color = "No face detected", (0, 165, 255)
        elif stats.calibrating:
            status, color = "Calibrating... sit like you normally study", (0, 200, 255)
        elif stats.head_down:
            status, color = "Head down (reading?) - not counted", (200, 200, 0)
        elif stats.gaze_down:
            status, color = "Eyes looking down - not counted", (200, 200, 0)
        elif stats.eyes_closed:
            status, color = f"EYES CLOSED {stats.closed_for:.1f}s", (0, 0, 255)
        elif stats.still_for >= self.detector.still_seconds * 0.5:
            status, color = f"No movement {stats.still_for:.0f}s", (0, 165, 255)
        else:
            status, color = "Awake", (0, 200, 0)
        cv2.rectangle(frame, (0, 0), (w, 36), (0, 0, 0), -1)
        cv2.putText(frame, status, (10, 25), FONT, 0.7, color, 2)
        fat = f"fatigue {self.score():.1f}/{AGENT_TRIGGER_SCORE:g}"
        if self.cooldown_left and not self.busy:
            m, sec = divmod(int(self.cooldown_left), 60)
            fat += f"  rest {m}:{sec:02d}"
        (tw, _), _ = cv2.getTextSize(fat, FONT, 0.6, 1)
        cv2.putText(frame, fat, (w - tw - 10, 25), FONT, 0.6, (255, 255, 255), 1)

        if not self.busy and not self.snooze_left:
            label = "Snooze +5 min"
            self.button(frame, (w - 160, 44, w - 10, 76), label, self.user_snooze, bg=(90, 60, 20), border=(255, 200, 0))

        if self.debug and stats.face_found:
            for i in LEFT_EYE + RIGHT_EYE:
                x, y = stats.landmarks_px[i]
                cv2.circle(frame, (int(x), int(y)), 2, (0, 255, 0), -1)
            thr = self.detector.ear_threshold
            txt = f"EAR {stats.ear:.2f} (thr {thr:.2f})" if thr else f"EAR {stats.ear:.2f}"
            txt += f"  look-down {stats.look_down:.2f}  MAR {stats.mar:.2f}"
            if self.detector.pitch_baseline:
                tilt = stats.pitch / self.detector.pitch_baseline
                txt += f"  head tilt {tilt:.2f}x (reading at {self.detector.head_down_factor:.2f}x)"
            cv2.putText(frame, txt, (10, 58), FONT, 0.5, (255, 255, 255), 1)

        if self.banner and time.time() < self.banner[1]:
            self._panel(frame, self.banner[0], (40, 40, 40))

        if self.morning is not None:
            self._draw_morning(frame)
        elif self.topic_editing:
            self._draw_topic(frame)
        if self.quiz_on_screen:
            self._draw_quiz(frame)
        if self.snooze_left and not self.busy:
            self._draw_snooze(frame)
        if self.waking_since is not None:
            self._draw_wake(frame)
        elif self.nap_until is not None:
            self._draw_nap(frame)
        elif self.pending_since is not None:
            self._draw_confirm(frame)
        elif self.recommendation is not None:
            self._draw_recommendation(frame)

        cv2.putText(frame, "q quit | s snooze | u resume | t topic | r report | c recalibrate | a ask | d debug", (10, h - 10),
                    FONT, 0.45, (220, 220, 220), 1)

    def _modal(self, frame, height, bg):
        h, w = frame.shape[:2]
        x1, x2 = 30, w - 30
        # below the status bar when it fits; tall boxes may cover it rather than run off the bottom
        y1 = max(40, (h - height) // 2) if height <= h - 50 else max(4, (h - height) // 2)
        y2 = y1 + height
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (w, h), (0, 0, 0), -1)            # dim the video
        cv2.addWeighted(overlay, 0.45, frame, 0.55, 0, frame)
        cv2.rectangle(frame, (x1, y1), (x2, y2), bg, -1)
        cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 255, 255), 1)
        return x1, y1, x2, y2

    def _draw_confirm(self, frame):
        left = max(0.0, CONFIRM_S - (time.time() - self.pending_since))
        x1, y1, x2, y2 = self._modal(frame, 200, (45, 45, 45))
        cv2.putText(frame, "Nodding off?", (x1 + 20, y1 + 40), FONT, 1.0, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(frame, f"Planning your break in {left:.0f}s unless this was a false alarm.",
                    (x1 + 20, y1 + 75), FONT, 0.55, (230, 230, 230), 1, cv2.LINE_AA)
        # countdown bar
        bar_w = int((x2 - x1 - 40) * (left / CONFIRM_S))
        cv2.rectangle(frame, (x1 + 20, y1 + 92), (x2 - 20, y1 + 102), (90, 90, 90), -1)
        cv2.rectangle(frame, (x1 + 20, y1 + 92), (x1 + 20 + bar_w, y1 + 102), (60, 160, 230), -1)
        mid = (x1 + x2) // 2
        self.button(frame, (x1 + 20, y2 - 70, mid - 10, y2 - 20), "False alarm (F)", self.false_alarm,
                    bg=(60, 60, 60))
        self.button(frame, (mid + 10, y2 - 70, x2 - 20, y2 - 20), "I'm tired - go now (Enter)", self.go_now,
                    bg=(150, 90, 40))

    def _draw_recommendation(self, frame):
        rec = self.recommendation
        bg = ACTION_COLORS.get(rec.get("action"), (80, 80, 80))
        w = frame.shape[1]
        lines = textwrap.wrap(rec.get("message", ""), width=max(20, (w - 100) // 11))
        height = 70 + 26 * len(lines) + 80
        x1, y1, x2, y2 = self._modal(frame, height, bg)
        label = ACTION_LABELS.get(rec.get("action"), "TAKE A BREAK")
        if rec.get("minutes"):
            label += f" ({rec['minutes']} min)"
        cv2.putText(frame, label, (x1 + 20, y1 + 40), FONT, 0.9, (255, 255, 255), 2, cv2.LINE_AA)
        y = y1 + 72
        for line in lines:
            cv2.putText(frame, line, (x1 + 20, y), FONT, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
            y += 26
        mid = (x1 + x2) // 2
        by1, by2 = y2 - 60, y2 - 16
        if self.is_desk_nap():
            mins = (rec.get("booked") or {}).get("minutes") or rec.get("minutes")
            primary = f"Start {mins}-min nap (Enter)"
        elif self.is_rest():
            primary = "Quit & rest (Enter)"
        else:
            primary = "Done, back to work (Enter)"
        self.button(frame, (x1 + 20, by1, mid - 8, by2), primary, self.accept, bg=(40, 110, 40))
        undo = "Undo - false alarm (F)" if rec.get("booked") else "False alarm (F)"
        self.button(frame, (mid + 8, by1, x2 - 20, by2), undo, self.false_alarm, bg=(40, 40, 140))

    def _draw_topic(self, frame):
        x1, y1, x2, y2 = self._modal(frame, 210, (90, 70, 40))
        cv2.putText(frame, "What are you studying tonight?", (x1 + 20, y1 + 40), FONT, 0.9, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(frame, "Optional. Used for a 3-question quiz after breaks.", (x1 + 20, y1 + 68), FONT, 0.5,
                    (230, 230, 230), 1, cv2.LINE_AA)
        cv2.rectangle(frame, (x1 + 20, y1 + 82), (x2 - 20, y1 + 122), (30, 30, 30), -1)
        cv2.rectangle(frame, (x1 + 20, y1 + 82), (x2 - 20, y1 + 122), (200, 200, 200), 1)
        shown = self.topic_buffer or ""
        max_chars = max(10, (x2 - x1 - 60) // 13)
        shown = shown[-max_chars:]
        cursor = "|" if int(time.time() * 2) % 2 == 0 else " "
        text = shown + cursor if shown or cursor == "|" else ""
        placeholder = not self.topic_buffer
        cv2.putText(frame, text if not placeholder else ("e.g. CSE 214 heaps" + cursor), (x1 + 30, y1 + 110), FONT, 0.7,
                    (150, 150, 150) if placeholder else (255, 255, 255), 1, cv2.LINE_AA)
        mid = (x1 + x2) // 2
        self.button(frame, (x1 + 20, y2 - 60, mid - 8, y2 - 16), "Save (Enter)", self.topic_save, bg=(40, 110, 40))
        self.button(frame, (mid + 8, y2 - 60, x2 - 20, y2 - 16), "Skip (Esc)", self.topic_skip)

    def _draw_quiz(self, frame):
        w = frame.shape[1]
        wrap = max(24, (w - 120) // 12)
        q = self.quiz_active
        if q is None:   # still generating
            x1, y1, x2, y2 = self._modal(frame, 150, (90, 60, 20))
            dots = "." * (1 + int(time.time() * 2) % 3)
            cv2.putText(frame, f"Making your quiz{dots}", (x1 + 20, y1 + 50), FONT, 0.9, (255, 255, 255), 2, cv2.LINE_AA)
            self.button(frame, (x1 + 20, y2 - 60, x2 - 20, y2 - 16), "Skip quiz (Esc)", self.quiz_skip)
            return
        qs = q["questions"]
        if self.quiz_idx >= len(qs):    # results
            x1, y1, x2, y2 = self._modal(frame, 190, (40, 90, 40))
            cv2.putText(frame, f"{self.quiz_score}/{len(qs)} correct", (x1 + 20, y1 + 55), FONT, 1.3,
                        (255, 255, 255), 3, cv2.LINE_AA)
            msg = ("Sharp! Your brain's back online." if self.quiz_score == len(qs) else
                   "Not bad. Review the ones you missed." if self.quiz_score else
                   "Still foggy? Grab some water before you dive back in.")
            cv2.putText(frame, msg, (x1 + 20, y1 + 95), FONT, 0.6, (235, 235, 235), 1, cv2.LINE_AA)
            self.button(frame, (x1 + 20, y2 - 60, x2 - 20, y2 - 16), "Done (Enter)", self.quiz_next, bg=(40, 110, 40))
            return
        item = qs[self.quiz_idx]
        qlines = textwrap.wrap(item["question"], wrap)
        elines = (textwrap.wrap(item["explanation"], int(wrap * 1.2))     # smaller font fits more per line
                  if self.quiz_choice is not None and item["explanation"] else [])
        answered = self.quiz_choice is not None
        height = 78 + 26 * len(qlines) + 4 * 42 + (28 + 24 * len(elines) if answered else 0) + 64
        x1, y1, x2, y2 = self._modal(frame, height, (60, 45, 30))
        cv2.putText(frame, f"Quick check: {q['topic_label']}"[:wrap + 10], (x1 + 20, y1 + 30), FONT, 0.6,
                    (220, 220, 220), 1, cv2.LINE_AA)
        cv2.putText(frame, f"{self.quiz_idx + 1}/{len(qs)}", (x2 - 60, y1 + 30), FONT, 0.6, (220, 220, 220), 1, cv2.LINE_AA)
        y = y1 + 62
        for line in qlines:
            cv2.putText(frame, line, (x1 + 20, y), FONT, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
            y += 26
        y += 4
        for i, opt in enumerate(item["options"]):
            bg = (70, 70, 70)
            if answered:
                if i == item["answer_index"]:
                    bg = (40, 130, 40)
                elif i == self.quiz_choice:
                    bg = (40, 40, 160)
            self.button(frame, (x1 + 20, y, x2 - 20, y + 34), f"{i + 1}   {opt}", (lambda i=i: self.quiz_answer(i)), bg=bg)
            y += 42
        if answered:
            y += 22
            for line in elines:
                cv2.putText(frame, line, (x1 + 20, y), FONT, 0.55, (235, 235, 235), 1, cv2.LINE_AA)
                y += 24
        mid = (x1 + x2) // 2
        if answered:
            label = "Next (Enter)" if self.quiz_idx < len(qs) - 1 else "See score (Enter)"
            self.button(frame, (x1 + 20, y2 - 52, mid - 8, y2 - 12), label, self.quiz_next, bg=(40, 110, 40))
        self.button(frame, (mid + 8, y2 - 52, x2 - 20, y2 - 12), "Skip quiz (Esc)", self.quiz_skip)

    def _draw_morning(self, frame):
        x1, y1, x2, y2 = self._modal(frame, 230, (70, 90, 40))
        bed = report.fmt_time(self.morning["bed_start"])
        cv2.putText(frame, "Good morning!", (x1 + 20, y1 + 40), FONT, 1.0, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(frame, f"You went to bed at {bed}. When did you get up?", (x1 + 20, y1 + 72), FONT, 0.6,
                    (235, 235, 235), 1, cv2.LINE_AA)
        cv2.putText(frame, report.fmt_time(self.morning_choice), (x1 + 20, y1 + 128), FONT, 1.6,
                    (255, 255, 255), 3, cv2.LINE_AA)
        slept = (self.morning_choice - self.morning["bed_start"]).total_seconds()
        cv2.putText(frame, f"= {report.fmt_dur(slept)} in bed", (x1 + 250, y1 + 122), FONT, 0.6,
                    (235, 235, 235), 1, cv2.LINE_AA)
        q = (x2 - x1 - 40) // 4
        by1, by2 = y2 - 60, y2 - 16
        self.button(frame, (x1 + 20, by1, x1 + 20 + q - 8, by2), "-15 min (-)", lambda: self.morning_adjust(-15))
        self.button(frame, (x1 + 20 + q, by1, x1 + 20 + 2 * q - 8, by2), "+15 min (+)", lambda: self.morning_adjust(15))
        self.button(frame, (x1 + 20 + 2 * q, by1, x1 + 20 + 3 * q - 8, by2), "That's right (Enter)",
                    self.morning_confirm, bg=(40, 110, 40))
        self.button(frame, (x1 + 20 + 3 * q, by1, x2 - 20, by2), "Skip (Esc)", self.morning_skip)

    def _draw_snooze(self, frame):
        m, sec = divmod(int(self.snooze_left), 60)
        x1, y1, x2, y2 = self._modal(frame, 190, (90, 60, 20))
        cv2.putText(frame, "Snoozed - camera off", (x1 + 20, y1 + 40), FONT, 1.0, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(frame, f"{m}:{sec:02d}", (x1 + 20, y1 + 100), FONT, 1.8, (255, 255, 255), 3, cv2.LINE_AA)
        cv2.putText(frame, "Watching resumes automatically", (x1 + 200, y1 + 95), FONT, 0.6,
                    (230, 230, 230), 1, cv2.LINE_AA)
        mid = (x1 + x2) // 2
        self.button(frame, (x1 + 20, y2 - 60, mid - 8, y2 - 16), "Resume now (U)", self.unsnooze, bg=(40, 110, 40))
        self.button(frame, (mid + 8, y2 - 60, x2 - 20, y2 - 16), "+5 more min (S)", self.user_snooze, bg=(60, 60, 60))

    def _draw_nap(self, frame):
        left = max(0, self.nap_until - time.time())
        m, sec = divmod(int(left), 60)
        x1, y1, x2, y2 = self._modal(frame, 190, (110, 70, 30))
        cv2.putText(frame, "Napping", (x1 + 20, y1 + 40), FONT, 1.0, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(frame, f"{m}:{sec:02d}", (x1 + 20, y1 + 100), FONT, 1.8, (255, 255, 255), 3, cv2.LINE_AA)
        cv2.putText(frame, f"Alarm at {datetime.fromtimestamp(self.nap_until):%I:%M %p}".replace(" 0", " "),
                    (x1 + 200, y1 + 95), FONT, 0.6, (230, 230, 230), 1, cv2.LINE_AA)
        self.button(frame, (x2 - 240, y2 - 60, x2 - 20, y2 - 16), "End nap early (Enter)", self.end_nap_early,
                    bg=(60, 60, 60))

    def _draw_wake(self, frame):
        x1, y1, x2, y2 = self._modal(frame, 170, (30, 30, 200))
        cv2.putText(frame, "Nap's over - wake up!", (x1 + 20, y1 + 50), FONT, 1.1, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(frame, "Stand up, stretch, grab some water.", (x1 + 20, y1 + 85), FONT, 0.6,
                    (240, 240, 240), 1, cv2.LINE_AA)
        self.button(frame, (x1 + 20, y2 - 60, x2 - 20, y2 - 16), "I'm up (Enter)", self.im_up, bg=(40, 110, 40))

    @staticmethod
    def _panel(frame, text, bg):
        h, w = frame.shape[:2]
        lines = textwrap.wrap(text, width=max(20, w // 13))
        top = h - 60 - 26 * len(lines)
        overlay = frame.copy()
        cv2.rectangle(overlay, (10, top), (w - 10, h - 40), bg, -1)
        cv2.addWeighted(overlay, 0.85, frame, 0.15, 0, frame)
        y = top + 26
        for line in lines:
            cv2.putText(frame, line, (24, y), FONT, 0.6, (255, 255, 255), 1)
            y += 26

    # ---------------------------------------------------------------- main loop
    # ---------------------------------------------------------------- camera
    def open_camera(self):
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY  # faster camera open on Windows
        cap = cv2.VideoCapture(CAMERA_INDEX, backend)
        if not cap.isOpened():
            cap.release()
            return None
        return cap

    def show_message_frame(self, text: str, shape):
        """One black frame with a message (used while the camera is (re)opening)."""
        frame = np.zeros(shape, np.uint8)
        frame = self.scale_frame(frame)
        h, w = frame.shape[:2]
        (tw, _), _ = cv2.getTextSize(text, FONT, 0.8, 2)
        cv2.putText(frame, text, ((w - tw) // 2, h // 2), FONT, 0.8, (220, 220, 220), 2, cv2.LINE_AA)
        cv2.imshow(WINDOW, frame)
        cv2.waitKey(1)

    def scale_frame(self, frame):
        if self.display_scale != 1.0:
            s = self.display_scale
            frame = cv2.resize(frame, None, fx=s, fy=s, interpolation=cv2.INTER_LINEAR)
        return frame

    # ---------------------------------------------------------------- main loop
    def run(self):
        cap = self.open_camera()
        if cap is None:
            raise SystemExit(f"Could not open camera {CAMERA_INDEX}. Try CAMERA_INDEX=1 in .env.")
        cv2.namedWindow(WINDOW)
        cv2.setMouseCallback(WINDOW, self.on_mouse)
        print("Sleep Agent running. Calibrating for a few seconds: sit like you normally study and look at your screen.")
        placed = False
        shape = (480, 640, 3)   # last camera frame size, used for the black screen
        try:
            while not self.quit_requested:
                if self.tracking_paused:
                    # Snoozed / napping / waking: camera off, black screen with the countdown.
                    if cap is not None:
                        cap.release()
                        cap = None
                        print("[camera] off")
                    frame = np.zeros(shape, np.uint8)
                    stats = FrameStats(calibrating=False)
                    self.tick()
                    wait_ms = 30   # no camera to pace the loop, so don't spin the CPU
                else:
                    if cap is None:
                        self.show_message_frame("Turning the camera back on...", shape)
                        cap = self.open_camera()
                        if cap is None:
                            print("[camera] couldn't reopen, retrying...")
                            time.sleep(1)
                            continue
                        self.detector.reset_tracking()  # the gap isn't a long eye closure
                        print("[camera] on")
                    ok, frame = cap.read()
                    if not ok:
                        continue
                    frame = cv2.flip(frame, 1)  # mirror view
                    shape = frame.shape
                    stats, events = self.detector.process(frame)
                    for ev in events:
                        self.on_event(ev)
                    self.tick()
                    wait_ms = 1

                pos = None
                if not self.window_placed:
                    fh, fw = frame.shape[:2]
                    self.display_scale, pos = fit_window(fw, fh, DISPLAY_SCALE, _work_area())
                if self.display_scale != 1.0:
                    # Enlarge for display only (after detection), then draw the UI at that size.
                    s = self.display_scale
                    frame = cv2.resize(frame, None, fx=s, fy=s, interpolation=cv2.INTER_LINEAR)
                    stats.landmarks_px = [(x * s, y * s) for x, y in stats.landmarks_px]
                self.draw(frame, stats)
                cv2.imshow(WINDOW, frame)
                if not placed:
                    center_window(WINDOW, frame.shape[1], frame.shape[0])
                    placed = True
                if not self.window_placed:
                    if pos:
                        cv2.moveWindow(WINDOW, *pos)
                    self.window_placed = True

                key = cv2.waitKey(wait_ms) & 0xFF
                if key != 255:
                    self.on_key(key)
        finally:
            if cap is not None:
                cap.release()
            cv2.destroyAllWindows()
            self.detector.close()
            seconds = self.log.end()
            path = report.write_report(open_browser=REPORT_ON_QUIT and seconds >= 60)
            if path:
                print(f"[report] {path}")

if __name__ == "__main__":
    App().run()
