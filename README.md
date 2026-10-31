# CrowdSense: Crowd Safety Management System

An ESP32-CAM and a laptop that count people entering and leaving a gate, show how full the venue is on a live dashboard, and warn staff when it gets crowded or a possible weapon is seen.

Everything runs on the ESP32's own Wi-Fi. **No internet is needed while the system is running.**

## What it does

- **Counts entries and exits.** Two IR beams in a line give the direction: outer beam then inner beam is an entry, the reverse is an exit.
- **Checks that it is a person.** On each beam break the ESP32 sends one photo to the laptop, and YOLOv8 confirms a human before the count changes.
- **Shows crowd status.** Green below 60% of capacity, orange from 60%, red from 90%, and a flashing critical alert with an alarm at 100%.
- **Warns about possible weapons.** A second YOLOv8 model looks for guns, knives, grenades and explosives in gate photos and in the periodic camera frames. See the limits below.
- **Keeps a history.** Every event is stored in SQLite, with charts for the last 24 hours and a CSV download.
- **Needs a login.** Passwords are stored as hashes, and the dashboard and all its data are behind sign-in.

## How it fits together

```
IR beams + ESP32-CAM  --Wi-Fi "CrowdSystem_ESP32"-->  Laptop (Flask + YOLOv8, 192.168.4.2:5000)
                                                          |
                                                          +--> Browser dashboard (http://127.0.0.1:5000)
```

| File | Role |
|---|---|
| `esp32_cam_crowd_system.ino` | ESP32-CAM firmware: Wi-Fi access point, beam logic, flash, photo upload |
| `app.py` | Flask server: person and weapon detection, counting, login, dashboard data |
| `database.py` | SQLite event log, statistics, hourly traffic, CSV export |
| `templates/`, `static/` | Login page and dashboard (HTML, CSS, JavaScript) |
| `API.md` | Every endpoint and what it returns |
| `test_system.py` | Automated checks |

## Run it (Windows)

1. Install Python 3.10 or newer, then in the project folder run `pip install -r requirements.txt`.
2. **First time only, with internet:** run `python app.py` once. It downloads `yolov8s.pt` (people) and `threat_model.pt` (weapons) into the folder. Neither is stored in Git.
3. It then asks you to choose a password for the user `admin` (at least 8 characters, hidden as you type). Only a hash is saved, in `credentials.json`.
4. Check that the start-up text says `Threat detection model loaded successfully.` If it says the threat model file was not found, weapon alerts are off.
5. Open `http://127.0.0.1:5000` and sign in with `admin` and your password.

Next time, double-click `start_crowdsense.bat` or run `python app.py`. To reset a forgotten password, stop the server, delete `credentials.json`, and start again.

## Connect to the ESP32

1. Power the ESP32-CAM from a proper 5 V / 2 A supply. Weak power causes random resets.
2. Join the Wi-Fi **CrowdSystem_ESP32** on the laptop (password is set in the `.ino`; change it before a real deployment).
3. Give the laptop a fixed address on that network: IP `192.168.4.2`, subnet prefix `24`, gateway `192.168.4.1`. The firmware sends all data to `192.168.4.2:5000`.
4. Start the server and open `http://127.0.0.1:5000`. This address works whichever Wi-Fi the laptop is on.
5. Allow Python through Windows Firewall for Private and Public networks when asked.

A phone can view the dashboard too: join the same Wi-Fi and open `http://192.168.4.2:5000`.

## Using the dashboard

| View | What you see |
|---|---|
| Dashboard | Entries, exits, people inside, capacity, zone colour, live occupancy graph |
| Monitoring | Latest camera frame with detected people and the crowd heatmap |
| Alerts | Zone changes, entries and exits, and weapon warnings |
| Analytics | Hourly entries and exits, the event table, CSV download |
| Settings | Capacity and thresholds |

A **weapon alert** shows as a red banner across the top of every view, sounds the alarm, and is logged. It stays until someone clicks **Acknowledge / Clear Threat Alert**.

## Settings you can change

Set these in the black window before `python app.py`, for example `set CROWDSENSE_ALLOW_GUEST=1`.

| Variable | Default | Effect |
|---|---|---|
| `CROWDSENSE_USER` | `admin` | Username |
| `CROWDSENSE_PASSWORD` | asked on first run | Initial password (only used if `credentials.json` does not exist) |
| `CROWDSENSE_ALLOW_GUEST` | off | Set to `1` to show a view-only guest sign-in |
| `CROWDSENSE_SCAN_PERIODIC` | `1` | Set to `0` to skip weapon checks on periodic frames and save CPU |
| `FLASK_SECRET_KEY` | random per start | Set it to stay signed in across restarts |

## Limits you should know about

- **Weapon detection is an aid, not a guarantee.** It uses a small YOLOv8-nano model. On its own test set it misses roughly 3 in 10 guns and gives about 1 false alarm in 6 gun alerts, and a 640x480 camera at a gate will do worse. A person must check every alert.
- The weapon banner names the type but does not mark where the weapon is.
- The ESP32 routes `/entry`, `/exit`, `/verify_frame` and `/periodic_frame` are open, because the firmware cannot sign in. Anyone on the ESP32 Wi-Fi could send fake counts, so keep that Wi-Fi password private.
- One gate and one camera are supported.

## Troubleshooting

| Problem | Fix |
|---|---|
| "This site can't be reached" | The server is not running. Start it and keep the black window open. |
| Page works on home Wi-Fi but not on the ESP32 Wi-Fi | Use `http://127.0.0.1:5000`, not the home-Wi-Fi address. |
| Counts do not change | Check the laptop IP is `192.168.4.2`, and the ESP32 serial monitor shows HTTP 200 for the verify call. |
| `Frontend file(s) not found` | Keep `templates/` and `static/` next to `app.py`. |
| No weapon alerts ever | Run once on internet so `threat_model.pt` downloads. |
