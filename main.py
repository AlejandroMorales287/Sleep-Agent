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

import cv2
from dotenv import load_dotenv

load_dotenv()

import alerts
from agent import SleepAgent
from detector import LEFT_EYE, RIGHT_EYE, FatigueDetector

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
    def busy(self) -> bool:
        """A popup, agent call or recommendation is already on screen."""
        return self.pending_since is not None or self.agent_busy or self.recommendation is not None

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
            print(f"[detector] long microsleep ({ev.duration:.1f}s) -> {LONG_MICROSLEEP_POINTS:g} pts")
            if self.should_trigger():
                self.open_confirm()
            return

        ev.points = {"microsleep": MICROSLEEP_POINTS, "yawn": YAWN_POINTS,
                     "stillness": STILL_POINTS}.get(ev.kind, 0.0)
        self.events.append(ev)
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
        if self.pending_since is not None and time.time() - self.pending_since >= CONFIRM_S:
            self.go_now()
        elif self.should_trigger():
            self.open_confirm()

    def go_now(self):
        if self.pending_since is None:
            return
        self.pending_since = None
        alerts.stop_alarm()
        self.ask_agent()

    def false_alarm(self):
        alerts.stop_alarm()
        self.release_front()
        if self.pending_since is not None:
            self.snooze()
            self.show_banner(f"False alarm - nothing booked. Snoozed {SNOOZE_MIN:g} min.", 4)
            print("[confirm] false alarm, cancelled before calling Claude")
            return
        if self.recommendation is not None:
            booked = self.recommendation.get("booked")
            self.recommendation = None
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

    def accept(self):
        """Primary button: rest -> quit the app; quick reset -> back to work."""
        if self.is_rest():
            self.quit_and_rest()
        else:
            self.dismiss()

    def quit_and_rest(self):
        print("[app] quitting - get some rest!")
        self.quit_requested = True

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
        if key == ord("q"):
            self.quit_requested = True
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
            self.snooze()
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
        if self.snooze_left:
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

        if not self.busy:
            label = "Snooze +5 min" if not self.snooze_left else "+5 more min"
            self.button(frame, (w - 160, 44, w - 10, 76), label, self.snooze, bg=(90, 60, 20), border=(255, 200, 0))

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

        if self.pending_since is not None:
            self._draw_confirm(frame)
        elif self.recommendation is not None:
            self._draw_recommendation(frame)

        cv2.putText(frame, "q quit | s snooze | u resume | c recalibrate | a ask agent | d debug", (10, h - 10),
                    FONT, 0.45, (220, 220, 220), 1)

    def _modal(self, frame, height, bg):
        h, w = frame.shape[:2]
        x1, x2 = 30, w - 30
        y1 = max(40, (h - height) // 2)
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
        primary = "Quit & rest (Enter)" if self.is_rest() else "Done, back to work (Enter)"
        self.button(frame, (x1 + 20, by1, mid - 8, by2), primary, self.accept, bg=(40, 110, 40))
        undo = "Undo - false alarm (F)" if rec.get("booked") else "False alarm (F)"
        self.button(frame, (mid + 8, by1, x2 - 20, by2), undo, self.false_alarm, bg=(40, 40, 140))

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
    def run(self):
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY  # faster camera open on Windows
        cap = cv2.VideoCapture(CAMERA_INDEX, backend)
        if not cap.isOpened():
            raise SystemExit(f"Could not open camera {CAMERA_INDEX}. Try CAMERA_INDEX=1 in .env.")
        cv2.namedWindow(WINDOW)
        cv2.setMouseCallback(WINDOW, self.on_mouse)
        print("Sleep Agent running. Calibrating for a few seconds: sit like you normally study and look at your screen.")
        placed = False
        try:
            while not self.quit_requested:
                ok, frame = cap.read()
                if not ok:
                    continue
                frame = cv2.flip(frame, 1)  # mirror view
                stats, events = self.detector.process(frame)
                for ev in events:
                    self.on_event(ev)
                self.tick()
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

                key = cv2.waitKey(1) & 0xFF
                if key != 255:
                    self.on_key(key)
        finally:
            cap.release()
            cv2.destroyAllWindows()
            self.detector.close()


if __name__ == "__main__":
    App().run()
