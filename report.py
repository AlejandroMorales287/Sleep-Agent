"""Session log + sleep report.

Two files, both in reports/:
  sleep_history.json  every session, appended over time (saved as you go)
  report.html         one page, rewritten each time: latest night first, past nights below

A "night" runs noon to noon, so a session at 11 PM and one at 2 AM are the same night.
Sleep for a night = desk naps (Start nap -> alarm) + bed sleep (Quit & rest -> wake-up).
Bed wake-up starts as the calendar block's planned end ("estimated") and is replaced
when the user answers the morning question on next launch ("confirmed").
"""

from __future__ import annotations

import html
import json
import os
import threading
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(HERE, "reports")
HISTORY_PATH = os.path.join(REPORTS_DIR, "sleep_history.json")
REPORT_PATH = os.path.join(REPORTS_DIR, "report.html")
MORNING_LOOKBACK_H = 36   # only ask about bedtimes this recent
_save_lock = threading.Lock()   # the agent runs in a background thread


def now() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def night_of(dt: datetime):
    """Noon-to-noon: anything before noon belongs to the previous evening's night."""
    return (dt - timedelta(hours=12)).date()


def fmt_dur(seconds: float) -> str:
    m = int(round(seconds / 60))
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


# ---------------------------------------------------------------------- storage
def load_history() -> dict:
    try:
        with open(HISTORY_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("sessions"), list):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"sessions": []}


def save_history(data: dict):
    with _save_lock:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        tmp = HISTORY_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, HISTORY_PATH)   # atomic: a crash never leaves a half-written file


# ---------------------------------------------------------------------- live session
class SessionLog:
    def __init__(self):
        self.data = load_history()
        self.session = {
            "start": _iso(now()), "end": None,
            "events": [],      # {"t", "kind": microsleep|long_microsleep|stillness|yawn}
            "popups": [],      # {"t", "outcome": false_alarm|agent}
            "advice": [],      # {"t", "action", "minutes", "message"}
            "naps": [],        # {"start", "end", "planned_minutes"}
            "snoozes": 0,
            "undos": 0,
            "bed": None,       # {"start", "planned_end", "actual_end", "confirmed"}
            "topic": "",
            "quizzes": [],     # {"t", "topic", "score", "answered", "total", "skipped"}
        }
        self.data["sessions"].append(self.session)
        self.save()

    def save(self):
        try:
            save_history(self.data)
        except Exception as e:
            print(f"[report] couldn't save history: {e}")

    # -- events
    def fatigue(self, kind: str):
        self.session["events"].append({"t": _iso(now()), "kind": kind})
        self.save()

    def popup(self, outcome: str):
        self.session["popups"].append({"t": _iso(now()), "outcome": outcome})
        self.save()

    def advice(self, rec: dict):
        self.session["advice"].append({"t": _iso(now()), "action": rec.get("action"),
                                       "minutes": rec.get("minutes"), "message": rec.get("message", "")})
        self.save()

    def snooze(self):
        self.session["snoozes"] += 1
        self.save()

    def undo(self):
        self.session["undos"] += 1
        self.save()

    def debug(self, kind: str, detail: str = ""):
        """Troubleshooting notes (e.g. why a quiz failed); not shown in the report."""
        self.session.setdefault("debug", []).append({"t": _iso(now()), "kind": kind, "detail": detail[:2000]})
        self.save()

    def set_topic(self, topic: str):
        self.session["topic"] = topic
        self.save()

    def quiz_result(self, topic: str, score: int, answered: int, total: int, skipped: bool):
        self.session["quizzes"].append({"t": _iso(now()), "topic": topic, "score": score,
                                        "answered": answered, "total": total, "skipped": skipped})
        self.save()

    def nap_start(self, planned_minutes: float):
        self.session["naps"].append({"start": _iso(now()), "end": None, "planned_minutes": planned_minutes})
        self.save()

    def nap_end(self):
        if self.session["naps"] and self.session["naps"][-1]["end"] is None:
            self.session["naps"][-1]["end"] = _iso(now())
            self.save()

    def to_bed(self, planned_end: datetime | None):
        self.session["bed"] = {"start": _iso(now()), "planned_end": _iso(planned_end) if planned_end else None,
                               "actual_end": None, "confirmed": False}
        self.save()

    def end(self) -> float:
        """Close the session; returns its length in seconds."""
        self.nap_end()
        self.session["end"] = _iso(now())
        self.save()
        return (_dt(self.session["end"]) - _dt(self.session["start"])).total_seconds()

    # -- morning question
    def pending_morning(self) -> dict | None:
        """Most recent unconfirmed bedtime from a previous session, if recent enough."""
        t = now()
        for s in reversed(self.data["sessions"][:-1]):
            bed = s.get("bed")
            if not bed:
                continue
            if bed.get("confirmed"):
                return None
            start = _dt(bed["start"])
            if t - start > timedelta(hours=MORNING_LOOKBACK_H):
                return None
            planned = _dt(bed.get("planned_end"))
            default = planned if planned and planned <= t else t   # up early -> default to now
            default = min(max(default, start + timedelta(minutes=15)), t)   # never in the future
            return {"session": s, "bed_start": start, "planned_end": planned, "default": default}
        return None

    def confirm_wake(self, pending: dict, wake: datetime):
        bed = pending["session"]["bed"]
        lo, hi = pending["bed_start"] + timedelta(minutes=5), pending["bed_start"] + timedelta(hours=16)
        wake = min(max(wake, lo), hi)   # the user knows best; just keep it within the night
        bed["actual_end"] = _iso(wake)
        bed["confirmed"] = True
        self.save()


# ---------------------------------------------------------------------- summarising
def summarize_nights(data: dict) -> list[dict]:
    """Group sessions into nights (newest first) with sleep and study totals."""
    nights: dict = {}
    for s in data.get("sessions", []):
        start = _dt(s.get("start"))
        if not start:
            continue
        end = _dt(s.get("end")) or start
        n = nights.setdefault(night_of(start), {
            "night": night_of(start), "sessions": [], "naps": [], "bed": None,
            "events": [], "popups": [], "advice": [], "snoozes": 0, "undos": 0, "topics": [], "quizzes": []})
        n["sessions"].append((start, end))
        for nap in s.get("naps", []):
            if nap.get("end"):
                n["naps"].append((_dt(nap["start"]), _dt(nap["end"])))
        n["events"] += [(_dt(e["t"]), e["kind"]) for e in s.get("events", [])]
        n["popups"] += s.get("popups", [])
        n["advice"] += s.get("advice", [])
        n["snoozes"] += s.get("snoozes", 0)
        n["undos"] += s.get("undos", 0)
        n["quizzes"] += s.get("quizzes", [])
        if s.get("topic") and s["topic"] not in n["topics"]:
            n["topics"].append(s["topic"])
        bed = s.get("bed")
        if bed:
            b_start = _dt(bed["start"])
            b_end = _dt(bed.get("actual_end")) or _dt(bed.get("planned_end"))
            if b_end and b_end > b_start:
                n["bed"] = {"start": b_start, "end": b_end, "confirmed": bool(bed.get("confirmed"))}

    out = []
    for key in sorted(nights, reverse=True):
        n = nights[key]
        nap_s = sum((e - s).total_seconds() for s, e in n["naps"])
        bed_s = (n["bed"]["end"] - n["bed"]["start"]).total_seconds() if n["bed"] else 0
        desk_s = sum((e - s).total_seconds() for s, e in n["sessions"])
        n.update(
            nap_seconds=nap_s, bed_seconds=bed_s, sleep_seconds=nap_s + bed_s,
            desk_seconds=desk_s, study_seconds=max(0.0, desk_s - nap_s),
            estimated=bool(n["bed"]) and not n["bed"]["confirmed"],
            # a long microsleep is an upgrade of a closure already logged as "microsleep"
            microsleeps=sum(1 for _, k in n["events"] if k == "microsleep"),
            long_microsleeps=sum(1 for _, k in n["events"] if k == "long_microsleep"),
            stillness=sum(1 for _, k in n["events"] if k == "stillness"),
            yawns=sum(1 for _, k in n["events"] if k == "yawn"),
            false_alarms=sum(1 for p in n["popups"] if p.get("outcome") == "false_alarm") + n["undos"],
        )
        out.append(n)
    return out


# ---------------------------------------------------------------------- HTML
CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1f;--muted:#6b6b70;--line:#e4e2dc;
--study:#5b8def;--nap:#a77bea;--bed:#3b3f63;--micro:#e5484d;--est:#c98a1b}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1e1e22;--ink:#ececf0;--muted:#9a9aa3;
--line:#2e2e34;--study:#6f9cf5;--nap:#c9a5ff;--bed:#4a5191;--micro:#ff6b6f;--est:#e0a640}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,"Segoe UI",system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:0 0 12px;color:var(--muted);font-weight:600;
text-transform:uppercase;letter-spacing:.04em}.sub{color:var(--muted);margin:0 0 24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin:0 0 16px}
.hero{display:flex;flex-wrap:wrap;gap:24px;align-items:flex-end}
.big{font-size:44px;font-weight:700;line-height:1;letter-spacing:-.02em}
.badge{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px;
border:1px solid var(--est);color:var(--est);margin-left:8px;vertical-align:middle}
.parts{display:flex;gap:24px;flex-wrap:wrap;color:var(--muted)}.parts b{color:var(--ink);display:block;font-size:18px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px}
.stat b{display:block;font-size:24px}.stat span{color:var(--muted);font-size:13px}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--muted);margin-top:8px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
svg{display:block;width:100%;height:auto}svg text{fill:var(--muted);font-size:11px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600;font-size:13px}td.n{font-variant-numeric:tabular-nums}
ul.advice{margin:0;padding:0;list-style:none}ul.advice li{padding:8px 0;border-bottom:1px solid var(--line)}
ul.advice li:last-child{border:0}.t{color:var(--muted);font-size:13px;margin-right:8px}
.empty{color:var(--muted)}
"""

ACTION_NAMES = {"sleep": "Go to bed", "nap": "Nap", "quick_reset": "Quick reset"}


def _timeline_svg(n: dict) -> str:
    starts = [s for s, _ in n["sessions"]] + ([n["bed"]["start"]] if n["bed"] else [])
    ends = [e for _, e in n["sessions"]] + ([n["bed"]["end"]] if n["bed"] else [])
    t0, t1 = min(starts), max(ends)
    t0 = t0.replace(minute=0, second=0, microsecond=0)
    t1 = (t1 + timedelta(minutes=59)).replace(minute=0, second=0, microsecond=0)
    span = max((t1 - t0).total_seconds(), 3600)
    W, H, top, bar = 800, 70, 14, 26
    pad = 26   # room for the first/last hour labels
    x = lambda dt: pad + (dt - t0).total_seconds() / span * (W - 2 * pad)
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Timeline of the night">']
    parts.append(f'<rect x="{pad}" y="{top}" width="{W-2*pad}" height="{bar}" rx="5" fill="var(--line)"/>')
    for s, e in n["sessions"]:
        parts.append(f'<rect x="{x(s):.1f}" y="{top}" width="{max(2, x(e)-x(s)):.1f}" height="{bar}" rx="4" fill="var(--study)"/>')
    for s, e in n["naps"]:
        parts.append(f'<rect x="{x(s):.1f}" y="{top}" width="{max(2, x(e)-x(s)):.1f}" height="{bar}" rx="4" fill="var(--nap)"/>')
    if n["bed"]:
        b = n["bed"]
        dash = ' stroke="var(--est)" stroke-width="2" stroke-dasharray="5 4"' if not b["confirmed"] else ""
        parts.append(f'<rect x="{x(b["start"]):.1f}" y="{top}" width="{max(2, x(b["end"])-x(b["start"])):.1f}" '
                     f'height="{bar}" rx="4" fill="var(--bed)"{dash}/>')
    for t, k in n["events"]:
        if k == "microsleep":
            parts.append(f'<circle cx="{x(t):.1f}" cy="{top-6}" r="3" fill="var(--micro)"/>')
        elif k == "long_microsleep":   # ring around the dot = that closure ran long
            parts.append(f'<circle cx="{x(t):.1f}" cy="{top-6}" r="6" fill="none" stroke="var(--micro)" stroke-width="1.5"/>')
    hours = int(span // 3600)
    step = 1 if hours <= 8 else 2 if hours <= 16 else 3
    for i in range(0, hours + 1, step):
        t = t0 + timedelta(hours=i)
        parts.append(f'<text x="{x(t):.1f}" y="{top+bar+18}" text-anchor="middle">{t.strftime("%I %p").lstrip("0")}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _hourly_svg(n: dict) -> str:
    counts: dict = {}
    for t, k in n["events"]:
        if k == "microsleep":
            h = t.replace(minute=0, second=0, microsecond=0)
            counts[h] = counts.get(h, 0) + 1
    if not counts:
        return '<p class="empty">No microsleeps this night.</p>'
    s0, s1 = min(s for s, _ in n["sessions"]), max(e for _, e in n["sessions"])
    hrs, h = [], s0.replace(minute=0, second=0, microsecond=0)
    while h <= s1:
        hrs.append(h)
        h += timedelta(hours=1)
    peak = max(counts.values())
    W, H, base = 800, 150, 120
    bw = min(60, (W - 20) / len(hrs) - 6)
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Microsleeps per hour">']
    for i, h in enumerate(hrs):
        c = counts.get(h, 0)
        cx = 10 + (i + 0.5) * (W - 20) / len(hrs)
        bh = (base - 20) * c / peak
        if c:
            parts.append(f'<rect x="{cx-bw/2:.1f}" y="{base-bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="3" fill="var(--micro)"/>')
            parts.append(f'<text x="{cx:.1f}" y="{base-bh-5:.1f}" text-anchor="middle">{c}</text>')
        parts.append(f'<text x="{cx:.1f}" y="{base+18}" text-anchor="middle">{h.strftime("%I %p").lstrip("0")}</text>')
    parts.append(f'<line x1="10" x2="{W-10}" y1="{base}" y2="{base}" stroke="var(--line)"/></svg>')
    return "".join(parts)


def _night_label(n: dict) -> str:
    d = n["night"]
    return f"Night of {d.strftime('%a %b')} {d.day}"


def build_report(data: dict) -> str:
    nights = summarize_nights(data)
    esc = html.escape
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           f'<title>Sleep Agent report</title><style>{CSS}</style></head><body><main>']
    if not nights:
        out.append('<h1>Sleep Agent</h1><p class="sub">No sessions yet.</p></main></body></html>')
        return "".join(out)

    n = nights[0]
    first, last = min(s for s, _ in n["sessions"]), max(e for _, e in n["sessions"])
    out.append(f'<h1>{_night_label(n)}</h1>')
    out.append(f'<p class="sub">Studied {fmt_time(first)} to {fmt_time(last)} · '
               f'{len(n["sessions"])} session{"s" if len(n["sessions"]) != 1 else ""}'
               f'{" · " + esc(", ".join(n["topics"])) if n["topics"] else ""}</p>')

    # hero
    badge = '<span class="badge">estimated</span>' if n["estimated"] else ""
    nap_txt = (", ".join(f"{fmt_time(s)}–{fmt_time(e)}" for s, e in n["naps"])) or "none"
    if n["bed"]:
        b = n["bed"]
        bed_txt = f'{fmt_time(b["start"])}–{fmt_time(b["end"])}' + ("" if b["confirmed"] else " (planned)")
    else:
        bed_txt = "not logged"
    out.append('<section class="card"><h2>Sleep</h2><div class="hero">'
               f'<div><div class="big">{fmt_dur(n["sleep_seconds"])}{badge}</div></div>'
               '<div class="parts">'
               f'<div><b>{fmt_dur(n["nap_seconds"])}</b>desk nap · {esc(nap_txt)}</div>'
               f'<div><b>{fmt_dur(n["bed_seconds"])}</b>in bed · {esc(bed_txt)}</div>'
               '</div></div>')
    if n["estimated"]:
        out.append('<p class="sub" style="margin:12px 0 0">Bed time uses your calendar\'s planned wake-up. '
                   'Open Sleep Agent and it will ask when you actually got up.</p>')
    if not n["bed"] and not n["naps"]:
        out.append('<p class="sub" style="margin:12px 0 0">Sleep is counted when you start a desk nap or '
                   'choose <b>Quit &amp; rest</b>.</p>')
    out.append('</section>')

    # timeline
    out.append('<section class="card"><h2>Timeline</h2>' + _timeline_svg(n) +
               '<div class="legend"><span><i style="background:var(--study)"></i>Studying</span>'
               '<span><i style="background:var(--nap)"></i>Desk nap</span>'
               '<span><i style="background:var(--bed)"></i>In bed</span>'
               '<span><i style="background:var(--micro);border-radius:50%"></i>Microsleep (ringed = long)</span>'
               '</div></section>')

    # stats
    stats = [(fmt_dur(n["study_seconds"]), "at the desk"), (n["microsleeps"], "microsleeps"),
             (n["long_microsleeps"], "ran long"), (n["stillness"], "motionless spells"),
             (n["yawns"], "yawns"), (n["false_alarms"], "false alarms"), (n["snoozes"], "snoozes")]
    out.append('<section class="card"><h2>Fatigue</h2><div class="stats">' +
               "".join(f'<div class="stat"><b>{v}</b><span>{l}</span></div>' for v, l in stats) + '</div></section>')

    out.append('<section class="card"><h2>Microsleeps by hour</h2>' + _hourly_svg(n) + '</section>')

    # agent advice
    if n["advice"]:
        items = "".join(
            f'<li><span class="t">{fmt_time(_dt(a["t"]))}</span><b>{esc(ACTION_NAMES.get(a.get("action"), "Break"))}'
            f'{" · " + fmt_dur(a["minutes"] * 60) if a.get("minutes") else ""}</b><br>{esc(a.get("message", ""))}</li>'
            for a in n["advice"])
        out.append(f'<section class="card"><h2>What the agent said</h2><ul class="advice">{items}</ul></section>')

    # quizzes
    if n["quizzes"]:
        rows = []
        for qz in n["quizzes"]:
            if qz.get("skipped") and not qz.get("answered"):
                result = "skipped"
            else:
                result = f'{qz["score"]}/{qz["total"]}' + (" (skipped part)" if qz.get("skipped") else "")
            rows.append(f'<li><span class="t">{fmt_time(_dt(qz["t"]))}</span><b>{esc(qz.get("topic", ""))}</b> · {result}</li>')
        out.append('<section class="card"><h2>Wake-up quizzes</h2><ul class="advice">' + "".join(rows) + '</ul></section>')

    # past nights
    if len(nights) > 1:
        rows = "".join(
            f'<tr><td>{_night_label(p)[9:]}</td><td class="n">{fmt_dur(p["sleep_seconds"])}'
            f'{" (est.)" if p["estimated"] else ""}</td><td class="n">{fmt_dur(p["study_seconds"])}</td>'
            f'<td class="n">{p["microsleeps"]}</td></tr>' for p in nights[1:31])
        out.append('<section class="card"><h2>Past nights</h2><table><tr><th>Night of</th><th>Sleep</th>'
                   f'<th>At desk</th><th>Microsleeps</th></tr>{rows}</table></section>')

    out.append(f'<p class="sub">Updated {fmt_time(now())} · Video never left your laptop.</p></main></body></html>')
    return "".join(out)


def write_report(open_browser: bool = False) -> str | None:
    try:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            f.write(build_report(load_history()))
        if open_browser:
            webbrowser.open(Path(REPORT_PATH).as_uri())   # handles spaces in the folder name
        return REPORT_PATH
    except Exception as e:
        print(f"[report] couldn't write report: {e}")
        return None
