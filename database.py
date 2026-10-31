"""
database.py - SQLite persistence layer for the Dynamic Safety Zone Monitoring System.

Uses only the Python standard library (sqlite3), so there is nothing to install.
The database file "crowd_system.db" is created next to this file, so it is found
no matter which folder you launch the server from.

Typical use inside Flask (app.py):

    import database
    database.init_db()                                   # once at startup
    occupancy = database.get_latest_occupancy()          # restore headcount after restart
    database.log_event("ENTRY", 1, occupancy, "verify_2026....jpg", "Human")
"""

import csv
import io
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crowd_system.db")

# ENTRY / EXIT : a confirmed person crossing the gate (counts change)
# TRIGGER      : a beam-break photo where YOLO found a person (audit record)
# OBJECT       : a beam-break photo where YOLO found no person (audit record, nothing counted)
# RESET        : counters cleared, so get_latest_occupancy() returns 0 afterwards
# THREAT_ALERT : weapon/threat detected on incoming verification frame
VALID_DIRECTIONS = ("ENTRY", "EXIT", "RESET", "TRIGGER", "OBJECT", "THREAT_ALERT")

logger = logging.getLogger(__name__)


@contextmanager
def _get_connection():
    """Open a short-lived connection, commit on success, roll back on error, always close.

    A fresh connection per call keeps this safe with Flask's multi-threaded server,
    because SQLite connections must not be shared between threads.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create the database, the events table and its indexes if they don't exist yet.

    Safe to call on every startup.
    """
    with _get_connection() as conn:
        # WAL lets the dashboard read while the ESP32 events are being written.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp         TEXT    NOT NULL,
                direction         TEXT    NOT NULL,
                people_count      INTEGER NOT NULL,
                current_occupancy INTEGER NOT NULL,
                image_filename    TEXT,
                trigger_type      TEXT
            )
            """
        )
        # Databases created before trigger_type existed are upgraded in place; old rows keep NULL.
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(events)")]
        if "trigger_type" not in columns:
            conn.execute("ALTER TABLE events ADD COLUMN trigger_type TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events (timestamp)")
    logger.info("Database ready at %s", DB_PATH)


def log_event(direction, people_count, current_occupancy, image_filename=None, trigger_type=None):
    """Insert one event with the current local timestamp. Returns the new row id.

    direction: "ENTRY", "EXIT", "RESET", "TRIGGER" or "OBJECT" - case-insensitive.
    trigger_type: free text shown in the dashboard, e.g. "Human" or "Object/False Trigger".
    """
    direction = str(direction).upper()
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {VALID_DIRECTIONS}, got {direction!r}")

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO events (timestamp, direction, people_count, current_occupancy, image_filename, trigger_type)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (timestamp, direction, int(people_count), int(current_occupancy), image_filename, trigger_type),
        )
        return cursor.lastrowid


def get_all_events(limit=50):
    """Return the most recent events as a list of dicts, newest first."""
    limit = max(1, int(limit))
    with _get_connection() as conn:
        # id is the tie-breaker for events that share the same second.
        rows = conn.execute(
            "SELECT * FROM events ORDER BY timestamp DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_latest_occupancy():
    """Return the most recent current_occupancy, or 0 if there are no events yet.

    Used at startup to restore the live headcount after a server restart.
    """
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT current_occupancy FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return int(row["current_occupancy"]) if row else 0


# ----------------------------------------------------------------------------
# Extras (not required, but useful for analytics and the demo)
# ----------------------------------------------------------------------------

def get_stats():
    """Summary numbers for an analytics panel.

    Totals count people (people_count summed), not just events, so a group of 3
    entering at once counts as 3.
    """
    with _get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN direction = 'ENTRY' THEN people_count END), 0) AS total_entries,
                COALESCE(SUM(CASE WHEN direction = 'EXIT'  THEN people_count END), 0) AS total_exits,
                COALESCE(MAX(current_occupancy), 0)                                   AS peak_occupancy,
                COUNT(*)                                                              AS total_events
            FROM events
            """
        ).fetchone()
    return dict(row)


def get_hourly_traffic(hours=24):
    """People entering/exiting per hour for the last N hours - ready for a bar chart.

    Returns a list like [{"hour": "2026-10-30 14:00", "entries": 12, "exits": 7}, ...]
    """
    hours = max(1, int(hours))
    with _get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                strftime('%Y-%m-%d %H:00', timestamp) AS hour,
                COALESCE(SUM(CASE WHEN direction = 'ENTRY' THEN people_count END), 0) AS entries,
                COALESCE(SUM(CASE WHEN direction = 'EXIT'  THEN people_count END), 0) AS exits
            FROM events
            WHERE timestamp >= datetime('now', 'localtime', ?)
            GROUP BY hour
            ORDER BY hour
            """,
            (f"-{hours} hours",),
        ).fetchall()
    return [dict(row) for row in rows]


def export_events_csv(limit=None):
    """Return all events (or the latest `limit`) as CSV text, oldest first.

    In Flask, return it with mimetype "text/csv" to give authorities a downloadable report.
    """
    query = "SELECT * FROM events ORDER BY id ASC"
    params = ()
    if limit is not None:
        query = "SELECT * FROM (SELECT * FROM events ORDER BY id DESC LIMIT ?) ORDER BY id ASC"
        params = (max(1, int(limit)),)

    with _get_connection() as conn:
        rows = conn.execute(query, params).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "timestamp", "direction", "people_count", "current_occupancy", "image_filename", "trigger_type"])
    for row in rows:
        writer.writerow([row["id"], row["timestamp"], row["direction"], row["people_count"],
                         row["current_occupancy"], row["image_filename"], row["trigger_type"]])
    return output.getvalue()


def delete_old_events(days=30):
    """Delete events older than N days so the database doesn't grow forever.

    Returns the number of rows deleted.
    """
    days = max(1, int(days))
    with _get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM events WHERE timestamp < datetime('now', 'localtime', ?)",
            (f"-{days} days",),
        )
        return cursor.rowcount


if __name__ == "__main__":
    # Quick self-test: python database.py
    # Runs against a throw-away temporary file, so it never touches crowd_system.db.
    import tempfile
    logging.basicConfig(level=logging.INFO)
    DB_PATH = os.path.join(tempfile.mkdtemp(), "selftest.db")
    init_db()
    print("Latest occupancy on startup:", get_latest_occupancy())
    log_event("TRIGGER", 1, 0, "test.jpg", "Human")
    log_event("ENTRY", 1, 1, "test.jpg", "Human")
    log_event("OBJECT", 0, 1, "box.jpg", "Object/False Trigger")
    print("Recent events:", get_all_events(limit=5))
    print("Stats:", get_stats())
    print("Self-test finished (temporary database, nothing saved).")
