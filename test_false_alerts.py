"""
test_false_alerts.py - checks the false-detection and false-alarm protections in app.py.

Run:   python test_false_alerts.py

Uses fake YOLO models (no real weights needed for the checks) and temporary folders, so it does not touch your
real database, captured images or credentials.
"""
import os
import sys
import tempfile
import time

import cv2
import numpy as np

# ---- keep the test away from the real database / credentials --------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import database                                    # noqa: E402
database.DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.setdefault("CROWDSENSE_PASSWORD", "test-password-123")
_cred = os.path.join(HERE, "credentials.json")
_cred_existed = os.path.exists(_cred)
import app                                         # noqa: E402

app.CAPTURES_DIR = tempfile.mkdtemp()
app.SAVE_DIR = tempfile.mkdtemp()

failures = 0


def check(name, condition, detail=""):
    global failures
    if not condition:
        failures += 1
    print(("PASS  " if condition else "FAIL  ") + name + ("" if condition else f"   -> {detail}"))


# ---- fake models -------------------------------------------------------------------------------------
class _Arr:
    def __init__(self, a):
        self.a = np.array(a, dtype=float)

    def cpu(self):
        return self

    def numpy(self):
        return self.a


class _Boxes:
    def __init__(self, rows):          # rows: (x1, y1, x2, y2, conf, cls)
        rows = rows or []
        self.xyxy = _Arr([r[:4] for r in rows]) if rows else _Arr(np.zeros((0, 4)))
        self.conf = _Arr([r[4] for r in rows])
        self.cls = _Arr([r[5] for r in rows])
        self._n = len(rows)

    def __len__(self):
        return self._n


class _Result:
    def __init__(self, rows, names):
        self.boxes = _Boxes(rows) if rows else None
        self.names = names


class FakePersonModel:
    names = {0: "person"}

    def __init__(self):
        self.person = []        # (x1,y1,x2,y2,conf)
        self.everyday = []      # phones / remotes etc.
        self.person_fn = None   # optional: decide boxes from the frame

    def predict(self, frame, classes=None, **kw):
        if classes == [0]:
            rows = self.person_fn(frame) if self.person_fn else self.person
            return [_Result([(*r[:5], 0) for r in rows], self.names)]
        return [_Result([(*r[:5], 67) for r in self.everyday], {67: "cell phone"})]


class FakeThreatModel:
    names = {0: "Gun", 1: "Knife", 2: "Phone"}

    def __init__(self):
        self.det = []           # (x1,y1,x2,y2,conf,cls)
        self.seen = []

    def predict(self, frame, **kw):
        self.seen.append(frame.copy())
        return [_Result(self.det, self.names)]


fake_person, fake_threat = FakePersonModel(), FakeThreatModel()
app.model, app.threat_model = fake_person, fake_threat

W, H = 640, 480
FRAME = np.full((H, W, 3), (30, 60, 90), np.uint8)
JPG = cv2.imencode(".jpg", FRAME)[1].tobytes()
client = app.app.test_client()


def reset():
    app.entry_count = app.exit_count = app.verified_people_count = 0
    app._threat_hits.clear()
    app.latest_threat_alert = None
    app._last_threat_logged = 0.0
    app.CAMERA_ROTATION = 0
    app.AUTO_ROTATE_SEARCH = False
    fake_person.person, fake_person.everyday, fake_person.person_fn = [], [], None
    fake_threat.det, fake_threat.seen = [], []


def verify(data=JPG):
    return client.post("/verify_frame", data=data, content_type="application/octet-stream").get_json()


def latest_saved_image():
    path = os.path.join(app.CAPTURES_DIR, sorted(f for f in os.listdir(app.CAPTURES_DIR) if f.startswith("verify_"))[-1])
    return cv2.imread(path)


print("\n== PEOPLE: false detections ==")
reset(); fake_person.person = [(100, 50, 400, 450, 0.72)]
r = verify(); check("normal person at 0.72 is counted", r["is_human"] and r["people_detected"] == 1, r)
reset(); fake_person.person = [(100, 50, 400, 450, 0.50)]
r = verify(); check("0.50 confidence is NOT counted (this was the 'person 0.50' phone close-up)", not r["is_human"], r)
reset(); fake_person.person = [(0, 0, 630, 470, 0.80)]
r = verify(); check("a box covering 96% of the frame (hand/phone on the lens) is NOT counted", not r["is_human"], r)
reset(); fake_person.person = [(10, 10, 30, 30, 0.90)]
r = verify(); check("a tiny speck is NOT counted", not r["is_human"], r)
reset(); fake_person.person = [(10, 50, 250, 450, 0.80), (300, 60, 560, 460, 0.70)]
r = verify(); check("two real people are counted as 2", r["is_human"] and r["people_detected"] == 2, r)
reset(); fake_person.person = [(100, 50, 400, 450, 0.80), (500, 60, 600, 200, 0.45)]
r = verify(); check("a weak extra box next to a strong one is not added to the count", r["people_detected"] == 1, r)
reset()
r = verify(); check("empty frame is not human", not r["is_human"], r)
reset(); fake_person.person = [(0, 0, 630, 470, 0.80)]
verify(); res = client.get("/entry").get_json()
check("/entry after a rejected frame changes nothing", res["status"] == "ignored" and app.entry_count == 0, res)
reset(); fake_person.person = [(100, 50, 400, 450, 0.72)]
verify(); res = client.get("/entry").get_json()
check("/entry after a real person still counts +1", res["status"] == "ok" and app.entry_count == 1, res)
verify(); client.get("/exit")
check("/exit still counts", app.exit_count == 1 and app.people_inside_count() == 0)

print("\n== WEAPONS: false alarms ==")
BOX = (200, 150, 330, 260, 0)      # ~4.6% of the frame
def gun(conf, box=BOX[:4], cls=0): return (*box, conf, cls)

reset(); fake_threat.det = [gun(0.55)]
r = verify(); check("weapon below 0.60 is ignored", not r["threat_detected"], r)
reset(); fake_threat.det = [gun(0.70)]
r = verify(); check("one 0.70 sighting alone does not raise the alarm", not r["threat_detected"] and app.latest_threat_alert is None, r)
r = verify(); check("the same weapon in a 2nd frame does", r["threat_detected"] and "Gun" in r["threats"], r)
check("alert carries the confidence", app.latest_threat_alert and app.latest_threat_alert.get("confidence") == 0.70, app.latest_threat_alert)
reset(); fake_threat.det = [gun(0.90)]
r = verify(); check("a very sure (0.90) weapon alerts immediately", r["threat_detected"], r)
reset(); fake_threat.det = [gun(0.70)]; fake_person.everyday = [(190, 140, 340, 270, 0.8)]
verify(); r = verify(); check("a 'gun' sitting on a phone is dropped, even seen twice", not r["threat_detected"], r)
reset(); fake_threat.det = [gun(0.95, box=(10, 10, 14, 14))]
r = verify(); check("a tiny 'weapon' speck is ignored", not r["threat_detected"], r)
reset(); fake_threat.det = [gun(0.95, box=(0, 0, 600, 450))]
r = verify(); check("a 'weapon' filling the whole frame is ignored", not r["threat_detected"], r)
reset(); fake_threat.det = [gun(0.95, cls=2)]
r = verify(); check("a non-weapon class (Phone) never alerts", not r["threat_detected"], r)
reset(); fake_threat.det = [gun(0.70, cls=0)]; verify()
fake_threat.det = [gun(0.70, cls=1)]; r = verify()
check("one Gun sighting + one Knife sighting do not confirm each other", not r["threat_detected"], r)
reset(); fake_threat.det = [gun(0.70)]; verify()
app._threat_hits.clear(); app._threat_hits.append((time.time() - 100, "Gun"))     # an old sighting
r = verify(); check("an old sighting (100 s ago) does not count towards confirmation", not r["threat_detected"], r)

print("\n== WEAPONS: scanned image and evidence photo ==")
reset(); fake_person.person = [(100, 50, 400, 450, 0.80)]; fake_threat.det = [gun(0.95)]
verify()
seen = fake_threat.seen[-1]
drawn = int((np.abs(seen.astype(int) - FRAME.astype(int)).sum(axis=2) > 60).sum()) if seen.shape == FRAME.shape else -1
check("the weapon model receives the RAW frame (no boxes or text drawn on it)", drawn == 0, f"{drawn} drawn pixels")
saved = latest_saved_image()
edge = saved[150:260, 200:203].reshape(-1, 3).mean(axis=0)       # left edge of the weapon box
check("the saved audit photo has the weapon box drawn in red", edge[2] > 150 and edge[0] < 110, edge)
events = [e for e in database.get_all_events(20) if e["direction"] == "THREAT_ALERT"]
check("alert is logged in the database with its confidence", events and "95%" in str(events[0]["trigger_type"]), events[:1])
status = client.get("/status")
with client.session_transaction() as sess:
    sess["authenticated"] = True; sess["username"] = "admin"
status = client.get("/status").get_json()
check("/status keeps every key the dashboard needs",
      all(k in status for k in ("threat_alert", "threat_detected", "people_inside", "max_capacity", "latest_image", "alerts")))

print("\n== WEAPONS: label matching ==")
for name, expected in (("Gun", True), ("Handgun", True), ("knife", True), ("gun_pistol", True), ("", False),
                       ("g", False), ("n", False), ("Phone", False), ("person", False), ("Gunnery", False)):
    check(f"label {name!r} -> {expected}", app.threat_label_matches(name) is expected)

print("\n== CAMERA ORIENTATION ==")
reset(); verify(); verify()
check("default: frames keep their original shape (no hidden rotation)", latest_saved_image().shape[:2] == (H, W))
reset(); app.CAMERA_ROTATION = 90; verify()
check("CAMERA_ROTATION=90 turns the saved frame to portrait", latest_saved_image().shape[:2] == (W, H))
reset(); app.CAMERA_ROTATION = 180
top_marked = FRAME.copy(); top_marked[:100, :] = (0, 0, 255)           # red band at the TOP
verify(cv2.imencode(".jpg", top_marked)[1].tobytes())
saved = latest_saved_image()
check("CAMERA_ROTATION=180 puts that band at the BOTTOM", saved[-50:, :].reshape(-1, 3).mean(axis=0)[2] > 150
      and saved[:50, :].reshape(-1, 3).mean(axis=0)[2] < 120)
reset(); app.AUTO_ROTATE_SEARCH = True
fake_person.person_fn = lambda f: [(50, 40, 300, 560, 0.8)] if f.shape[0] > f.shape[1] else []
r = verify(); check("auto-rotate (opt-in) can find a sideways person in that frame", r["is_human"], r)
fake_person.person_fn = lambda f: []
r = verify()
check("... and does NOT lock the rotation: the next frame is back to normal", latest_saved_image().shape[:2] == (H, W))

print("\nALL PASS" if failures == 0 else f"\n{failures} CHECK(S) FAILED")
if not _cred_existed and os.path.exists(_cred):
    os.remove(_cred)
sys.exit(1 if failures else 0)
