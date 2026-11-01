"""
DYNAMIC SAFETY ZONE MONITORING SYSTEM — Laptop server
-----------------------------------------------------
Run this on your LAPTOP. It:
  1. Receives single verification frames from the ESP32-CAM at /verify_frame
     (one per beam-break event) and replies with {"is_human": true/false}
     so the ESP32 knows whether to count that crossing.
  2. Receives periodic "ambient" frames at /periodic_frame for the live
     density view and heatmap (no flash used for these).
  3. Tracks entry/exit counts sent by the ESP32 AFTER it has already
     confirmed a frame was human (see /entry, /exit).
  4. Saves annotated images + a crowd heatmap to ./processed_images/,
     with automatic cleanup so the folder doesn't grow forever.
  5. Serves a live dashboard (dark theme) that updates itself via a
     JS polling loop against /status - no full-page reloads - with a
     large, hard-to-miss occupancy color badge and an alert history tab.

SETUP (one-time, on your laptop):
    pip install flask ultralytics opencv-python-headless numpy

FIRST RUN:
    ultralytics will auto-download yolov8s.pt (~22MB, pretrained on COCO)

NETWORK SETUP is unchanged - ESP32-CAM broadcasts "CrowdSystem_ESP32",
laptop connects and takes static IP 192.168.4.2, then run: python app.py
Dashboard: http://192.168.4.2:5000
"""

from flask import Flask, Response, render_template, request, jsonify, redirect, session, send_from_directory, url_for
from ultralytics import YOLO
try:
    from huggingface_hub import hf_hub_download
except ImportError:
    hf_hub_download = None
import cv2
import numpy as np
import mimetypes
import os
import glob
import hmac
import sys
import time
import json
import getpass
import secrets
from werkzeug.security import check_password_hash, generate_password_hash
import database
from datetime import datetime
from collections import deque

# ---- SPLIT SETUP ------------------------------------------------------------------
# Every path is built from this file's own folder, so the app works no matter which
# folder you launch it from.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Some Windows PCs map .css/.js to the wrong type in the registry (e.g. text/plain), which
# makes the browser silently ignore the stylesheet or script. Setting them explicitly avoids it.
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/javascript", ".js")

REQUIRED_FRONTEND_FILES = (
    os.path.join("templates", "index.html"),
    os.path.join("templates", "login.html"),
    os.path.join("static", "css", "style.css"),
    os.path.join("static", "css", "login.css"),
    os.path.join("static", "js", "dashboard.js"),
    os.path.join("static", "js", "login.js"),
)


def check_frontend_files():
    """Fail early with a clear message if the frontend files are not where Flask expects them."""
    missing = [p for p in REQUIRED_FRONTEND_FILES if not os.path.isfile(os.path.join(BASE_DIR, p))]
    if missing:
        raise SystemExit(
            "\nFrontend file(s) not found next to app.py:\n  - " + "\n  - ".join(missing) +
            "\n\nKeep this folder layout (app.py and database.py side by side):\n"
            "  app.py\n  database.py\n  templates/index.html\n"
            "  static/css/style.css\n  static/js/dashboard.js\n"
        )


check_frontend_files()

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

LOGIN_USERNAME = os.environ.get("CROWDSENSE_USER", "admin")
GUEST_ENABLED = os.environ.get("CROWDSENSE_ALLOW_GUEST") == "1"   # view-only guest, OFF by default
_CRED_FILE = os.path.join(BASE_DIR, "credentials.json")           # git-ignored, holds only a hash


def _load_password_hash():
    """The password is never in the code: it is asked once on first run and stored as a hash."""
    if os.path.exists(_CRED_FILE):
        with open(_CRED_FILE) as f:
            return json.load(f)["hash"]
    pw = os.environ.get("CROWDSENSE_PASSWORD")
    if not pw and sys.stdin and sys.stdin.isatty():
        while not pw or len(pw) < 8:
            pw = getpass.getpass(f"First run - choose a password for '{LOGIN_USERNAME}' (8+ characters): ")
    if not pw:
        pw = secrets.token_urlsafe(9)
        print(f"First run - generated password for '{LOGIN_USERNAME}': {pw}")
    h = generate_password_hash(pw)
    with open(_CRED_FILE, "w") as f:
        json.dump({"hash": h}, f)
    return h


PASSWORD_HASH = _load_password_hash()
_fails = {}   # ip -> (failed attempts, time of first failure)


@app.before_request
def require_login():
    public_endpoints = {
        "login",
        "guest_login",
        "logout",
        "static",
        "verify_frame",
        "periodic_frame",
        "entry",
        "exit_route",
    }
    if request.endpoint not in public_endpoints and not session.get("authenticated"):
        return redirect(url_for("login"))
    if session.get("username") == "Guest" and request.endpoint in {"set_capacity", "reset_counts", "clear_threat"}:
        return jsonify({"error": "Guests can view only."}), 403


# -----------------------------------------------------------------------------------

# ---- Model: "s" instead of "n" - meaningfully better accuracy, still fast on CPU ----
_local_model = os.path.join(BASE_DIR, "yolov8s.pt")
model = YOLO(_local_model if os.path.exists(_local_model) else "yolov8s.pt")
PERSON_CLASS_ID = 0  # COCO class 0 = "person"

# ---- FALSE-DETECTION SETTINGS (people) ----------------------------------------------
CONF_THRESHOLD = 0.35         # boxes below this are ignored completely (the "trust threshold")
MIN_PERSON_AREA = 0.03        # a person box must cover at least 3% of the frame ...
MAX_PERSON_AREA = 0.85        # ... and at most 85%: a hand, phone or object right against the lens is not a person
CONFIRM_STREAK_REQUIRED = 2

# ---- CAMERA ORIENTATION ------------------------------------------------------------
# Degrees CLOCKWISE to turn every frame so that people appear upright: 0, 90, 180 or 270.
# Look at one saved frame_*.jpg: if people are upside down use 180, if they lie on their side use 90 or 270.
# It is a fixed setting on purpose. The old code searched all 4 rotations and then LOCKED onto whichever
# one gave any detection, so one lucky false hit rotated every later frame the wrong way.
CAMERA_ROTATION = int(os.environ.get("CAMERA_ROTATION", "90"))
if CAMERA_ROTATION not in (0, 90, 180, 270):
    raise SystemExit("CAMERA_ROTATION must be 0, 90, 180 or 270")
AUTO_ROTATE_SEARCH = os.environ.get("CROWDSENSE_AUTO_ROTATE", "0") == "1"   # off: every extra angle adds false alarms

# ---- Threat / Weapon Detection Model (Guns, Pistols, Knives, Explosives) ----
THREAT_MODEL_PATH = os.path.join(BASE_DIR, "threat_model.pt")
# ---- FALSE-ALARM SETTINGS (weapons) --------------------------------------------------
THREAT_CONF_THRESHOLD = 0.65   # a weapon box below this confidence is ignored
THREAT_STRONG_CONF = 0.85      # at or above this a single frame raises the alert straight away
THREAT_CONFIRM_HITS = 2        # otherwise the SAME weapon must be seen in this many frames ...
THREAT_CONFIRM_WINDOW_S = 15   # ... within this many seconds
THREAT_MIN_AREA = 0.015        # ignore boxes smaller than 1.5% of the frame
THREAT_MAX_AREA = 0.60         # ignore boxes covering more than 60% of the frame (close-up objects)
THREAT_MIN_ASPECT_RATIO = 0.15
THREAT_MAX_ASPECT_RATIO = 6.0
THREAT_PERSON_PROXIMITY = 0.15  # expand each person box by 15% in both dimensions
THREAT_LOOKALIKE_OVERLAP = 0.5 # drop a "weapon" if this much of it sits on a phone / remote / laptop ...
LOOKALIKE_COCO_IDS = [63, 64, 65, 66, 67, 73]   # laptop, mouse, remote, keyboard, cell phone, book
LOOKALIKE_CONF = 0.30
TARGET_THREAT_LABELS = ['gun', 'pistol', 'knife', 'weapon', 'rifle', 'explosive', 'grenade', 'explosion',
                        'handgun', 'firearm', 'revolver', 'shotgun', 'bomb']
threat_model = None

try:
    if not os.path.exists(THREAT_MODEL_PATH) and hf_hub_download is not None:
        try:
            print("Downloading pre-trained threat detection model weights...")
            downloaded_threat_path = hf_hub_download(
                repo_id="Subh775/Threat-Detection-YOLOv8n",
                filename="weights/best.pt"
            )
            import shutil
            shutil.copy(downloaded_threat_path, THREAT_MODEL_PATH)
            print(f"Threat model saved locally to {THREAT_MODEL_PATH}")
        except Exception as dl_err:
            print(f"Hugging Face threat model download notice: {dl_err}")

    if os.path.exists(THREAT_MODEL_PATH):
        threat_model = YOLO(THREAT_MODEL_PATH)
        print("Threat detection model loaded successfully.")
        print("Threat model classes:", getattr(threat_model, "names", "unknown"))
    else:
        print("Warning: Threat model file not found; threat inspection will be skipped.")
except Exception as e:
    print(f"Warning: Failed to initialize threat detection model: {e}")
    threat_model = None

# Global state for latest threat alert
latest_threat_alert = None


def threat_label_matches(name):
    """True only if a model class name really is a weapon word. Whole words only: the old check also
    accepted any class name that was merely a piece of a target word (even an empty name)."""
    words = [w for w in "".join(ch if ch.isalpha() else " " for ch in str(name).lower()).split() if w]
    return any(w in TARGET_THREAT_LABELS for w in words)


def _overlap_fraction(box, other):
    """Share of `box` (x1,y1,x2,y2) that lies inside `other`."""
    ix1, iy1 = max(box[0], other[0]), max(box[1], other[1])
    ix2, iy2 = min(box[2], other[2]), min(box[3], other[3])
    inter = max(ix2 - ix1, 0) * max(iy2 - iy1, 0)
    area = max((box[2] - box[0]) * (box[3] - box[1]), 1e-6)
    return inter / area


def _threat_near_person(threat_box, person_boxes):
    """Require a threat candidate to overlap or be close to a detected person."""
    for person_box in person_boxes or []:
        x1, y1, x2, y2 = person_box
        width, height = x2 - x1, y2 - y1
        margin_x, margin_y = width * THREAT_PERSON_PROXIMITY, height * THREAT_PERSON_PROXIMITY
        nearby_box = (x1 - margin_x, y1 - margin_y, x2 + margin_x, y2 + margin_y)
        if (threat_box[0] <= nearby_box[2] and threat_box[2] >= nearby_box[0]
                and threat_box[1] <= nearby_box[3] and threat_box[3] >= nearby_box[1]):
            return True
    return False


def scan_threats(frame, person_boxes=None):
    """Look for weapons in ONE frame (a numpy BGR image) and return a list of
    {"label", "confidence", "box"} that survived every false-alarm filter.

    IMPORTANT: `frame` must be the RAW camera image. The old code scanned the saved copy that already had
    the person boxes and text drawn on it, and those drawings could themselves look like objects."""
    if threat_model is None or frame is None:
        return []
    try:
        if not person_boxes:
            return []
        h, w = frame.shape[:2]
        frame_area = float(h * w)
        result = threat_model.predict(frame, conf=THREAT_CONF_THRESHOLD, imgsz=640, verbose=False)[0]
        if result.boxes is None or len(result.boxes) == 0:
            return []
        xyxy = result.boxes.xyxy.cpu().numpy()
        confs = result.boxes.conf.cpu().numpy()
        classes = result.boxes.cls.cpu().numpy()

        candidates = []
        for box, conf, cls_id in zip(xyxy, confs, classes):
            name = str(result.names.get(int(cls_id), "")).strip()
            if not threat_label_matches(name) or float(conf) < THREAT_CONF_THRESHOLD:
                continue
            area_ratio = max(box[2] - box[0], 0) * max(box[3] - box[1], 0) / frame_area
            if area_ratio < THREAT_MIN_AREA or area_ratio > THREAT_MAX_AREA:
                continue
            box_width, box_height = max(box[2] - box[0], 0), max(box[3] - box[1], 0)
            if box_height == 0:
                continue
            aspect_ratio = box_width / box_height
            if not THREAT_MIN_ASPECT_RATIO <= aspect_ratio <= THREAT_MAX_ASPECT_RATIO:
                continue
            if not _threat_near_person(box, person_boxes):
                continue
            candidates.append({"label": name.capitalize(), "confidence": float(conf),
                               "box": [float(v) for v in box]})
        if not candidates:
            return []

        # Phones, remotes and laptops are the classic "gun" false alarms: if the weapon box sits on one, drop it.
        # The everyday-object check only runs when a weapon candidate exists, so it costs nothing normally.
        everyday = model.predict(frame, classes=LOOKALIKE_COCO_IDS, conf=LOOKALIKE_CONF, imgsz=640, verbose=False)[0]
        if everyday.boxes is not None and len(everyday.boxes) > 0:
            everyday_boxes = everyday.boxes.xyxy.cpu().numpy()
            candidates = [c for c in candidates
                          if not any(_overlap_fraction(c["box"], ob) >= THREAT_LOOKALIKE_OVERLAP
                                     for ob in everyday_boxes)]
        return candidates
    except Exception as err:
        print(f"Threat inspection warning: {err}")
        return []


def inspect_frame_for_threats(frame):
    """Labels of weapons found in one frame (kept so older code and tests that call it keep working)."""
    return list(dict.fromkeys(c["label"] for c in scan_threats(frame)))


THREAT_LOG_COOLDOWN_S = 30   # same weapon seen again within 30 s updates the banner but is not logged again
SCAN_PERIODIC_FRAMES = os.environ.get("CROWDSENSE_SCAN_PERIODIC", "1") == "1"   # set to 0 to save CPU
_last_threat_logged = 0.0
_threat_hits = deque()       # (time, label) of recent weapon sightings, used for the 2-frame confirmation


def handle_threats(frame, filename, detections=None, person_boxes=None):
    """Scan one RAW frame, apply the confirmation rule, then raise / log the alert.
    Returns the confirmed weapon labels (empty list = no alert). `detections` lets the caller pass the
    result of scan_threats() when it already ran it (to draw the boxes on the saved image)."""
    global latest_threat_alert, _last_threat_logged
    try:
        found = detections if detections is not None else scan_threats(frame, person_boxes)
    except Exception as err:
        print(f"Threat inspection error: {err}")
        return []

    now = time.time()
    while _threat_hits and now - _threat_hits[0][0] > THREAT_CONFIRM_WINDOW_S:
        _threat_hits.popleft()
    if not found:
        return []
    for det in found:
        _threat_hits.append((now, det["label"]))

    confirmed = []
    for det in found:
        sightings = sum(1 for _, lab in _threat_hits if lab == det["label"])
        if det["confidence"] >= THREAT_STRONG_CONF or sightings >= THREAT_CONFIRM_HITS:
            confirmed.append(det)
    if not confirmed:
        print("Possible weapon seen but not confirmed yet: " +
              ", ".join(f"{d['label']} {d['confidence']:.0%}" for d in found))
        return []

    weapons = list(dict.fromkeys(d["label"] for d in confirmed))
    weapon_list = ", ".join(weapons)
    top_conf = max(d["confidence"] for d in confirmed)
    latest_threat_alert = {
        "threat_type": weapons,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "weapons_str": weapon_list,
        "image_filename": filename,
        "confidence": round(top_conf, 2),
    }
    if now - _last_threat_logged >= THREAT_LOG_COOLDOWN_S:
        _last_threat_logged = now
        try:
            database.log_event("THREAT_ALERT", 0, people_inside_count(), filename,
                               f"Threat: {weapon_list} ({top_conf:.0%})")
        except Exception as db_err:
            print(f"Failed to log THREAT_ALERT in database: {db_err}")
        add_alert(f"CRITICAL: Weapon Detected ({weapon_list}, {top_conf:.0%} sure) - please check the image", "critical")
    return weapons


SAVE_DIR = os.path.join(BASE_DIR, "processed_images")
os.makedirs(SAVE_DIR, exist_ok=True)
CAPTURES_DIR = os.path.join(BASE_DIR, "static", "captures")
os.makedirs(CAPTURES_DIR, exist_ok=True)

# ---- Image retention: keep disk usage bounded for a multi-day event ----
MAX_IMAGES_KEPT = 500

def cleanup_old_images():
    for folder in (SAVE_DIR, CAPTURES_DIR):
        files = sorted(glob.glob(os.path.join(folder, "*.jpg")), key=os.path.getmtime)
        excess = len(files) - MAX_IMAGES_KEPT
        for f in files[:max(excess, 0)]:
            try:
                os.remove(f)
            except OSError:
                pass

# ---- Venue capacity thresholds (EDIT THESE for your actual venue) ----
MAX_CAPACITY = 5
ORANGE_THRESHOLD = 0.6   # 60% of capacity -> orange
RED_THRESHOLD = 0.9      # 90% of capacity -> red
CRITICAL_THRESHOLD = 1.0 # 100%+ -> red AND the flash alert

# The ESP32 firmware already calls /entry and /exit after /verify_frame. Counting a
# second time inside /verify_frame (via ?direction=) would double count, so it is OFF.
# Turn it on only if your firmware sends ?direction=entry|exit and does NOT call /entry,/exit.
AUTO_COUNT_FROM_VERIFY = False

# ---- In-memory state ----
entry_count = 0
exit_count = 0
latest_people_in_frame = 0
latest_image_name = None
latest_heatmap_name = None
verified_people_count = 0
# --- DATABASE INTEGRATION ---
last_verify_image_name = None
recent_human_confidences = deque(maxlen=CONFIRM_STREAK_REQUIRED)

# ---- Alert history (shown in its own dashboard tab) ----
alert_log = deque(maxlen=100)
last_zone_color = "green"

# ---- Crowd-count history, used only to draw the dashboard graph ----
# (purely additive - does not touch entry/exit/zone logic at all)
crowd_history = deque(maxlen=150)


def add_alert(message, kind="info"):
    alert_log.appendleft({
        "time": datetime.now().strftime("%H:%M:%S"),
        "type": kind,
        "message": message,
    })


# --- DATABASE INTEGRATION ---
def safe_log_event(direction, people_count, occupancy, image, trigger_type=None):
    try:
        database.log_event(direction, people_count, occupancy, image, trigger_type)
    except Exception as error:
        print(f"Database event logging failed: {error}")


def people_inside_count():
    return max(entry_count - exit_count, 0)


def zone_status(people_inside):
    ratio = people_inside / MAX_CAPACITY if MAX_CAPACITY else 0
    if ratio >= CRITICAL_THRESHOLD:
        return "red", True   # color, critical-flash-flag
    elif ratio >= RED_THRESHOLD:
        return "red", False
    elif ratio >= ORANGE_THRESHOLD:
        return "orange", False
    else:
        return "green", False


def check_zone_transition():
    """Call after entry/exit counts change. Logs an alert only on a
    transition (not on every poll), so the history doesn't get spammed."""
    global last_zone_color
    people_inside = people_inside_count()
    color, critical = zone_status(people_inside)
    if color != last_zone_color:
        if color == "green":
            add_alert(f"Back to GREEN zone ({people_inside}/{MAX_CAPACITY})", "green")
        elif color == "orange":
            add_alert(f"Entered ORANGE zone ({people_inside}/{MAX_CAPACITY})", "orange")
        elif color == "red" and not critical:
            add_alert(f"Entered RED zone ({people_inside}/{MAX_CAPACITY})", "red")
        last_zone_color = color
    if critical:
        add_alert(f"CRITICAL - venue overcrowded ({people_inside}/{MAX_CAPACITY})", "critical")


# --- DATABASE INTEGRATION ---
try:
    database.init_db()
except Exception as error:
    print(f"Database initialization failed: {error}")

try:
    # Entries/Exits cards restart, but People Inside is restored from persisted occupancy.
    entry_count = database.get_latest_occupancy()
except Exception as error:
    print(f"Database occupancy restore failed: {error}")
    entry_count = 0
exit_count = 0
last_zone_color = zone_status(people_inside_count())[0]
if entry_count > 0:
    add_alert(f"Server restarted - restored {entry_count} people inside", "info")


def _rotate_fixed(img):
    """Turn the frame by the fixed CAMERA_ROTATION so people are upright."""
    code = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}.get(CAMERA_ROTATION)
    return img if code is None else cv2.rotate(img, code)


def judge_person_boxes(result, frame_shape):
    """Decide, box by box, whether a YOLO 'person' box is trusted.
    Returns [{"box", "conf", "counted", "reason"}]. A box is counted only if it is confident enough
    AND has a believable size (not a speck, not a hand or phone filling the whole lens)."""
    entries = []
    if result.boxes is None or len(result.boxes) == 0:
        return entries
    h, w = frame_shape[:2]
    frame_area = float(h * w)
    xyxy = result.boxes.xyxy.cpu().numpy()
    confs = result.boxes.conf.cpu().numpy()
    classes = result.boxes.cls.cpu().numpy()
    for box, conf, cls_id in zip(xyxy, confs, classes):
        if int(cls_id) != PERSON_CLASS_ID:
            continue
        conf = float(conf)
        if conf < CONF_THRESHOLD:
            continue
        area = max(box[2] - box[0], 0) * max(box[3] - box[1], 0) / frame_area
        reason = None
        if area < MIN_PERSON_AREA:
            reason = "too small"
        elif area > MAX_PERSON_AREA:
            reason = "too large, close-up object"
        entries.append({"box": [float(v) for v in box], "conf": conf, "class_id": int(cls_id),
                        "counted": reason is None, "reason": reason})
    return entries


def draw_person_boxes(img, entries):
    """Green box = counted as a person. Grey box = seen but ignored, with the reason written on it,
    so the audit photo shows exactly why a crossing was or was not counted."""
    out = img.copy()
    for e in entries:
        x1, y1, x2, y2 = [int(v) for v in e["box"]]
        color = (60, 200, 60) if e["counted"] else (150, 150, 150)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        label = f"person {e['conf']:.2f}" + ("" if e["counted"] else f" - ignored: {e['reason']}")
        cv2.putText(out, label, (x1 + 4, max(y1 + 18, 18)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
    return out


def draw_threat_boxes(img, detections):
    """Red boxes for weapon candidates that passed the filters, so the saved photo shows what was flagged."""
    out = img
    for det in detections or []:
        if out is img:
            out = img.copy()
        x1, y1, x2, y2 = [int(v) for v in det["box"]]
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 0, 255), 3)
        cv2.putText(out, f"{det['label']} {det['confidence']:.0%}", (x1 + 4, max(y1 + 20, 20)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    return out


def run_detection(img, search_rotations=True):
    """Run person detection. Returns (raw_img, annotated, boxes, is_human).

    * raw_img   - the frame after the fixed CAMERA_ROTATION, with nothing drawn on it
    * annotated - the same frame with green (counted) and grey (ignored) person boxes drawn
    * boxes     - only the COUNTED person boxes
    * is_human  - True only if at least one box passed the confidence and size checks
    """
    def detect(frame):
        result = model.predict(frame, classes=[PERSON_CLASS_ID], conf=CONF_THRESHOLD, imgsz=640, verbose=False)[0]
        return judge_person_boxes(result, frame.shape)

    img = _rotate_fixed(img)
    entries = detect(img)

    # Optional (off by default): if nobody was found, try the other angles for THIS frame only.
    # Nothing is remembered, so one false hit can never turn the camera view sideways for later frames.
    if AUTO_ROTATE_SEARCH and search_rotations and not any(e["counted"] for e in entries):
        for code in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE):
            candidate = cv2.rotate(img, code)
            candidate_entries = detect(candidate)
            if any(e["counted"] for e in candidate_entries):
                img, entries = candidate, candidate_entries
                break

    boxes = [e["box"] for e in entries if e["counted"]]
    annotated = draw_person_boxes(img, entries)
    is_human = any(e["class_id"] == PERSON_CLASS_ID for e in entries)
    recent_human_confidences.append(is_human)
    return img, annotated, boxes, is_human


def build_heatmap(img, boxes):
    """Builds a density heatmap from detected person bounding-box centers
    and overlays it on the frame - shows WHERE in the gate/frame people
    are clustering, for the dashboard's crowd heatmap panel."""
    h, w = img.shape[:2]
    density = np.zeros((h, w), dtype=np.float32)

    for (x1, y1, x2, y2) in boxes:
        cx, cy = int((x1 + x2) / 2), int((y1 + y2) / 2)
        cv2.circle(density, (cx, cy), radius=max(w, h) // 8, color=1.0, thickness=-1)

    if boxes:
        density = cv2.GaussianBlur(density, (0, 0), sigmaX=max(w, h) / 12)
        density = density / (density.max() + 1e-6)
    density_u8 = (density * 255).astype(np.uint8)
    heat_color = cv2.applyColorMap(density_u8, cv2.COLORMAP_JET)

    if boxes:
        overlay = cv2.addWeighted(img, 0.55, heat_color, 0.45, 0)
    else:
        overlay = img.copy()  # no detections - show plain frame, no false-color wash
    return overlay


def decode_upload():
    img_bytes = request.get_data()
    if not img_bytes:
        return None
    np_arr = np.frombuffer(img_bytes, np.uint8)
    decode_flags = cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION
    return cv2.imdecode(np_arr, decode_flags)


@app.route("/verify_frame", methods=["POST"])
def verify_frame():
    """Called once per beam-break event. Verifies human-or-nature and 
    automatically registers entry/exit if a direction query parameter is provided."""
    global latest_image_name, latest_heatmap_name, latest_people_in_frame
    global last_verify_image_name, entry_count, exit_count, verified_people_count
    global latest_threat_alert

    verified_people_count = 0
    # Check if a direction was passed in the URL (e.g. /verify_frame?direction=entry)
    direction = request.args.get("direction", "").lower()

    img = decode_upload()
    if img is None:
        return jsonify({"error": "could not decode image", "is_human": False, "threat_detected": False, "threats": []}), 400

    img, annotated, boxes, is_human = run_detection(img)
    latest_people_in_frame = len(boxes)
    verified_people_count = 0

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"verify_{timestamp}.jpg"

    # --- WEAPON / THREAT DETECTION ---
    # Scan the RAW frame (never the annotated copy: the drawn boxes and text can look like objects),
    # then draw what was flagged onto the saved audit image.
    threat_found = scan_threats(img, boxes)
    annotated = draw_threat_boxes(annotated, threat_found)

    cv2.imwrite(os.path.join(CAPTURES_DIR, filename), annotated)
    cv2.imwrite(os.path.join(SAVE_DIR, filename), annotated)

    last_verify_image_name = filename
    latest_image_name = filename

    detected_weapons = handle_threats(
        img, filename, detections=threat_found, person_boxes=boxes
    )
    threat_detected = bool(detected_weapons)
    verified_people_count = len(boxes) if is_human and not threat_detected else 0

    heatmap = build_heatmap(img, boxes)
    heatmap_name = f"heatmap_{timestamp}.jpg"
    cv2.imwrite(os.path.join(SAVE_DIR, heatmap_name), heatmap)
    cv2.imwrite(os.path.join(CAPTURES_DIR, heatmap_name), heatmap)
    latest_heatmap_name = heatmap_name

    trigger_type = "Human" if is_human and not threat_detected else (
        "Threat" if threat_detected else "Object"
    )
    # TRIGGER is the existing SQLite direction for a verified human frame.
    verify_direction = "TRIGGER" if is_human and not threat_detected else "OBJECT"

    # Confirmed threats are logged by handle_threats as THREAT_ALERT.
    if not threat_detected:
        safe_log_event(verify_direction, len(boxes), people_inside_count(), filename, trigger_type)

    # AUTOMATIC COUNT REGISTRATION IF HUMAN AND DIRECTION IS PROVIDED
    count_updated = False
    if is_human and not threat_detected and AUTO_COUNT_FROM_VERIFY:
        if direction in ["entry", "in", "1"]:
            people_crossed = len(boxes)
            entry_count += people_crossed
            safe_log_event("ENTRY", people_crossed, people_inside_count(), filename, "Human")
            people_label = "person" if people_crossed == 1 else "people"
            add_alert(
                f"Entry recorded: {people_crossed} {people_label} "
                f"({people_inside_count()} now inside)",
                "entry",
            )
            check_zone_transition()
            verified_people_count = 0
            count_updated = True
        elif direction in ["exit", "out", "2"]:
            people_crossed = len(boxes)
            if people_crossed > 0:
                exit_count += people_crossed
                safe_log_event("EXIT", people_crossed, people_inside_count(), filename, "Human")
                people_label = "person" if people_crossed == 1 else "people"
                add_alert(
                    f"Exit recorded: {people_crossed} {people_label} "
                    f"({people_inside_count()} now inside)",
                    "exit",
                )
                check_zone_transition()
                verified_people_count = 0
                count_updated = True
    elif not is_human and not threat_detected:
        add_alert(f"Non-human trigger ({trigger_type}) - {people_inside_count()} inside", "orange")

    cleanup_old_images()
    return jsonify({
        "status": "ok",
        "is_human": bool(is_human),
        "threat_detected": bool(threat_detected),
        "threats": detected_weapons,
        "people_detected": len(boxes),
        "trigger_type": trigger_type,
        "image_filename": filename,
        "entry_count": entry_count,
        "exit_count": exit_count,
        "people_inside": people_inside_count()
    })


@app.route("/periodic_frame", methods=["POST"])
def periodic_frame():
    """Ambient frame (no flash) purely for the live density view / heatmap - not used for counting."""
    global latest_image_name, latest_heatmap_name, latest_people_in_frame

    img = decode_upload()
    if img is None:
        return jsonify({"error": "could not decode image"}), 400

    img, annotated, boxes, _ = run_detection(img, search_rotations=False)
    latest_people_in_frame = len(boxes)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"frame_{timestamp}.jpg"
    threat_found = scan_threats(img, boxes) if SCAN_PERIODIC_FRAMES else []   # raw frame, see verify_frame
    annotated = draw_threat_boxes(annotated, threat_found)
    cv2.imwrite(os.path.join(CAPTURES_DIR, filename), annotated)
    cv2.imwrite(os.path.join(SAVE_DIR, filename), annotated)
    latest_image_name = filename

    if SCAN_PERIODIC_FRAMES:   # also look for weapons in the ambient frames, not just at the gate
        handle_threats(img, filename, detections=threat_found, person_boxes=boxes)

    heatmap = build_heatmap(img, boxes)
    heatmap_name = f"heatmap_{timestamp}.jpg"
    cv2.imwrite(os.path.join(SAVE_DIR, heatmap_name), heatmap)
    cv2.imwrite(os.path.join(CAPTURES_DIR, heatmap_name), heatmap)
    latest_heatmap_name = heatmap_name

    cleanup_old_images()
    return jsonify({"status": "ok", "people_detected": len(boxes)})


@app.route("/entry", methods=["GET"])
def entry():
    global entry_count, verified_people_count
    people_crossed = verified_people_count
    verified_people_count = 0
    if people_crossed == 0:
        return jsonify({
            "status": "ignored",
            "message": "No verified people to count",
            "entry_count": entry_count,
            "people_inside": people_inside_count(),
        })

    entry_count += people_crossed
    # --- DATABASE INTEGRATION ---
    safe_log_event("ENTRY", people_crossed, people_inside_count(), last_verify_image_name, "Human")
    people_label = "person" if people_crossed == 1 else "people"
    add_alert(
        f"Entry recorded: {people_crossed} {people_label} "
        f"({people_inside_count()} now inside)",
        "entry",
    )
    check_zone_transition()
    return jsonify({
        "status": "ok",
        "entry_count": entry_count,
        "people_crossed": people_crossed,
        "people_inside": people_inside_count(),
    })


@app.route("/exit", methods=["GET"])
def exit_route():
    global exit_count, verified_people_count
    people_crossed = verified_people_count
    verified_people_count = 0
    if people_crossed == 0:
        return jsonify({
            "status": "ignored",
            "message": "No verified people to count",
            "exit_count": exit_count,
            "people_inside": people_inside_count(),
        })

    exit_count += people_crossed
    # --- DATABASE INTEGRATION ---
    safe_log_event("EXIT", people_crossed, people_inside_count(), last_verify_image_name, "Human")
    people_label = "person" if people_crossed == 1 else "people"
    add_alert(
        f"Exit recorded: {people_crossed} {people_label} "
        f"({people_inside_count()} now inside)",
        "exit",
    )
    check_zone_transition()
    return jsonify({
        "status": "ok",
        "exit_count": exit_count,
        "people_crossed": people_crossed,
        "people_inside": people_inside_count(),
    })


@app.route("/reset", methods=["POST"])
def reset_counts():
    global entry_count, exit_count, latest_people_in_frame, last_zone_color
    entry_count = 0
    exit_count = 0
    latest_people_in_frame = 0
    last_zone_color = "green"
    # --- DATABASE INTEGRATION ---
    safe_log_event("RESET", 0, 0, None, "System")
    add_alert("Counters reset to 0", "info")
    return jsonify({"status": "ok", "message": "Counters reset to 0"})


@app.route("/static/captures/<filename>")
def get_static_capture(filename):
    if os.path.exists(os.path.join(CAPTURES_DIR, filename)):
        return send_from_directory(CAPTURES_DIR, filename)
    return send_from_directory(SAVE_DIR, filename)


@app.route("/processed_images/<filename>")
def get_image(filename):
    if os.path.exists(os.path.join(CAPTURES_DIR, filename)):
        return send_from_directory(CAPTURES_DIR, filename)
    return send_from_directory(SAVE_DIR, filename)


@app.route("/status")
def status():
    """Polled by the dashboard's JS every couple seconds so the page updates
    live without a full reload."""
    people_inside = people_inside_count()
    color, critical = zone_status(people_inside)

    crowd_history.append({"time": datetime.now().strftime("%H:%M:%S"), "value": people_inside})

    return jsonify({
        "entry_count": entry_count,
        "exit_count": exit_count,
        "people_inside": people_inside,
        "max_capacity": MAX_CAPACITY,
        "zone_color": color,
        "critical": critical,
        "people_in_frame": latest_people_in_frame,
        "latest_image": latest_image_name,
        "latest_heatmap": latest_heatmap_name,
        "alerts": list(alert_log),
        "history": list(crowd_history),
        "threat_alert": latest_threat_alert,
        "threat_detected": bool(latest_threat_alert),
    })


@app.route("/clear_threat", methods=["POST"])
def clear_threat():
    global latest_threat_alert
    latest_threat_alert = None
    add_alert("Security threat alert cleared by operator", "info")
    return jsonify({"status": "ok", "message": "Threat alert cleared"})


# --- DATABASE INTEGRATION ---
@app.route("/history")
def history():
    try:
        try:
            limit = int(request.args.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 500))
        return jsonify({
            "events": database.get_all_events(limit),
            "stats": database.get_stats(),
        })
    except Exception as error:
        print(f"Database history request failed: {error}")
        return jsonify({"error": str(error)}), 500


@app.route("/hourly")
def hourly():
    try:
        try:
            hours = int(request.args.get("hours", 24))
        except (TypeError, ValueError):
            hours = 24
        hours = max(1, min(hours, 168))
        return jsonify(database.get_hourly_traffic(hours))
    except Exception as error:
        print(f"Database hourly request failed: {error}")
        return jsonify({"error": str(error)}), 500


@app.route("/export.csv")
def export_csv():
    try:
        return Response(
            database.export_events_csv(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=crowd_events.csv"},
        )
    except Exception as error:
        print(f"Database CSV export failed: {error}")
        return jsonify({"error": str(error)}), 500


@app.route("/")
def dashboard():
    # The page lives in templates/index.html; its styles are in static/css/style.css
    # and its script in static/js/dashboard.js (split out of this file unchanged).
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect(url_for("dashboard"))

    error = None
    username = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))
        ip = request.remote_addr or "?"
        tries, first = _fails.get(ip, (0, 0.0))
        if tries >= 5 and time.time() - first < 300:
            error = "Too many attempts. Wait 5 minutes and try again."
        elif not hmac.compare_digest(username, LOGIN_USERNAME) or not check_password_hash(PASSWORD_HASH, password):
            _fails[ip] = (tries + 1, first if tries and time.time() - first < 300 else time.time())
            error = "Wrong username or password."
        else:
            _fails.pop(ip, None)
            session.clear()
            session["authenticated"] = True
            session["username"] = username
            if remember:
                session.permanent = True
            return redirect(url_for("dashboard"))

    return render_template("login.html", error=error, username=username, guest_enabled=GUEST_ENABLED)


@app.route("/guest_login", methods=["GET", "POST"])
def guest_login():
    if not GUEST_ENABLED:
        return render_template("login.html", error="Guest access is turned off.", username="", guest_enabled=GUEST_ENABLED)
    session.clear()
    session["authenticated"] = True
    session["username"] = "Guest"
    return redirect(url_for("dashboard"))


@app.route("/set_capacity", methods=["GET", "POST"])
def set_capacity():
    global MAX_CAPACITY
    cap = request.args.get("capacity")
    if not cap and request.is_json:
        cap = request.json.get("capacity")
    if not cap and request.form:
        cap = request.form.get("capacity")
    if cap:
        try:
            val = int(cap)
            if val > 0:
                MAX_CAPACITY = val
                add_alert(f"Venue capacity updated to {MAX_CAPACITY}", "info")
                check_zone_transition()
                return jsonify({"status": "ok", "max_capacity": MAX_CAPACITY})
        except ValueError:
            pass
    return jsonify({"error": "Invalid capacity value"}), 400


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
