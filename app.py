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

CONF_THRESHOLD = 0.4          # <-- YOLO "trust threshold" for a human detection
CONFIRM_STREAK_REQUIRED = 2

# ---- Threat / Weapon Detection Model (Guns, Pistols, Knives, Explosives) ----
THREAT_MODEL_PATH = os.path.join(BASE_DIR, "threat_model.pt")
THREAT_CONF_THRESHOLD = 0.45
TARGET_THREAT_LABELS = ['gun', 'pistol', 'knife', 'weapon', 'rifle', 'explosive', 'grenade', 'explosion']
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
    else:
        print("Warning: Threat model file not found; threat inspection will be skipped.")
except Exception as e:
    print(f"Warning: Failed to initialize threat detection model: {e}")
    threat_model = None

# Global state for latest threat alert
latest_threat_alert = None


def inspect_frame_for_threats(image_path):
    """Runs the threat model on the incoming JPEG frame and returns a list of detected threat labels."""
    detected_threats = []
    if threat_model is None:
        return detected_threats
    try:
        results = threat_model.predict(
            image_path,
            conf=THREAT_CONF_THRESHOLD,
            imgsz=640,
            verbose=False,
        )
        if results and len(results) > 0:
            result = results[0]
            if result.boxes is not None and len(result.boxes) > 0:
                for cls_id in result.boxes.cls.cpu().numpy():
                    name = result.names.get(int(cls_id), "").lower().strip()
                    for target in TARGET_THREAT_LABELS:
                        if target in name or name in target:
                            detected_threats.append(name.capitalize())
                            break
        return list(dict.fromkeys(detected_threats))
    except Exception as err:
        print(f"Threat inspection warning: {err}")
        return []

THREAT_LOG_COOLDOWN_S = 30   # same weapon seen again within 30 s updates the banner but is not logged again
SCAN_PERIODIC_FRAMES = os.environ.get("CROWDSENSE_SCAN_PERIODIC", "1") == "1"   # set to 0 to save CPU
_last_threat_logged = 0.0


def handle_threats(image_path, filename):
    """Scan one saved frame, raise the security alert, log it (rate-limited). Returns the labels found."""
    global latest_threat_alert, _last_threat_logged
    try:
        weapons = inspect_frame_for_threats(image_path)
    except Exception as err:
        print(f"Threat inspection error: {err}")
        return []
    if not weapons:
        return []
    weapon_list = ", ".join(weapons)
    latest_threat_alert = {
        "threat_type": weapons,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "weapons_str": weapon_list,
        "image_filename": filename,
    }
    if time.time() - _last_threat_logged >= THREAT_LOG_COOLDOWN_S:
        _last_threat_logged = time.time()
        try:
            database.log_event("THREAT_ALERT", 0, people_inside_count(), filename, f"Threat: {weapon_list}")
        except Exception as db_err:
            print(f"Failed to log THREAT_ALERT in database: {db_err}")
        add_alert(f"CRITICAL: Weapon Detected ({weapon_list})", "critical")
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
selected_orientation_index = 0

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


def run_detection(img, search_rotations=True):
    """Run person detection and return (img, annotated, boxes, is_human).

    SPEED FIX: the previous version always ran YOLO on 4 rotated copies of every
    frame (about 4x slower). The ESP32 waits for the /verify_frame reply with a
    time limit, so a slow reply made it give up and never call /entry or /exit.
    Now only the last orientation that worked is tried first (one pass, same
    speed as the original). The other 3 rotations are tried only if that pass
    finds nobody, and only when search_rotations is True.
    """
    global selected_orientation_index

    rotations = [None, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE]

    def orient(index):
        return img if rotations[index] is None else cv2.rotate(img, rotations[index])

    def predict(frames):
        return list(model.predict(
            frames,
            classes=[PERSON_CLASS_ID],
            conf=CONF_THRESHOLD,
            imgsz=640,
            verbose=False,
        ))

    def detection_quality(result):
        if result.boxes is None:
            return 0, 0.0
        boxes = result.boxes
        count = len(boxes)
        confidence_sum = float(boxes.conf.sum().item()) if boxes.conf is not None else 0.0
        return count, confidence_sum

    # Pass 1: the orientation that worked last time (starts as the normal, upright one).
    best_index = selected_orientation_index
    best_img = orient(best_index)
    best_result = predict([best_img])[0]

    # Pass 2 (only if nobody was found): try the remaining rotations in one batch.
    if search_rotations and detection_quality(best_result)[0] == 0:
        other_indexes = [i for i in range(len(rotations)) if i != best_index]
        other_frames = [orient(i) for i in other_indexes]
        other_results = predict(other_frames)
        if len(other_results) != len(other_frames):
            raise RuntimeError(
                f"YOLO returned {len(other_results)} results for {len(other_frames)} image orientations"
            )
        winner = max(
            range(len(other_results)),
            key=lambda k: (*detection_quality(other_results[k]), -k),
        )
        if detection_quality(other_results[winner])[0]:
            best_index = other_indexes[winner]
            best_img = other_frames[winner]
            best_result = other_results[winner]
            selected_orientation_index = best_index

    img = best_img
    boxes = (
        best_result.boxes.xyxy.cpu().numpy().tolist()
        if best_result.boxes is not None
        else []
    )
    annotated = best_result.plot()
    detected_this_frame = bool(boxes)

    recent_human_confidences.append(detected_this_frame)
    is_human_confirmed = detected_this_frame   # single-frame result, used for /verify_frame
    return img, annotated, boxes, is_human_confirmed


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
    verified_people_count = len(boxes) if is_human else 0

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"verify_{timestamp}.jpg"
    
    cv2.imwrite(os.path.join(CAPTURES_DIR, filename), annotated)
    cv2.imwrite(os.path.join(SAVE_DIR, filename), annotated)
    
    last_verify_image_name = filename
    latest_image_name = filename

    # --- WEAPON / THREAT DETECTION ---
    detected_weapons = handle_threats(os.path.join(CAPTURES_DIR, filename), filename)
    threat_detected = bool(detected_weapons)

    heatmap = build_heatmap(img, boxes)
    heatmap_name = f"heatmap_{timestamp}.jpg"
    cv2.imwrite(os.path.join(SAVE_DIR, heatmap_name), heatmap)
    cv2.imwrite(os.path.join(CAPTURES_DIR, heatmap_name), heatmap)
    latest_heatmap_name = heatmap_name

    trigger_type = "Human" if is_human else "Object/False Trigger"
    verify_direction = "TRIGGER" if is_human else "OBJECT"

    # Log snapshot into SQLite audit trail
    safe_log_event(verify_direction, len(boxes), people_inside_count(), filename, trigger_type)

    # AUTOMATIC COUNT REGISTRATION IF HUMAN AND DIRECTION IS PROVIDED
    count_updated = False
    if is_human and AUTO_COUNT_FROM_VERIFY:
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
    elif not is_human:
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
    cv2.imwrite(os.path.join(CAPTURES_DIR, filename), annotated)
    cv2.imwrite(os.path.join(SAVE_DIR, filename), annotated)
    latest_image_name = filename

    if SCAN_PERIODIC_FRAMES:   # also look for weapons in the ambient frames, not just at the gate
        handle_threats(os.path.join(CAPTURES_DIR, filename), filename)

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
