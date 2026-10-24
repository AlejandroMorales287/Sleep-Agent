# Sleep Agent

Studying late and nodding off? Sleep Agent watches for microsleeps through your webcam.
When you keep drifting off, a Claude agent checks your Google Calendar and decides whether
you should **go to bed**, **take a nap** (and books it), or do a **quick reset** because a
deadline is too close to sleep.

**Privacy:** video never leaves your laptop. Face tracking runs locally; only short text like
"3 microsleeps in 10 min" is sent to Claude.

## Setup (Windows, PowerShell)

Python 3.10–3.13.

```powershell
cd "C:\Users\theta\OneDrive\Documents\Sleep Agent"
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then put your ANTHROPIC_API_KEY in .env
```

Get an API key at https://console.anthropic.com. The face model (`face_landmarker.task`)
downloads automatically on first run.

## Run

```powershell
python main.py    # full app
python agent.py   # test the agent alone, no webcam
```

For the first ~4 seconds, sit like you normally study so it can calibrate to your face.

## What it detects

| Signal | Rule | Points |
|---|---|---|
| Microsleep | eyes shut ≥ 0.8s while facing the screen (soft chime) | 1 |
| Long microsleep | same closure still shut at 10s | upgraded to 2 |
| Nodding off | eyes shut while upright, then head drops or face leaves frame | counts as above |
| No movement | no head movement, gaze shift or blink for 60s | 2 |
| Yawn | mouth wide open ≥ 1.2s | 0.5 |

**Not counted:** blinks, reading with your head tilted down, looking down with just your
eyes, walking away from the desk. Nearly-shut eyes always count, wherever you're looking.

Points expire 10 minutes after they happen. At **2 points** the popup opens.

## What happens next

1. **"Nodding off?" popup.** Your alarm plays, the window jumps to the front with keyboard
   focus, and a 15s countdown starts. Nothing is sent to Claude yet.
   - **False alarm (F):** cancel, snooze 5 min. No Claude call, nothing booked.
   - **I'm tired, go now (Enter):** skip the countdown.
2. **The agent** reads your calendar (next 3 days) and picks sleep, nap (20 or 90 min) or a
   quick reset. For sleep/nap it books the block; the code computes the exact times and adds
   them to the message, so the calendar and the advice always match.
3. **Recommendation panel**
   - Sleep/nap: **Quit & rest (Enter)** or **Undo - false alarm (F)**, which deletes the
     booked event (Calendar API only, no Claude credits).
   - Quick reset: **Done, back to work (Enter)** or **False alarm (F)**.
4. After real advice it stays quiet for 5 minutes. False alarms don't start that cooldown.

## Keys

| Key | Action |
|---|---|
| S / button | Snooze 5 min (press again to stack, max 60) |
| U | Resume watching now |
| C | Recalibrate |
| A | Ask the agent now (demo shortcut) |
| D | Debug overlay: eye opening, gaze, head tilt |
| Q | Quit |

## Files

| File | What it does |
|---|---|
| `main.py` | Webcam loop, fatigue score, popup/buttons, snooze, window focus |
| `detector.py` | MediaPipe face tracking: eyes, gaze, head tilt, nods, stillness, yawns |
| `agent.py` | Claude tool-use loop: read calendar, book/undo blocks, give advice |
| `calendar_tool.py` | Google Calendar + offline demo calendar |
| `alerts.py` | Alarm sound, chime, Windows notification, text-to-speech |
| `demo_calendar*.json` | Offline scenarios: busy (midterm tomorrow) and free |

## Google Calendar

Without it the app uses `demo_calendar.json`. To connect yours:

1. https://console.cloud.google.com → create a project → enable **Google Calendar API**.
2. **Google Auth Platform → Audience**: add your Gmail as a **test user**.
3. **Credentials** → Create OAuth client ID → **Desktop app** → download the JSON.
4. Save it here as `credentials.json`. First run opens a browser to sign in, then saves `token.json`.

If sign-in fails, the app falls back to the demo calendar instead of crashing.
If booked times look shifted by hours, check your Google Calendar's time zone in its settings.

## Tuning

Every setting is listed with its default in `.env.example`. Press **D** to see the live
numbers while you adjust:

- Normal blinks or squints trigger it → lower `EAR_CLOSED_FRACTION`.
- Real eye closures get missed → raise `EAR_CLOSED_FRACTION`.
- Looking down at notes still counts → lower `LOOK_DOWN_THRESHOLD` or `HEAD_DOWN_FACTOR`.
- A slight head tilt is excused as reading → raise `HEAD_DOWN_FACTOR`.

## Demo settings

Put these in `.env` so things happen fast on stage:

```
LONG_MICROSLEEP_SECONDS=5
STILL_SECONDS=20
AGENT_COOLDOWN_SECONDS=30
USE_DEMO_CALENDAR=true      # optional: no Wi-Fi or Google sign-in needed
```

## Demo script

1. Start the app and press **D**: show the live tracking, then press D again.
2. Look down at a notebook: "not counted". Close your eyes briefly: soft chime.
3. Close your eyes and let your head drop: **"NODDING OFF"**, then the alarm and popup.
4. Press **F**: false alarm, nothing sent to Claude. Repeat and let the countdown run.
5. The agent reads the calendar, books a nap and explains why. Show the event in Google Calendar.
6. Press **F** on the panel: the event disappears. Switch to `demo_calendar_free.json` to show
   "go to bed" instead.
