"""Sound, desktop notification and (optional) speech. Every piece degrades
gracefully, so missing packages never crash the app."""

from __future__ import annotations

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ALARM_SOUND = os.path.join(HERE, os.getenv("ALARM_SOUND", "alarm.wav"))

try:
    import winsound  # Windows only, stdlib
except ImportError:
    winsound = None

try:
    from plyer import notification
except Exception:
    notification = None

try:
    import pyttsx3
except Exception:
    pyttsx3 = None


def beep(kind: str = "nudge"):
    def _play():
        if winsound:
            # MessageBeep plays Windows system sounds through your normal speakers
            # (winsound.Beep uses a legacy tone that many laptops are silent on).
            if kind == "nudge":
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            else:  # urgent
                for _ in range(3):
                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                    time.sleep(0.35)
        else:
            sys.stdout.write("\a")
            sys.stdout.flush()

    threading.Thread(target=_play, daemon=True).start()


def alarm():
    """Play the custom alarm file (WAV) if present, otherwise the urgent beeps.
    Async, so the video never freezes."""
    if winsound and os.path.exists(ALARM_SOUND):
        try:
            winsound.PlaySound(ALARM_SOUND, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            return
        except Exception as e:
            print(f"[alerts] couldn't play {ALARM_SOUND}: {e}")
    beep("urgent")


def stop_alarm():
    if winsound:
        try:
            winsound.PlaySound(None, 0)
        except Exception:
            pass


def notify(title: str, message: str):
    if notification:
        try:
            notification.notify(title=title, message=message[:250], app_name="Sleep Agent", timeout=10)
            return
        except Exception:
            pass
    print(f"\n*** {title}: {message}\n")


def speak(text: str):
    if not pyttsx3:
        return

    def _say():
        try:
            engine = pyttsx3.init()
            engine.say(text)
            engine.runAndWait()
        except Exception:
            pass

    threading.Thread(target=_say, daemon=True).start()
