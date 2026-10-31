# API Contract: Crowd Safety Management

This file is the agreement between the **backend** (`app.py`, `database.py`) and the **frontend** (`templates/`, `static/`).

**Rules**
1. The backend will not rename or remove any endpoint or JSON field listed here.
2. The frontend may only read the fields listed here.
3. If the UI needs new data, the backend adds a new field or endpoint (never changes an old one) and this file is updated in the same pull request.
4. Frontend work touches `templates/` and `static/`. Backend work touches `app.py` and `database.py`.

Base URL on the event laptop: `http://192.168.4.2:5000`. For local use: `http://127.0.0.1:5000`.

---

## Folder layout (required; `app.py` exits if frontend files are missing)

```
app.py
database.py
requirements.txt
templates/index.html, templates/login.html
static/css/style.css, static/css/login.css
static/js/dashboard.js, static/js/login.js
static/captures/            (created automatically, not in git)
processed_images/           (created automatically, not in git)
```

Not in git, created on the laptop: `yolov8s.pt`, `threat_model.pt`, `crowd_system.db`, `credentials.json`.

---

## Authentication

Every endpoint below needs a signed-in session cookie, **except** the four device endpoints, the login page and `/static/...` files. A request without a session is redirected (HTTP 302) to `/login`.

| Endpoint | Behaviour |
|---|---|
| `GET /login` | Sign-in page. Redirects to `/` if already signed in. |
| `POST /login` | Form fields `username`, `password`, optional `remember`. Success redirects to `/`. Failure shows the page again with `Wrong username or password.` After 5 failures from one address, sign-in is blocked for 5 minutes. |
| `GET\|POST /guest_login` | View-only guest session. **Only works when `CROWDSENSE_ALLOW_GUEST=1`**; otherwise shows the sign-in page with an error. |
| `GET\|POST /logout` | Clears the session and redirects to `/login`. |

Guests get HTTP 403 `{ "error": "Guests can view only." }` from `/set_capacity`, `/reset` and `/clear_threat`.

The username comes from `CROWDSENSE_USER` (default `admin`). The password is chosen on first run and stored as a hash in `credentials.json`.

---

## Dashboard endpoints (browser, login required)

### `GET /`
The dashboard page (`templates/index.html`).

### `GET /status` (polled every couple of seconds)
```json
{
  "entry_count": 12,
  "exit_count": 5,
  "people_inside": 7,
  "max_capacity": 10,
  "zone_color": "orange",
  "critical": false,
  "people_in_frame": 3,
  "latest_image": "verify_20261031_101500_123456.jpg",
  "latest_heatmap": "heatmap_20261031_101500_123456.jpg",
  "alerts": [
    { "time": "10:15:00", "type": "orange", "message": "Entered ORANGE zone (7/10)" }
  ],
  "history": [ { "time": "10:15:02", "value": 7 } ],
  "threat_detected": true,
  "threat_alert": {
    "threat_type": ["Knife"],
    "timestamp": "2026-10-31 10:15:00",
    "weapons_str": "Knife",
    "image_filename": "frame_....jpg"
  }
}
```
| Field | Meaning |
|---|---|
| `people_inside` | `entry_count - exit_count`, never below 0 |
| `zone_color` | `"green"`, `"orange"` or `"red"` |
| `critical` | `true` at 100% of capacity or more (show the flashing alert) |
| `latest_image`, `latest_heatmap` | file names, may be `null`; load from `/static/captures/<name>` |
| `alerts` | newest first, max 100; `type` is `info, green, orange, red, critical, entry, exit` |
| `history` | up to 150 points, oldest first |
| `threat_detected` | `true` while a weapon alert is active |
| `threat_alert` | `null` when none; otherwise the labels, time and photo of the latest detection |

**Zone rules (backend logic, do not duplicate in the frontend):** under 60% green, 60% to 89% orange, 90% and above red, 100% and above red plus `critical`.

### `GET /history?limit=50`
`limit` is 1 to 500, default 50. Newest event first.
```json
{
  "events": [
    { "id": 41, "timestamp": "2026-10-31 10:15:00", "direction": "ENTRY",
      "people_count": 1, "current_occupancy": 7,
      "image_filename": "verify_....jpg", "trigger_type": "Human" }
  ],
  "stats": { "total_entries": 12, "total_exits": 5, "peak_occupancy": 9, "total_events": 40 }
}
```
`direction` is one of `ENTRY, EXIT, RESET, TRIGGER, OBJECT, THREAT_ALERT`. On failure: HTTP 500 `{ "error": "..." }`.

### `GET /hourly?hours=24`
`hours` is 1 to 168, default 24: `[ { "hour": "2026-10-31 10:00", "entries": 12, "exits": 7 } ]`

### `GET /export.csv`
Downloads all events as `crowd_events.csv`. Columns: `id, timestamp, direction, people_count, current_occupancy, image_filename, trigger_type`.

### `POST /reset`
Resets entry and exit counters to 0. Returns `{ "status": "ok", "message": "Counters reset to 0" }`. POST only; guests are refused.

### `GET|POST /set_capacity`
Accepts `?capacity=N`, JSON `{ "capacity": N }` or a form field. Returns `{ "status": "ok", "max_capacity": N }`. Invalid or non-positive values return HTTP 400 `{ "error": "Invalid capacity value" }`. Guests are refused.

### `POST /clear_threat`
Clears the active weapon alert. Returns `{ "status": "ok", "message": "Threat alert cleared" }`. POST only; guests are refused.

### Images
`GET /static/captures/<filename>` and `GET /processed_images/<filename>` serve the same saved frames (newest 500 are kept). Login required.

---

## Device endpoints (ESP32-CAM only, open without login)

The firmware cannot sign in, so these four stay public. The frontend must not call them.

### `POST /verify_frame`
Body: raw JPEG bytes, sent once per beam-break event.
```json
{ "status": "ok", "is_human": true, "threat_detected": false, "threats": [],
  "people_detected": 3, "trigger_type": "Human", "image_filename": "verify_....jpg",
  "entry_count": 12, "exit_count": 5, "people_inside": 7 }
```
Bad image: HTTP 400 `{ "error": "...", "is_human": false, "threat_detected": false, "threats": [] }`.
The frame is also checked for weapons. A detection sets `threat_alert` in `/status` and writes a `THREAT_ALERT` event (logged at most once per 30 seconds).

### `POST /periodic_frame`
Body: raw JPEG bytes. Ambient frame for the live view and heatmap, not counted. Returns `{ "status": "ok", "people_detected": 3 }`. Also checked for weapons unless `CROWDSENSE_SCAN_PERIODIC=0`.

### `GET /entry`
Adds the verified people from the last `/verify_frame`. Returns `{ "status": "ok", "entry_count": 13, "people_crossed": 1, "people_inside": 8 }`. If no verified person is waiting: `{ "status": "ignored", ... }`.

### `GET /exit`
Same for exits: `{ "status": "ok", "exit_count": 6, "people_crossed": 1, "people_inside": 7 }`, or `{ "status": "ignored", ... }`.

---

## Testing

The device endpoints work without login:
```bash
curl http://localhost:5000/entry
curl http://localhost:5000/exit
curl --data-binary @test.jpg http://localhost:5000/verify_frame
```
Everything else needs a session. With curl:
```bash
curl -c jar.txt -d "username=admin&password=YOUR_PASSWORD" http://localhost:5000/login
curl -b jar.txt http://localhost:5000/status
curl -b jar.txt -X POST http://localhost:5000/reset
```

## Configuration (environment variables)

`CROWDSENSE_USER`, `CROWDSENSE_PASSWORD` (first run only), `CROWDSENSE_ALLOW_GUEST` (`1` to enable), `CROWDSENSE_SCAN_PERIODIC` (`0` to disable), `FLASK_SECRET_KEY` (keeps sessions across restarts).
