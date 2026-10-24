"""Fatigue detection from webcam frames.

Uses MediaPipe Face Landmarker (478 landmarks) to compute:
  - EAR (eye aspect ratio): drops toward 0 when eyes close
  - MAR (mouth aspect ratio): rises when yawning
  - a head-pitch ratio, so looking down at notes isn't mistaken for closed eyes

The first few seconds are a calibration phase: sit normally, eyes open,
looking at the screen. Thresholds are then set relative to YOUR face.
"""

from __future__ import annotations

import math
import os
import time
import urllib.request
from dataclasses import dataclass, field

import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mp_tasks
from mediapipe.tasks.python import vision

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/1/face_landmarker.task"
)
MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "face_landmarker.task")

# Landmark indices (MediaPipe face mesh topology)
# Order per eye: p1 (outer corner), p2, p3 (upper lid), p4 (inner corner), p5, p6 (lower lid)
LEFT_EYE = [33, 160, 158, 133, 153, 144]
RIGHT_EYE = [362, 385, 387, 263, 373, 380]
MOUTH = {"top": 13, "bottom": 14, "left": 78, "right": 308}
FOREHEAD, NOSE_TIP, CHIN = 10, 1, 152
LEFT_IRIS, RIGHT_IRIS = 468, 473   # iris centers (Face Landmarker gives 478 points incl. irises)


def ensure_model() -> str:
    if not os.path.exists(MODEL_PATH):
        print("Downloading face landmark model (one time, ~4 MB)...")
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
    return MODEL_PATH


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def eye_aspect_ratio(pts) -> float:
    p1, p2, p3, p4, p5, p6 = pts
    horizontal = _dist(p1, p4)
    if horizontal == 0:
        return 0.0
    return (_dist(p2, p6) + _dist(p3, p5)) / (2.0 * horizontal)


def mouth_aspect_ratio(top, bottom, left, right) -> float:
    width = _dist(left, right)
    return _dist(top, bottom) / width if width else 0.0


def pitch_ratio(forehead, nose, chin) -> float:
    """Grows when the head tilts down (forehead->nose stretches, nose->chin shrinks)."""
    lower = chin[1] - nose[1]
    return (nose[1] - forehead[1]) / lower if lower > 1e-6 else 0.0


def _look_down_score(result) -> float:
    """Average of MediaPipe's eyeLookDownLeft/Right blendshapes (0 if unavailable)."""
    shapes = getattr(result, "face_blendshapes", None)
    if not shapes:
        return 0.0
    vals = [c.score for c in shapes[0] if c.category_name in ("eyeLookDownLeft", "eyeLookDownRight")]
    return sum(vals) / len(vals) if vals else 0.0


@dataclass
class Event:
    kind: str          # "microsleep", "long_microsleep" (same closure, still shut), "yawn" or "stillness"
    duration: float    # seconds
    timestamp: float   # time.time()


@dataclass
class FrameStats:
    face_found: bool = False
    ear: float = 0.0
    mar: float = 0.0
    pitch: float = 0.0
    eyes_closed: bool = False
    head_down: bool = False
    gaze_down: bool = False      # eyes looking down (reading/phone), lids lowered but open
    nodding: bool = False        # closure that began upright, now with head dropped / face lost
    still_for: float = 0.0       # seconds with no head movement, gaze shift or blink
    look_down: float = 0.0       # MediaPipe eyeLookDown score, 0..1
    closed_for: float = 0.0
    calibrating: bool = True
    landmarks_px: list = field(default_factory=list)


class FatigueDetector:
    def __init__(
        self,
        microsleep_seconds: float = 0.8,
        long_microsleep_seconds: float = 10.0,
        yawn_seconds: float = 1.2,
        calibration_seconds: float = 4.0,
        ear_closed_fraction: float = 0.50,
        ear_deep_fraction: float = 0.30,
        look_down_threshold: float = 0.45,
        commit_seconds: float = 0.4,
        still_seconds: float = 60.0,
        still_head_threshold: float = 0.03,   # head moved > 3% of face width = movement
        still_gaze_threshold: float = 0.06,   # iris moved > 6% of eye width = movement
        still_ear_fraction: float = 0.25,     # eye opening changed > 25% (a blink) = movement
        mar_yawn_threshold: float = 0.55,
        head_down_factor: float = 1.60,
    ):
        self.microsleep_seconds = microsleep_seconds
        self.long_microsleep_seconds = long_microsleep_seconds
        self.yawn_seconds = yawn_seconds
        self.calibration_seconds = calibration_seconds
        self.ear_closed_fraction = ear_closed_fraction
        self.ear_deep_fraction = ear_deep_fraction
        self.look_down_threshold = look_down_threshold
        self.commit_seconds = commit_seconds
        self.still_seconds = still_seconds
        self.still_head_threshold = still_head_threshold
        self.still_gaze_threshold = still_gaze_threshold
        self.still_ear_fraction = still_ear_fraction
        self.mar_yawn_threshold = mar_yawn_threshold
        self.head_down_factor = head_down_factor

        options = vision.FaceLandmarkerOptions(
            base_options=mp_tasks.BaseOptions(model_asset_path=ensure_model()),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            output_face_blendshapes=True,
        )
        self.landmarker = vision.FaceLandmarker.create_from_options(options)
        # Frame clock for MediaPipe: set once and never reset, since it rejects
        # timestamps that go backwards. Recalibrating only resets thresholds.
        self._t0_ms = int(time.time() * 1000)
        self._last_ts_ms = -1
        self.recalibrate()

    # ------------------------------------------------------------------ #
    def recalibrate(self):
        self._calib_start: float | None = None
        self._calib_ear: list[float] = []
        self._calib_pitch: list[float] = []
        self.ear_threshold: float | None = None
        self.open_ear: float | None = None
        self.ear_deep_threshold: float | None = None
        self.pitch_baseline: float | None = None
        self._closed_since: float | None = None
        self._yawn_since: float | None = None
        self._microsleep_reported = False
        self._long_reported = False
        self._committed = False
        self._yawn_reported = False
        self._reset_stillness()

    @property
    def calibrated(self) -> bool:
        return self.ear_threshold is not None

    # ------------------------------------------------------------------ #
    def process(self, frame_bgr) -> tuple[FrameStats, list[Event]]:
        """Feed one BGR frame (from OpenCV). Returns stats + any new events."""
        now = time.time()
        stats = FrameStats(calibrating=not self.calibrated)
        events: list[Event] = []

        rgb = np.ascontiguousarray(frame_bgr[:, :, ::-1])
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        ts = int(now * 1000) - self._t0_ms
        ts = max(ts, self._last_ts_ms + 1)  # timestamps must strictly increase
        self._last_ts_ms = ts
        result = self.landmarker.detect_for_video(image, ts)

        if not result.face_landmarks:
            self._yawn_since = None
            self._reset_stillness()
            if self._committed and self._closed_since is not None:
                # Eyes were already shut while upright, then the face dropped out of view:
                # that's someone nodding off, so keep the clock running.
                stats.nodding = True
                stats.calibrating = False
                stats.closed_for = now - self._closed_since
                self._emit_closure_events(stats.closed_for, now, events)
            else:
                # No face for any other reason (walked away): reset.
                self._reset_closure()
            return stats, events

        h, w = frame_bgr.shape[:2]
        lm = result.face_landmarks[0]
        px = [(p.x * w, p.y * h) for p in lm]
        stats.face_found = True
        stats.landmarks_px = px

        ear = (eye_aspect_ratio([px[i] for i in LEFT_EYE]) +
               eye_aspect_ratio([px[i] for i in RIGHT_EYE])) / 2.0
        mar = mouth_aspect_ratio(*(px[MOUTH[k]] for k in ("top", "bottom", "left", "right")))
        pitch = pitch_ratio(px[FOREHEAD], px[NOSE_TIP], px[CHIN])
        stats.ear, stats.mar, stats.pitch = ear, mar, pitch
        stats.look_down = _look_down_score(result)

        # ---- calibration -------------------------------------------------
        if not self.calibrated:
            if self._calib_start is None:
                self._calib_start = now
            self._calib_ear.append(ear)
            self._calib_pitch.append(pitch)
            if now - self._calib_start >= self.calibration_seconds and len(self._calib_ear) > 10:
                # Use the upper-middle of the distribution so blinks don't drag it down.
                open_ear = float(np.percentile(self._calib_ear, 70))
                self.open_ear = open_ear
                self.ear_threshold = open_ear * self.ear_closed_fraction
                self.ear_deep_threshold = open_ear * self.ear_deep_fraction
                self.pitch_baseline = float(np.median(self._calib_pitch))
            return stats, events

        stats.calibrating = False
        stats.head_down = pitch > self.pitch_baseline * self.head_down_factor
        stats.eyes_closed = ear < self.ear_threshold
        # Lids lowered because you're looking down at notes/phone: gaze says "down" and the
        # eyes aren't nearly shut. Nearly-shut eyes always count, wherever the gaze reads.
        stats.gaze_down = (stats.eyes_closed and stats.look_down >= self.look_down_threshold
                           and ear >= self.ear_deep_threshold)

        # ---- microsleep ---------------------------------------------------
        # Looking down (head or eyes) excuses lowered lids, EXCEPT when the eyes shut first
        # while upright: nodding off = eyes close, then the head drops. Once a closure has
        # lasted commit_seconds upright (longer than a blink), it's locked in until the eyes open.
        upright_closed = stats.eyes_closed and not stats.head_down and not stats.gaze_down
        if stats.eyes_closed and (upright_closed or self._committed):
            if self._closed_since is None:
                self._closed_since = now
            stats.closed_for = now - self._closed_since
            if upright_closed and stats.closed_for >= self.commit_seconds:
                self._committed = True
            stats.nodding = self._committed and not upright_closed
            self._emit_closure_events(stats.closed_for, now, events)
        else:
            self._reset_closure()

        # ---- stillness: no head movement, no gaze shift, no blink -------
        stats.still_for = self._update_stillness(px, ear, now, events)

        # ---- yawn: sustained wide mouth ----------------------------------
        if mar > self.mar_yawn_threshold:
            if self._yawn_since is None:
                self._yawn_since = now
            if now - self._yawn_since >= self.yawn_seconds and not self._yawn_reported:
                self._yawn_reported = True
                events.append(Event("yawn", now - self._yawn_since, now))
        else:
            self._yawn_since = None
            self._yawn_reported = False

        return stats, events

    def _emit_closure_events(self, closed_for: float, now: float, events: list):
        if closed_for >= self.microsleep_seconds and not self._microsleep_reported:
            self._microsleep_reported = True
            events.append(Event("microsleep", closed_for, now))
        # Still shut well past a normal microsleep: escalate while it's happening,
        # so someone who fully dozes off still triggers the alarm.
        if closed_for >= self.long_microsleep_seconds and not self._long_reported:
            self._long_reported = True
            events.append(Event("long_microsleep", closed_for, now))

    def _reset_stillness(self):
        self._still_anchor = None
        self._still_smooth = None
        self._still_since = None
        self._still_reported = False

    def _update_stillness(self, px, ear: float, now: float, events: list) -> float:
        face_w = _dist(px[33], px[263]) or 1.0          # outer eye corners: scale reference
        head = [px[i] for i in (NOSE_TIP, FOREHEAD, CHIN)]
        gaze = []
        for iris, (a, b) in ((LEFT_IRIS, (33, 133)), (RIGHT_IRIS, (362, 263))):
            eye_w = _dist(px[a], px[b]) or 1.0
            cx, cy = (px[a][0] + px[b][0]) / 2, (px[a][1] + px[b][1]) / 2
            gaze.append(((px[iris][0] - cx) / eye_w, (px[iris][1] - cy) / eye_w))

        # Smooth head + gaze a little so tracking jitter doesn't count as movement.
        # (EAR stays raw so a quick blink still registers.)
        if self._still_smooth is None:
            self._still_smooth = (head, gaze)
        else:
            a = 0.4
            sh, sg = self._still_smooth
            head = [(a * x + (1 - a) * ox, a * y + (1 - a) * oy) for (x, y), (ox, oy) in zip(head, sh)]
            gaze = [(a * x + (1 - a) * ox, a * y + (1 - a) * oy) for (x, y), (ox, oy) in zip(gaze, sg)]
            self._still_smooth = (head, gaze)

        moved = self._still_anchor is None
        if not moved:
            ah, ag, aear = self._still_anchor
            head_move = max(_dist(p, q) for p, q in zip(head, ah)) / face_w
            gaze_move = max(_dist(p, q) for p, q in zip(gaze, ag))
            blink = abs(ear - aear) > (self.open_ear or ear or 1.0) * self.still_ear_fraction
            moved = head_move > self.still_head_threshold or gaze_move > self.still_gaze_threshold or blink
        if moved:
            self._still_anchor = (head, gaze, ear)
            self._still_since = now
            self._still_reported = False
            return 0.0

        still_for = now - self._still_since
        if still_for >= self.still_seconds and not self._still_reported:
            self._still_reported = True
            events.append(Event("stillness", still_for, now))
        return still_for

    def _reset_closure(self):
        self._closed_since = None
        self._microsleep_reported = False
        self._long_reported = False
        self._committed = False

    def close(self):
        self.landmarker.close()
