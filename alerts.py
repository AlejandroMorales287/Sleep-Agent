"""Sound, desktop notification and (optional) speech. Every piece degrades
gracefully, so missing packages never crash the app."""

from __future__ import annotations

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ALARM_SOUND = os.path.join(HERE, os.getenv("ALARM_SOUND", "alarm.wav"))
WAKE_SOUND = os.path.join(HERE, "wake_alarm.wav")   # generated from ALARM_SOUND
WAKE_REPEATS = 3

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


def _build_wake_sound() -> str | None:
    """Make wake_alarm.wav from the alarm sound: louder (soft-limited, no harsh clipping)
    and repeated WAKE_REPEATS times with short gaps. Rebuilt if alarm.wav changes."""
    if not os.path.exists(ALARM_SOUND):
        return None
    if os.path.exists(WAKE_SOUND) and os.path.getmtime(WAKE_SOUND) >= os.path.getmtime(ALARM_SOUND):
        return WAKE_SOUND
    try:
        import wave
        import numpy as np
        with wave.open(ALARM_SOUND, "rb") as w:
            params = w.getparams()
            raw = w.readframes(w.getnframes())
        if params.sampwidth != 2:
            return None  # only 16-bit PCM supported; caller falls back to the plain alarm
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        x = np.tanh(1.6 * x) / np.tanh(1.6)          # ~+5 dB louder, peaks stay below clipping
        gap = np.zeros(int(params.framerate * 0.5) * params.nchannels, dtype=np.float32)
        y = np.concatenate([x, gap] * (WAKE_REPEATS - 1) + [x])
        with wave.open(WAKE_SOUND, "wb") as w:
            w.setparams(params)
            w.writeframes((np.clip(y, -1, 1) * 32767).astype(np.int16).tobytes())
        return WAKE_SOUND
    except Exception as e:
        print(f"[alerts] couldn't build wake sound: {e}")
        return None


def wake_alarm():
    """Nap's over: louder alarm, played WAKE_REPEATS times. Async."""
    path = _build_wake_sound()
    if winsound and path:
        try:
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            return
        except Exception as e:
            print(f"[alerts] couldn't play wake sound: {e}")
    for _ in range(WAKE_REPEATS):
        alarm()
        time.sleep(3)


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
