# API Contract: Crowd Safety Management

This file is the agreement between **backend** (`app.py`, `database.py`) and **frontend** (`templates/`, `static/`).

**Rules**
1. The backend will NOT rename or remove any endpoint or JSON field listed here.
2. The frontend may ONLY read the fields listed here.
3. If the UI needs new data, the backend ADDS a new field or endpoint (never changes an old one) and this file is updated in the same pull request.
4. Frontend work only touches `templates/` and `static/`. Backend work only touches `app.py` and `database.py`.

Base URL (on the event laptop): `http://192.168.4.2:5000`. For local testing: `http://localhost:5000`.

---

## Folder layout (required, `app.py` exits if these are missing)

```
app.py
database.py
requirements.txt
templates/index.html
static/css/style.css
static/js/dashboard.js
static/captures/        (created automatically, NOT in git)
```

---

## Dashboard endpoints (used by the browser)

### `GET /`
Returns the dashboard page (`templates/index.html`).

### `GET /status`  (polled every couple of seconds)
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
  "history": [
    { "time": "10:15:02", "value": 7 }
  ]
}
```
| Field | Meaning |
|---|---|
| `people_inside` | `entry_count - exit_count`, never below 0 |
| `zone_color` | `"green"`, `"orange"` or `"red"` |
| `critical` | `true` when occupancy is 100% or more of capacity (show the flashing alert) |
| `latest_image`, `latest_heatmap` | file names, may be `null`. Load from `/static/captures/<name>` |
| `alerts` | newest first, max 100. `type` is one of `info, green, orange, red, critical, entry, exit` |
| `history` | up to 150 points for the crowd graph, oldest first |

**Zone rules (backend logic, do not duplicate in the frontend):**
under 60% green, 60% to 89% orange, 90% and above red, 100% and above red + `critical`.

### `GET /history?limit=50`
`limit` is 1 to 500, default 50.
```json
{
  "events": [
    {
      "id": 41,
      "timestamp": "2026-10-31 10:15:00",
      "direction": "ENTRY",
      "people_count": 1,
      "current_occupancy": 7,
      "image_filename": "verify_....jpg",
      "trigger_type": "Human"
    }
  ],
  "stats": {
    "total_entries": 12,
    "total_exits": 5,
    "peak_occupancy": 9,
    "total_events": 40
  }
}
```
`direction` is one of `ENTRY, EXIT, RESET, TRIGGER, OBJECT`. Newest event first.
On failure: HTTP 500 with `{ "error": "..." }`.

### `GET /hourly?hours=24`
`hours` is 1 to 168, default 24. Ready for a bar chart.
```json
[ { "hour": "2026-10-31 10:00", "entries": 12, "exits": 7 } ]
```

### `GET /export.csv`
Downloads all events as `crowd_events.csv`.
Columns: `id, timestamp, direction, people_count, current_occupancy, image_filename, trigger_type`.

### `GET|POST /reset`
Resets entry/exit counters to 0. Returns `{ "status": "ok", "message": "Counters reset to 0" }`.

### Images
- `GET /static/captures/<filename>`
- `GET /processed_images/<filename>`

Both serve the same saved frames. Keep only the newest 500 images.

---

## Device endpoints (used by the ESP32-CAM, frontend must not call these)

### `POST /verify_frame`
Body: raw JPEG bytes. Sent once per beam-break event.
```json
{ "status": "ok", "is_human": true, "people_detected": 3,
  "trigger_type": "Human", "image_filename": "verify_....jpg" }
```
Bad image: HTTP 400 `{ "error": "...", "is_human": false }`.

### `POST /periodic_frame`
Body: raw JPEG bytes. Ambient frame for the live view and heatmap only (not counted).
Returns `{ "status": "ok", "people_detected": 3 }`.

### `GET /entry`
Adds an entry. Returns `{ "status": "ok", "entry_count": 13 }`.

### `GET /exit`
Adds an exit. Returns `{ "status": "ok", "exit_count": 6 }`.
If nobody is inside: `{ "status": "ignored", "message": "...", "exit_count": 5, "people_inside": 0 }`.

---

## Planned additions (backend, behind a flag, OFF by default)

These are additive and will not change anything above while the flag is off.

- `GET /entry?count=N` and `GET /exit?count=N`: optional group size. Without `count`, the server uses the person count from the last verified frame (valid for a few seconds, used once).
- `/status` gets one new field: `"last_group_size": 3` (frontend may show it, and may ignore it).

---

### `GET|POST /set_capacity`
Updates the venue maximum capacity. Accepts `?capacity=N` query param or JSON `{ "capacity": N }`. Returns `{ "status": "ok", "max_capacity": N }`.

### `POST /clear_threat`
Acknowledges and clears any active weapon/threat alert. Returns `{ "status": "ok", "threat_cleared": true }`.

### Authentication endpoints
- `GET /login`: Displays the CrowdSense sign-in page.
- `POST /login`: Validates credentials (`username`, `password`, `remember`). Redirects to `/`.
- `GET|POST /guest_login`: Signs in as guest and redirects to `/`.
- `POST|GET /logout`: Clears session and redirects to `/login`.

---

## Testing without the ESP32

```bash
curl http://localhost:5000/entry
curl http://localhost:5000/exit
curl http://localhost:5000/reset
curl --data-binary @test.jpg http://localhost:5000/verify_frame
curl http://localhost:5000/status
```
