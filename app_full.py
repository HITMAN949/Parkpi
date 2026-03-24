"""
ParkPi — Smart Parking System  (full version with plate recognition)
Flask + WebSocket + HC-SR04 sensors + OpenCV plate recognition + servo barrier.
"""

import time
import threading
import sqlite3
import json
import logging
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_sock import Sock

try:
    import RPi.GPIO as GPIO
    ON_PI = True
except ImportError:
    ON_PI = False

from plate_recognition.recognizer import (
    PlateRecognizer, SimulatedRecognizer, HAS_CV2, HAS_PICAM
)

# ─────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────

DB_PATH = "parking.db"
SENSOR_POLL_INTERVAL = 2.0
OCCUPIED_THRESHOLD_CM = 30.0

SPOT_PINS = {
    "A1": (4, 17),  "A2": (27, 22), "A3": (10, 9),  "A4": (11, 5),
    "B1": (6, 13),  "B2": (19, 26), "B3": (20, 21),  "B4": (16, 12),
    "C1": (24, 25), "C2": (8, 7),   "C3": (1, 0),    "C4": (23, 18),
}

LED_PINS = {
    "A1": (2, 3),
    "A2": (14, 15),
}

BARRIER_PIN = 18

app = Flask(__name__)
sock = Sock(app)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("parkpi")

state_lock = threading.Lock()
spot_states = {}
connected_clients = set()

# ─────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS spots (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'free',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            spot_id TEXT NOT NULL,
            plate TEXT NOT NULL,
            date TEXT NOT NULL,
            time_from TEXT NOT NULL,
            time_to TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            FOREIGN KEY (spot_id) REFERENCES spots(id)
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            spot_id TEXT,
            event_type TEXT NOT NULL,
            detail TEXT,
            ts TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plate_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate TEXT NOT NULL,
            action TEXT NOT NULL,
            spot_id TEXT,
            ts TEXT NOT NULL
        );
    """)
    now = datetime.now().isoformat()
    for sid in SPOT_PINS:
        cur.execute(
            "INSERT OR IGNORE INTO spots (id, status, updated_at) VALUES (?, 'free', ?)",
            (sid, now)
        )
    con.commit()
    con.close()
    log.info("Database ready")


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def load_states_from_db():
    global spot_states
    with db() as con:
        rows = con.execute("SELECT id, status FROM spots").fetchall()
    with state_lock:
        for row in rows:
            spot_states[row["id"]] = row["status"]


def persist_state(spot_id, status):
    with db() as con:
        con.execute(
            "UPDATE spots SET status=?, updated_at=? WHERE id=?",
            (status, datetime.now().isoformat(), spot_id)
        )
        con.execute(
            "INSERT INTO events (spot_id, event_type, detail, ts) VALUES (?, 'state_change', ?, ?)",
            (spot_id, status, datetime.now().isoformat())
        )

# ─────────────────────────────────────────────────
# GPIO helpers
# ─────────────────────────────────────────────────

def setup_gpio():
    if not ON_PI:
        return
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for sid, (trig, echo) in SPOT_PINS.items():
        GPIO.setup(trig, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(echo, GPIO.IN)
    for sid, (g, r) in LED_PINS.items():
        GPIO.setup(g, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(r, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(BARRIER_PIN, GPIO.OUT)
    log.info("GPIO initialised")


def measure_distance_cm(trig, echo) -> float:
    GPIO.output(trig, GPIO.HIGH)
    time.sleep(0.00001)
    GPIO.output(trig, GPIO.LOW)
    start = time.time()
    while GPIO.input(echo) == 0:
        start = time.time()
        if time.time() - start > 0.05:
            return 999.0
    stop = time.time()
    while GPIO.input(echo) == 1:
        stop = time.time()
        if stop - start > 0.05:
            return 999.0
    return ((stop - start) * 34300) / 2


def set_led(spot_id, occupied: bool):
    if not ON_PI or spot_id not in LED_PINS:
        return
    g, r = LED_PINS[spot_id]
    GPIO.output(g, GPIO.LOW if occupied else GPIO.HIGH)
    GPIO.output(r, GPIO.HIGH if occupied else GPIO.LOW)


def open_barrier(duration=5):
    if not ON_PI:
        log.info("[SIM] Barrier open for %ds", duration)
        return
    pwm = GPIO.PWM(BARRIER_PIN, 50)
    pwm.start(7.5)
    time.sleep(0.5)
    pwm.ChangeDutyCycle(12.5)
    time.sleep(duration)
    pwm.ChangeDutyCycle(7.5)
    time.sleep(0.5)
    pwm.stop()

# ─────────────────────────────────────────────────
# Plate recognition callback
# ─────────────────────────────────────────────────

def on_plate_detected(plate: str):
    """
    Called by PlateRecognizer whenever a confirmed plate is read.
    Checks if plate has an active reservation → opens barrier.
    Logs all detections to plate_log table.
    """
    log.info("Plate at gate: %s", plate)
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M")

    with db() as con:
        # Find active reservation matching this plate and current time window
        reservation = con.execute("""
            SELECT r.id, r.spot_id, r.time_from, r.time_to
            FROM reservations r
            WHERE r.plate = ?
              AND r.date = ?
              AND r.time_from <= ?
              AND r.time_to >= ?
              AND r.status = 'active'
            ORDER BY r.time_from
            LIMIT 1
        """, (plate, today, current_time, current_time)).fetchone()

        action = "granted" if reservation else "denied"
        con.execute(
            "INSERT INTO plate_log (plate, action, spot_id, ts) VALUES (?, ?, ?, ?)",
            (plate, action,
             reservation["spot_id"] if reservation else None,
             now.isoformat())
        )

    # Broadcast plate detection event to dashboard
    broadcast({
        "type": "plate_detected",
        "plate": plate,
        "action": action,
        "spot_id": reservation["spot_id"] if reservation else None,
        "ts": now.isoformat(),
    })

    if reservation:
        log.info("Access granted → spot %s", reservation["spot_id"])
        threading.Thread(target=open_barrier, args=(6,), daemon=True).start()
    else:
        log.info("Access denied — no reservation found for %s", plate)

# ─────────────────────────────────────────────────
# Sensor polling
# ─────────────────────────────────────────────────

def simulate_distance(spot_id) -> float:
    import random
    with state_lock:
        st = spot_states.get(spot_id, "free")
    if st == "occupied":
        return random.uniform(5, 20)
    return random.uniform(80, 200)


def poll_sensors():
    log.info("Sensor polling started")
    while True:
        changed = []
        for spot_id, (trig, echo) in SPOT_PINS.items():
            dist = measure_distance_cm(trig, echo) if ON_PI else simulate_distance(spot_id)
            new_status = "occupied" if dist < OCCUPIED_THRESHOLD_CM else None

            with state_lock:
                current = spot_states.get(spot_id, "free")

            if current == "reserved":
                time.sleep(0.05)
                continue

            if new_status and current != "occupied":
                with state_lock:
                    spot_states[spot_id] = "occupied"
                persist_state(spot_id, "occupied")
                set_led(spot_id, True)
                changed.append((spot_id, "occupied"))
            elif not new_status and current == "occupied":
                with state_lock:
                    spot_states[spot_id] = "free"
                persist_state(spot_id, "free")
                set_led(spot_id, False)
                changed.append((spot_id, "free"))

            time.sleep(0.05)

        if changed:
            broadcast({"type": "state_update", "changes": changed})

        time.sleep(SENSOR_POLL_INTERVAL)


def check_reservations():
    while True:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        current_time = now.strftime("%H:%M")
        with db() as con:
            expired = con.execute("""
                SELECT spot_id FROM reservations
                WHERE date = ? AND time_to <= ? AND status = 'active'
            """, (today, current_time)).fetchall()
            for row in expired:
                sid = row["spot_id"]
                con.execute(
                    "UPDATE reservations SET status='expired' WHERE spot_id=? AND status='active'",
                    (sid,)
                )
                with state_lock:
                    if spot_states.get(sid) == "reserved":
                        spot_states[sid] = "free"
                persist_state(sid, "free")
                broadcast({"type": "state_update", "changes": [(sid, "free")]})
        time.sleep(30)

# ─────────────────────────────────────────────────
# WebSocket
# ─────────────────────────────────────────────────

def broadcast(data: dict):
    msg = json.dumps(data)
    dead = set()
    for ws in list(connected_clients):
        try:
            ws.send(msg)
        except Exception:
            dead.add(ws)
    connected_clients.difference_update(dead)


@sock.route("/ws")
def websocket(ws):
    connected_clients.add(ws)
    with state_lock:
        snapshot = dict(spot_states)
    ws.send(json.dumps({"type": "snapshot", "states": snapshot}))
    try:
        while True:
            ws.receive()
    except Exception:
        pass
    finally:
        connected_clients.discard(ws)

# ─────────────────────────────────────────────────
# REST API
# ─────────────────────────────────────────────────

@app.get("/api/spots")
def api_spots():
    with state_lock:
        snapshot = dict(spot_states)
    with db() as con:
        reservations = con.execute(
            "SELECT spot_id, plate, date, time_from, time_to FROM reservations WHERE status='active'"
        ).fetchall()
    res_map = {r["spot_id"]: dict(r) for r in reservations}
    return jsonify([
        {"id": sid, "status": st, "reservation": res_map.get(sid)}
        for sid, st in snapshot.items()
    ])


@app.post("/api/reserve")
def api_reserve():
    data = request.get_json()
    spot_id   = data.get("spot_id", "").strip().upper()
    plate     = data.get("plate", "").strip().upper()
    date      = data.get("date", "")
    time_from = data.get("time_from", "")
    time_to   = data.get("time_to", "")

    if not all([spot_id, plate, date, time_from, time_to]):
        return jsonify({"error": "All fields required"}), 400
    if spot_id not in SPOT_PINS:
        return jsonify({"error": "Unknown spot"}), 404

    with state_lock:
        current = spot_states.get(spot_id)
    if current != "free":
        return jsonify({"error": f"Spot {spot_id} is {current}"}), 409

    with db() as con:
        con.execute("""
            INSERT INTO reservations (spot_id, plate, date, time_from, time_to, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?)
        """, (spot_id, plate, date, time_from, time_to, datetime.now().isoformat()))

    with state_lock:
        spot_states[spot_id] = "reserved"
    persist_state(spot_id, "reserved")
    broadcast({"type": "state_update", "changes": [(spot_id, "reserved")]})
    return jsonify({"ok": True, "spot_id": spot_id, "plate": plate})


@app.delete("/api/reserve/<spot_id>")
def api_cancel(spot_id):
    spot_id = spot_id.upper()
    with db() as con:
        con.execute(
            "UPDATE reservations SET status='cancelled' WHERE spot_id=? AND status='active'",
            (spot_id,)
        )
    with state_lock:
        if spot_states.get(spot_id) == "reserved":
            spot_states[spot_id] = "free"
    persist_state(spot_id, "free")
    broadcast({"type": "state_update", "changes": [(spot_id, "free")]})
    return jsonify({"ok": True})


@app.get("/api/reservations")
def api_reservations():
    with db() as con:
        rows = con.execute(
            "SELECT * FROM reservations ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/plate-log")
def api_plate_log():
    with db() as con:
        rows = con.execute(
            "SELECT * FROM plate_log ORDER BY ts DESC LIMIT 30"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/stats")
def api_stats():
    with state_lock:
        snap = dict(spot_states)
    total = len(snap)
    free  = sum(1 for s in snap.values() if s == "free")
    occ   = sum(1 for s in snap.values() if s == "occupied")
    res   = sum(1 for s in snap.values() if s == "reserved")
    return jsonify({
        "total": total, "free": free, "occupied": occ, "reserved": res,
        "occupancy_pct": round((occ + res) / total * 100) if total else 0
    })


@app.post("/api/barrier/open")
def api_barrier():
    data = request.get_json(silent=True, force=True) or {}
    secs = int(data.get("seconds", 5))
    threading.Thread(target=open_barrier, args=(secs,), daemon=True).start()
    return jsonify({"ok": True, "duration": secs})


@app.get("/")
def index():
    return render_template("index.html")

# ─────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    load_states_from_db()
    setup_gpio()

    # Start plate recognizer
    if HAS_CV2 and (HAS_PICAM or ON_PI):
        recognizer = PlateRecognizer(
            on_plate_detected=on_plate_detected,
            use_picamera=HAS_PICAM,
            save_debug_frames=True,
        )
    else:
        log.info("Starting simulated plate recognizer")
        recognizer = SimulatedRecognizer(on_plate_detected=on_plate_detected)
    recognizer.start()

    threading.Thread(target=poll_sensors,       daemon=True).start()
    threading.Thread(target=check_reservations, daemon=True).start()

    log.info("ParkPi running on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
