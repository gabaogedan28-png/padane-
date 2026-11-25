import os
import re
import sqlite3
import random
import requests
from datetime import datetime
from functools import wraps

from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, template_folder="templates", static_folder="static", instance_relative_config=True)
app.secret_key = "padane-dev-secret-change-me"  # dev-only key; swap for an env var in production
os.makedirs(app.instance_path, exist_ok=True)
DB_FILE = os.path.join(app.instance_path, "database.db")

print("🚀 Backend is attempting to start...")


# ==============================================================================
# DATABASE HELPERS
# ==============================================================================
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def now():
    return datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")


def log_audit(actor, role, action, details="", source="Web Interface"):
    """Writes one row into the audit trail so every coordinated action is traceable."""
    conn = get_db()
    conn.execute(
        """INSERT INTO audit_log (timestamp, actor, role, action, details, ip_address, source)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (now(), actor, role, action, details, request.remote_addr or "unknown", source),
    )
    conn.commit()
    conn.close()


# ==============================================================================
# GNSS PARSING + REVERSE GEOCODING
#
# The hardware sometimes forwards a raw GNSS fix string instead of clean
# decimal lat/lng, e.g.:
#   "1043.02846,N,12234.04194,E,070726,082346.00,71.8,0.000,174.33"
# Fields: lat, lat_dir, lon, lon_dir, date(ddmmyy), time(hhmmss.ss),
#         altitude, speed, course
# These helpers turn that into decimal coordinates and then into a
# human-readable place name, instead of showing raw numbers to the user.
# ==============================================================================
def _nmea_coord_to_decimal(raw_value, direction):
    """Converts NMEA ddmm.mmmm / dddmm.mmmm coordinate format to decimal degrees."""
    if raw_value is None or direction is None:
        return None
    raw_value = float(raw_value)
    degrees = int(raw_value / 100)
    minutes = raw_value - (degrees * 100)
    decimal = degrees + (minutes / 60)
    if direction.strip().upper() in ("S", "W"):
        decimal = -decimal
    return round(decimal, 6)


def parse_gnss_string(raw):
    """
    Parses a raw GNSS fix string like:
    "1043.02846,N,12234.04194,E,070726,082346.00,71.8,0.000,174.33"
    Returns (lat_decimal, lng_decimal) or None if the string doesn't match.
    """
    if not raw or not isinstance(raw, str):
        return None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) < 4:
        return None
    try:
        lat = _nmea_coord_to_decimal(parts[0], parts[1])
        lng = _nmea_coord_to_decimal(parts[2], parts[3])
        if lat is None or lng is None:
            return None
        return lat, lng
    except (ValueError, IndexError):
        return None


def reverse_geocode(lat, lng):
    """
    Looks up a detailed, human-readable location for a lat/lng pair using
    OpenStreetMap's free Nominatim API. Builds a string from most to least
    specific: named building/POI (if the fix lands on one) or street,
    then barangay, then city/municipality, then province. Falls back to
    plain decimal coordinates if the lookup fails or times out.
    """
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "lat": lat,
                "lon": lng,
                "format": "json",
                "zoom": 18,
                "addressdetails": 1,
                "namedetails": 1,
            },
            headers={"User-Agent": "PADANE-Device-Tracker/1.0"},
            timeout=5,
        )
        if not resp.ok:
            return f"{lat:.5f}, {lng:.5f}"

        data = resp.json()
        addr = data.get("address", {})
        namedetails = data.get("namedetails", {}) or {}

        # Named building / point of interest, if the fix lands on or near one
        building = (
            namedetails.get("name")
            or addr.get("building")
            or addr.get("amenity")
            or addr.get("shop")
            or addr.get("office")
            or addr.get("tourism")
            or addr.get("leisure")
        )

        # Street-level detail, used as a fallback when there's no named building
        street = " ".join(filter(None, [addr.get("house_number"), addr.get("road")])) or None

        # Barangay-level detail -- PH barangays are usually tagged as suburb/
        # neighbourhood/quarter/village in OSM data
        barangay = (
            addr.get("suburb") or addr.get("neighbourhood")
            or addr.get("quarter") or addr.get("village")
        )
        if barangay and not barangay.lower().startswith("brgy"):
            barangay = f"Brgy. {barangay}"

        city = addr.get("city") or addr.get("town") or addr.get("municipality") or addr.get("county")
        province = addr.get("state") or addr.get("region")

        parts = []
        if building:
            parts.append(building)
        elif street:
            parts.append(street)
        if barangay:
            parts.append(barangay)
        if city:
            parts.append(city)
        if province and province != city:
            parts.append(province)

        # Drop consecutive duplicates (e.g. a POI name matching the city name)
        deduped = []
        for p in parts:
            if not deduped or deduped[-1] != p:
                deduped.append(p)

        if deduped:
            return ", ".join(deduped)
        if data.get("display_name"):
            return data["display_name"]
    except requests.RequestException:
        pass
    return f"{lat:.5f}, {lng:.5f}"


# ==============================================================================
# DATABASE INITIALIZATION
# ==============================================================================
def init_db():
    """Initializes the database, seeds a default admin, and creates every
    supporting table needed for the user <-> admin coordination features."""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            fullname TEXT NOT NULL,
            address TEXT,
            contactnumber TEXT,
            role TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'active'
        )
    ''')
    # Migrate older DBs that don't have the 'status' column yet
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    except sqlite3.OperationalError:
        pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            device_serial TEXT UNIQUE NOT NULL,
            device_type TEXT NOT NULL DEFAULT 'Assistive Device',
            status TEXT NOT NULL DEFAULT 'Paired',
            sos_triggered INTEGER NOT NULL DEFAULT 0,
            battery_level INTEGER NOT NULL DEFAULT 100,
            last_lat REAL,
            last_lng REAL,
            last_address TEXT,
            last_activity TEXT,
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')
    try:
        cursor.execute(
            "ALTER TABLE devices ADD COLUMN sos_triggered INTEGER NOT NULL DEFAULT 0"
    )
    except sqlite3.OperationalError:
        pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            alert_type TEXT NOT NULL,
            lat REAL,
            lng REAL,
            address TEXT,
            channels TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT,
            resolved_at TEXT,
            resolved_by TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS caregivers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            relation TEXT,
            phone TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            actor TEXT,
            role TEXT,
            action TEXT,
            details TEXT,
            ip_address TEXT,
            source TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS support_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT,
            description TEXT,
            severity TEXT DEFAULT 'Medium',
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT,
            resolved_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    # --------------------------------------------------------------------
    # USER <-> ADMIN COORDINATION LAYER
    # --------------------------------------------------------------------
    # Acknowledgement fields on alerts: lets an admin mark "I'm responding"
    # BEFORE the incident is fully resolved. The ESP32 picks this up on its
    # next telemetry check-in and gives the wearer physical feedback
    # (buzzer + confirmation SMS) that help is coming.
    for ddl in [
        "ALTER TABLE alerts ADD COLUMN acknowledged INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE alerts ADD COLUMN acknowledged_by TEXT",
        "ALTER TABLE alerts ADD COLUMN acknowledged_at TEXT",
        "ALTER TABLE alerts ADD COLUMN device_notified INTEGER NOT NULL DEFAULT 0",
    ]:
        try:
            cursor.execute(ddl)
        except sqlite3.OperationalError:
            pass

    # Two-way chat between a user and admin. One thread per user_id;
    # sender_role tells us which side of the conversation a row came from.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            sender_role TEXT NOT NULL,
            sender_name TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT,
            read_by_user INTEGER NOT NULL DEFAULT 0,
            read_by_admin INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    # Remote commands an admin can queue for a specific ESP32 unit. The
    # device drains its pending commands on its next telemetry check-in
    # (see /api/iot/telemetry), so delivery can take up to ~gnssInterval.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS device_commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER NOT NULL,
            device_serial TEXT NOT NULL,
            command TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            requested_by TEXT,
            created_at TEXT,
            delivered_at TEXT,
            FOREIGN KEY (device_id) REFERENCES devices (id)
        )
    ''')

    # Seed a default administrator account if it doesn't exist yet
    cursor.execute("SELECT * FROM users WHERE username = 'admin'")
    if not cursor.fetchone():
        hashed_pw = generate_password_hash("padane")
        cursor.execute('''
            INSERT INTO users (username, password, fullname, address, contactnumber, role, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', ("admin", hashed_pw, "System Administrator", "HQ Command Center", "911", "admin", "active"))
        print("💡 Default Admin seeded! Username: 'admin' | Password: 'admin123'")

    conn.commit()
    conn.close()


# ==============================================================================
# AUTH DECORATORS
# ==============================================================================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Not authenticated. Please log in again."}), 401
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Not authenticated. Please log in again."}), 401
        if session.get("role") != "admin":
            return jsonify({"error": "Administrator access required."}), 403
        return f(*args, **kwargs)
    return wrapper


# ==============================================================================
# FRONTEND ROUTING (Serving your HTML files)
# ==============================================================================
@app.route('/')
def home():
    return redirect(url_for('user_manual_page'))

@app.route('/usermanual.html')
def user_manual_page():
    return render_template('auth/usermanual.html')

@app.route('/login.html')
def login_page():
    return render_template('auth/login.html')

@app.route('/register.html')
def register_page():
    return render_template('auth/register.html')

@app.route('/index.html')
def user_dashboard():
    return render_template('dashboard/index.html')

@app.route('/admin.html')
def admin_dashboard():
    return render_template('admin/admin.html')

@app.route('/audit_trail.html')
def audit_trail():
    return render_template('admin/audit_trail.html')

@app.route('/user_profiles.html')
def user_profiles():
    return render_template('admin/user_profiles.html')

@app.route('/device_management.html')
def device_management():
    return render_template('admin/device_management.html')

@app.route('/trigger.html')
def trigger():
    return render_template('dashboard/trigger.html')

@app.route('/nearest-help.html')
def nearest_help():
    return render_template('dashboard/nearest-help.html')

@app.route('/server-ping.html')
def server_ping():
    return render_template('dashboard/server-ping.html')

@app.route('/alert-caregivers.html')
def alert_caregivers():
    return render_template('dashboard/alert-caregivers.html')

@app.route('/account-settings.html')
def account_settings():
    return render_template('dashboard/account-settings.html')

@app.route('/support-help.html')
def support_help():
    return render_template('dashboard/support-help.html')

@app.route('/real_alert.html')
def real_alert_page():
    return render_template('dashboard/real_alert.html')

@app.route('/user_setting.html')
def user_setting():
    return render_template('dashboard/user_setting.html')

# ------------------------------------------------------------------------
# Public endpoint for the ESP32's PHYSICAL SOS BUTTON.
# This is the ONLY place a GNSS fix gets written to last_lat/last_lng and
# the ONLY place sos_triggered gets set to 1. Routine telemetry pings
# (see /api/iot/telemetry below) intentionally do NOT touch location, so
# nothing reaches the tracking console until the button is actually pressed.
#
# The alert row inserted here is tagged 'sos_hardware' (rather than plain
# ------------------------------------------------------------------------
# GEOCODE LOOKUP (used by the ESP32 firmware to build a human-readable
# SMS body before it sends the text over the modem -- the device only has
# raw lat/lng from GNSS, so it asks the Flask PC, which has internet
# access, to turn that into a readable place name via reverse_geocode()).
# No login required -- same pattern as the other hardware-facing routes.
# ------------------------------------------------------------------------
@app.route('/api/geocode', methods=['GET'])
def api_geocode():
    lat = request.args.get('lat')
    lng = request.args.get('lng')
    if lat is None or lng is None:
        return jsonify({"error": "lat and lng query params are required"}), 400
    try:
        address = reverse_geocode(float(lat), float(lng))
    except (TypeError, ValueError):
        return jsonify({"error": "lat and lng must be numbers"}), 400
    return jsonify({"address": address}), 200


# 'sos') specifically so the console/admin views can tell a physical
# button-press apart from a manual on-screen trigger.
# ------------------------------------------------------------------------
@app.route('/api/trigger-hardware', methods=['POST'])
def api_trigger_hardware():
    data = request.get_json() or {}
    device_serial = data.get('device_serial')
    lat = data.get('lat')
    lng = data.get('lng')
    address = data.get('address')

    if not device_serial:
        return jsonify({"error": "device_serial is required"}), 400

    # The module sometimes sends a raw GNSS fix string (e.g. "1043.02846,N,
    # 12234.04194,E,070726,082346.00,71.8,0.000,174.33") in place of, or
    # alongside, lat/lng. If that's what we got, parse real coordinates out of it.
    if (lat is None or lng is None) and address:
        parsed = parse_gnss_string(address)
        if parsed:
            lat, lng = parsed

    if lat is None or lng is None:
        return jsonify({"error": "No valid GNSS coordinates were provided"}), 400

    # Always show a place name instead of raw numbers on the tracking console
    address = reverse_geocode(float(lat), float(lng))

    conn = get_db()
    cursor = conn.cursor()
    print("\n========== ESP32 REQUEST ==========")
    print(data)
    print("Device Serial:", device_serial)
    print("Latitude:", lat)
    print("Longitude:", lng)
    print("Address:", address)
    print("==================================")

    # Look up which user this hardware unit actually belongs to
    cursor.execute("SELECT id, user_id FROM devices WHERE device_serial = ?", (device_serial,))
    device = cursor.fetchone()
    print("Database Lookup Result:", device)

    if not device:
        conn.close()
        return jsonify({"error": "Unknown device_serial. Pair this device from the dashboard first."}), 404

    owner_id = device["user_id"]

    # 'sos_hardware' distinguishes a physical-button trigger from a manual
    # on-screen trigger in the alerts table.
    cursor.execute('''
        INSERT INTO alerts (user_id, alert_type, lat, lng, address, status, created_at)
        VALUES (?, 'sos_hardware', ?, ?, ?, 'open', ?)
    ''', (owner_id, lat, lng, address, now()))
    alert_id = cursor.lastrowid

    # Only on a physical button press do we record/expose a GNSS fix.
    cursor.execute('''
        UPDATE devices SET last_lat=?, last_lng=?, last_address=?, last_activity=?, status='Critical', sos_triggered=1
        WHERE id=?
    ''', (lat, lng, address, now(), device["id"]))

    conn.commit()
    conn.close()

    log_audit("Hardware Device", "device", "Triggered Emergency SOS", f"{device_serial} @ {address}", "ESP32 Hardware")

    print(f"✅ Hardware SOS received from {device_serial}: {lat}, {lng}")
    return jsonify({"message": "Hardware SOS recorded", "alert_id": alert_id, "sos_triggered": True}), 201

# ==============================================================================
# DEVICE STATUS API (Used by the frontend to poll ESP32 location)
#
# Location is only ever surfaced here while sos_triggered is true. Outside
# of an active SOS, last_lat/last_lng come back as null so the console has
# nothing to plot -- routine hardware activity never leaks a live position.
# The trigger.html console polls this endpoint and watches sos_triggered
# flip from false -> true to know exactly when the physical button fired.
# ==============================================================================
@app.route('/api/devices/status', methods=['GET'])
@login_required
def get_device_status():

    conn = get_db()

    device = conn.execute('''
        SELECT
            d.last_lat,
            d.last_lng,
            d.last_activity,
            d.last_address,
            d.device_serial,
            d.status,
            d.sos_triggered
        FROM devices d
        WHERE d.user_id = ?
        ORDER BY d.id DESC
        LIMIT 1
    ''', (session["user_id"],)).fetchone()

    conn.close()

    if not device:
        return jsonify({
            "paired": False
        }), 200

    sos_active = bool(device["sos_triggered"])

    return jsonify({
        "paired": True,
        # Withhold coordinates unless the physical SOS button has actually been triggered
        "last_lat": device["last_lat"] if sos_active else None,
        "last_lng": device["last_lng"] if sos_active else None,
        "last_activity": device["last_activity"],
        "last_address": device["last_address"] if sos_active else None,
        "status": device["status"],
        "sos_triggered": sos_active,
        "online": True
    }), 200

# Add this new route to your app.py
@app.route('/api/get-latest-location', methods=['GET'])
@login_required
def get_latest_location():
    conn = get_db()
    # Fetch the most recent device activity for the logged-in user
    row = conn.execute('''
        SELECT last_lat, last_lng, last_address, sos_triggered FROM devices
        WHERE user_id = ?
        ORDER BY id DESC LIMIT 1
    ''', (session["user_id"],)).fetchone()
    conn.close()

    # Same rule as /api/devices/status: no location outside an active SOS
    if row and row["last_lat"] and bool(row["sos_triggered"]):
        return jsonify({
            "lat": row["last_lat"],
            "lng": row["last_lng"],
            "address": row["last_address"]
        }), 200
    return jsonify({"error": "No active SOS location to report"}), 404

# ==============================================================================
# AUTH API
# ==============================================================================
@app.route('/api/register', methods=['POST'])
def api_register():
    data = request.get_json()

    username = data.get('username', '').strip()
    password = data.get('password', '')
    fullname = data.get('fullname', '').strip()
    address = data.get('address', '').strip()
    contactnumber = data.get('contactnumber', '').strip()

    if not username or not password or not fullname:
        return jsonify({"error": "Missing required fields (Username, Password, Full Name)"}), 400

    hashed_password = generate_password_hash(password)

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO users (username, password, fullname, address, contactnumber, role, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (username, hashed_password, fullname, address, contactnumber, 'user', 'active'))
        conn.commit()
        conn.close()
        log_audit(fullname, "user", "Registered account", f"Username: {username}", "Registration Portal")
        return jsonify({"message": "Registration successful"}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Username already exists. Choose another one."}), 400
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return jsonify({"error": "Please fill in all fields"}), 400

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, password, fullname, contactnumber, role, status FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()
    conn.close()

    if user and check_password_hash(user["password"], password):
        if user["status"] == "suspended":
            return jsonify({"error": "This account has been suspended by an administrator."}), 403

        # Real server-side session, on top of the client's sessionStorage flags
        session["user_id"] = user["id"]
        session["username"] = user["fullname"]
        session["login_name"] = username
        session["role"] = user["role"]

        log_audit(user["fullname"], user["role"], "User login", "Authenticated successfully", "Web Interface")

        return jsonify({
            "message": "Login successful",
            "user_id": user["id"],
            "name": user["fullname"],
            "emergency_contact": user["contactnumber"] if user["contactnumber"] else "Not Specified",
            "role": user["role"]
        }), 200
    else:
        return jsonify({"error": "Incorrect username or password"}), 401


@app.route('/api/logout', methods=['POST'])
def api_logout():
    if "user_id" in session:
        log_audit(session.get("username", "Unknown"), session.get("role", "user"), "User logout", "", "Web Interface")
    session.clear()
    return jsonify({"message": "Logged out"}), 200

# ==============================================================================
# EMERGENCY ALERTS (admin coordination views)
#
# NOTE: The old /api/trigger endpoint that let a logged-in user fire an SOS
# from the web UI has been removed. The physical button on the paired ESP32
# (via /api/trigger-hardware) is now the ONLY way to open an SOS alert.
# This route intentionally returns 410 Gone in case anything still calls it.
# ==============================================================================
@app.route('/api/trigger', methods=['POST'])
@login_required
def api_trigger():
    return jsonify({
        "error": "On-screen SOS triggering has been disabled. Press the physical button on your paired device to send an alert."
    }), 410


@app.route('/api/alerts', methods=['GET'])
@login_required
def api_alerts():
    conn = get_db()
    if session["role"] == "admin":
        rows = conn.execute('''
            SELECT alerts.*, users.fullname FROM alerts
            JOIN users ON users.id = alerts.user_id
            ORDER BY alerts.id DESC LIMIT 50
        ''').fetchall()
    else:
        rows = conn.execute('''
            SELECT alerts.*, users.fullname FROM alerts
            JOIN users ON users.id = alerts.user_id
            WHERE user_id = ? ORDER BY alerts.id DESC LIMIT 50
        ''', (session["user_id"],)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows]), 200


@app.route('/api/alerts/<int:alert_id>/acknowledge', methods=['POST'])
@admin_required
def api_acknowledge_alert(alert_id):
    """Admin marks an open alert as 'responding' without closing it out yet.
    This flips a flag the device drains on its next telemetry check-in
    (see /api/iot/telemetry) so the wearer gets a buzzer + SMS confirmation
    that help is on the way -- real two-way coordination, not just a
    one-way SOS."""
    conn = get_db()
    alert = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
    if not alert:
        conn.close()
        return jsonify({"error": "Alert not found"}), 404

    conn.execute('''
        UPDATE alerts SET acknowledged=1, acknowledged_by=?, acknowledged_at=?, device_notified=0
        WHERE id=?
    ''', (session["username"], now(), alert_id))
    conn.commit()
    conn.close()

    log_audit(session["username"], "admin", "Acknowledged incident (responding)", f"Alert #{alert_id}", "Support Portal")
    return jsonify({"message": "Alert acknowledged. The wearer's device will be notified shortly."}), 200


@app.route('/api/alerts/<int:alert_id>/resolve', methods=['POST'])
@admin_required
def api_resolve_alert(alert_id):
    conn = get_db()
    cursor = conn.cursor()

    # Look up the alert first so we can also clear the triggering device's SOS flag
    alert = cursor.execute("SELECT user_id FROM alerts WHERE id=?", (alert_id,)).fetchone()

    cursor.execute('''
        UPDATE alerts SET status='resolved', resolved_at=?, resolved_by=?, acknowledged=0 WHERE id=?
    ''', (now(), session["username"], alert_id))

    if alert:
        # Once resolved, stop exposing the last GNSS fix -- location only
        # reappears the next time the physical SOS button is pressed.
        cursor.execute('''
            UPDATE devices SET status='Paired', sos_triggered=0
            WHERE user_id=?
        ''', (alert["user_id"],))

    conn.commit()
    conn.close()
    log_audit(session["username"], "admin", "Resolved incident", f"Alert #{alert_id}", "Support Portal")
    return jsonify({"message": "Alert resolved"}), 200


# ==============================================================================
# DEVICE MANAGEMENT
# ==============================================================================
@app.route('/api/devices', methods=['GET'])
@login_required
def api_devices():
    conn = get_db()
    if session["role"] == "admin":
        rows = conn.execute('''
            SELECT devices.*, users.fullname FROM devices
            JOIN users ON users.id = devices.user_id
            ORDER BY devices.id DESC
        ''').fetchall()
    else:
        rows = conn.execute('''
            SELECT devices.*, users.fullname FROM devices
            JOIN users ON users.id = devices.user_id
            WHERE user_id = ? ORDER BY devices.id DESC
        ''', (session["user_id"],)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows]), 200


@app.route('/api/devices', methods=['POST'])
@login_required
def api_register_device():
    data = request.get_json() or {}
    device_serial = data.get('device_serial', '').strip() or f"PAD-{random.randint(100000, 999999)}"
    device_type = data.get('device_type', 'Assistive Device').strip()

    try:
        conn = get_db()
        conn.execute('''
            INSERT INTO devices (user_id, device_serial, device_type, status, battery_level, created_at)
            VALUES (?, ?, ?, 'Paired', ?, ?)
        ''', (session["user_id"], device_serial, device_type, random.randint(70, 100), now()))
        conn.commit()
        conn.close()
        log_audit(session["username"], session["role"], "Paired hardware unit", f"Device [{device_serial}]", "System")
        return jsonify({"message": "Device registered"}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "That device serial is already registered."}), 400


# ==============================================================================
# DEVICE RE-PAIRING (Account Settings -> "Pair New Device")
#
# Lets a user swap the hardware unit linked to their account -- e.g. they got
# a replacement device with a new serial. This is deliberately NOT a raw edit
# of device_serial: it unlinks whatever device the account currently has and
# links the new one, in a single transaction, so the account is never left
# pointing at a serial that doesn't exist or double-linked to two devices.
# ==============================================================================
DEVICE_SERIAL_PATTERN = re.compile(r'^[A-Z0-9][A-Z0-9\-]{2,31}$')

@app.route('/api/devices/repair', methods=['POST'])
@login_required
def api_repair_device():
    data = request.get_json() or {}
    new_serial = data.get('device_serial', '').strip().upper()

    if not new_serial:
        return jsonify({"error": "Please enter a device serial."}), 400

    if not DEVICE_SERIAL_PATTERN.match(new_serial):
        return jsonify({
            "error": "Device serial must be 3-32 characters, letters/numbers/dashes only (e.g. PAD-451107)."
        }), 400

    conn = get_db()
    try:
        cursor = conn.cursor()

        # What's currently linked to this account, if anything (for the
        # audit log and so the frontend can show what just changed).
        old_device = cursor.execute(
            "SELECT device_serial FROM devices WHERE user_id = ?",
            (session["user_id"],)
        ).fetchone()

        if old_device and old_device["device_serial"] == new_serial:
            conn.close()
            return jsonify({"error": "That's already your paired device."}), 400

        # Unlink the old unit and link the new one as a single transaction --
        # either both happen or neither does, so the account can't end up
        # with zero devices or two devices from a failure partway through.
        cursor.execute("DELETE FROM devices WHERE user_id = ?", (session["user_id"],))
        cursor.execute('''
            INSERT INTO devices (user_id, device_serial, device_type, status, battery_level, created_at)
            VALUES (?, ?, 'Assistive Device', 'Paired', ?, ?)
        ''', (session["user_id"], new_serial, random.randint(70, 100), now()))

        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({"error": "That device serial is already registered to another account."}), 400
    except Exception:
        conn.rollback()
        conn.close()
        raise
    else:
        conn.close()

    old_serial_label = old_device["device_serial"] if old_device else "(none)"
    log_audit(
        session["username"], session["role"], "Re-paired hardware unit",
        f"{old_serial_label} -> {new_serial}", "Web Interface"
    )

    return jsonify({"message": "Device re-paired successfully.", "device_serial": new_serial}), 200


# ==============================================================================
# CAREGIVERS
# ==============================================================================
@app.route('/api/caregivers', methods=['GET'])
@login_required
def api_get_caregivers():
    conn = get_db()
    rows = conn.execute("SELECT * FROM caregivers WHERE user_id = ? ORDER BY id", (session["user_id"],)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows]), 200


@app.route('/api/caregivers', methods=['POST'])
@login_required
def api_add_caregiver():
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    relation = data.get('relation', '').strip()
    phone = data.get('phone', '').strip()

    if not name or not phone:
        return jsonify({"error": "Name and phone number are required."}), 400

    conn = get_db()
    conn.execute('''
        INSERT INTO caregivers (user_id, name, relation, phone, status)
        VALUES (?, ?, ?, ?, 'active')
    ''', (session["user_id"], name, relation, phone))
    conn.commit()
    conn.close()
    log_audit(session["username"], session["role"], "Added caregiver contact", name, "Web Interface")
    return jsonify({"message": "Caregiver added"}), 201


@app.route('/api/caregivers/<int:cg_id>', methods=['DELETE'])
@login_required
def api_delete_caregiver(cg_id):
    conn = get_db()
    conn.execute("DELETE FROM caregivers WHERE id=? AND user_id=?", (cg_id, session["user_id"]))
    conn.commit()
    conn.close()
    log_audit(session["username"], session["role"], "Removed caregiver contact", f"Caregiver #{cg_id}", "Web Interface")
    return jsonify({"message": "Caregiver removed"}), 200


@app.route('/api/caregivers/alert', methods=['POST'])
@login_required
def api_alert_caregivers():
    data = request.get_json() or {}
    channels = [c for c in ["sms", "push", "voice"] if data.get(c)]

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO alerts (user_id, alert_type, channels, status, created_at)
        VALUES (?, 'caregiver', ?, 'open', ?)
    ''', (session["user_id"], ",".join(channels), now()))
    alert_id = cursor.lastrowid
    caregivers = cursor.execute("SELECT * FROM caregivers WHERE user_id=?", (session["user_id"],)).fetchall()
    conn.commit()
    conn.close()

    log_audit(session["username"], session["role"], "Broadcast alert to caregivers",
              f"Channels: {', '.join(channels) if channels else 'none'} — {len(caregivers)} contact(s)", "Alert Broadcast Module")

    return jsonify({
        "message": "Caregiver broadcast recorded",
        "alert_id": alert_id,
        "caregivers": [dict(c) for c in caregivers]
    }), 201


# ==============================================================================
# MESSAGES (user <-> admin two-way chat)
#
# One thread per user_id. Users always talk to "the admin team" as a whole;
# admins pick a specific user's thread from /api/messages/threads.
# ==============================================================================
@app.route('/api/messages/threads', methods=['GET'])
@admin_required
def api_message_threads():
    conn = get_db()
    rows = conn.execute('''
        SELECT
            u.id AS user_id,
            u.fullname,
            u.username,
            (SELECT body FROM messages m WHERE m.user_id = u.id ORDER BY m.id DESC LIMIT 1) AS last_body,
            (SELECT created_at FROM messages m WHERE m.user_id = u.id ORDER BY m.id DESC LIMIT 1) AS last_at,
            (SELECT COUNT(*) FROM messages m WHERE m.user_id = u.id AND m.sender_role = 'user' AND m.read_by_admin = 0) AS unread
        FROM users u
        WHERE u.role != 'admin'
        ORDER BY last_at IS NULL, last_at DESC
    ''').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows]), 200


@app.route('/api/messages', methods=['GET'])
@login_required
def api_get_messages():
    if session["role"] == "admin":
        target_user_id = request.args.get('user_id', type=int)
        if not target_user_id:
            return jsonify({"error": "user_id is required for admins"}), 400
    else:
        target_user_id = session["user_id"]

    conn = get_db()
    rows = conn.execute('''
        SELECT * FROM messages WHERE user_id=? ORDER BY id ASC
    ''', (target_user_id,)).fetchall()

    # Mark the thread as read for whichever side is viewing it right now
    if session["role"] == "admin":
        conn.execute("UPDATE messages SET read_by_admin=1 WHERE user_id=? AND read_by_admin=0", (target_user_id,))
    else:
        conn.execute("UPDATE messages SET read_by_user=1 WHERE user_id=? AND read_by_user=0", (target_user_id,))
    conn.commit()
    conn.close()
    return jsonify([dict(r) for r in rows]), 200


@app.route('/api/messages', methods=['POST'])
@login_required
def api_send_message():
    data = request.get_json() or {}
    body = data.get('body', '').strip()
    if not body:
        return jsonify({"error": "Message cannot be empty."}), 400

    if session["role"] == "admin":
        target_user_id = data.get('user_id')
        if not target_user_id:
            return jsonify({"error": "user_id is required for admins"}), 400
        read_by_user, read_by_admin = 0, 1
    else:
        target_user_id = session["user_id"]
        read_by_user, read_by_admin = 1, 0

    conn = get_db()
    conn.execute('''
        INSERT INTO messages (user_id, sender_role, sender_name, body, created_at, read_by_user, read_by_admin)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (target_user_id, session["role"], session["username"], body, now(), read_by_user, read_by_admin))
    conn.commit()
    conn.close()

    log_audit(session["username"], session["role"], "Sent message", body[:60], "Messages")
    return jsonify({"message": "Message sent"}), 201


@app.route('/api/messages/unread-count', methods=['GET'])
@login_required
def api_unread_count():
    conn = get_db()
    if session["role"] == "admin":
        count = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE sender_role='user' AND read_by_admin=0"
        ).fetchone()["c"]
    else:
        count = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE user_id=? AND sender_role='admin' AND read_by_user=0",
            (session["user_id"],)
        ).fetchone()["c"]
    conn.close()
    return jsonify({"unread": count}), 200


# ==============================================================================
# REMOTE DEVICE COMMANDS (admin -> ESP32 coordination)
#
# Admins queue a command here; the ESP32 drains its pending commands the
# next time it checks in via /api/iot/telemetry (see below), executes it
# locally (buzzer alarm / forced location refresh), and the location
# refresh path reports back through /api/iot/locate.
# ==============================================================================
@app.route('/api/iot/commands', methods=['POST'])
@admin_required
def api_queue_device_command():
    data = request.get_json() or {}
    device_serial = data.get('device_serial', '').strip()
    command = data.get('command', '').strip().lower()

    if command not in ('alarm', 'locate'):
        return jsonify({"error": "command must be 'alarm' or 'locate'"}), 400

    conn = get_db()
    device = conn.execute("SELECT id FROM devices WHERE device_serial=?", (device_serial,)).fetchone()
    if not device:
        conn.close()
        return jsonify({"error": "Unknown device_serial"}), 404

    conn.execute('''
        INSERT INTO device_commands (device_id, device_serial, command, status, requested_by, created_at)
        VALUES (?, ?, ?, 'pending', ?, ?)
    ''', (device["id"], device_serial, command, session["username"], now()))
    conn.commit()
    conn.close()

    log_audit(session["username"], "admin", f"Queued remote command: {command}", device_serial, "Device Management")
    return jsonify({"message": f"'{command}' command queued for {device_serial}. Delivered on next check-in."}), 201


@app.route('/api/iot/locate', methods=['POST'])
def api_iot_locate_response():
    """The ESP32 posts here after executing a remote 'locate' command. Unlike
    /api/trigger-hardware this does NOT flag an SOS -- it's just a location
    refresh requested by an admin, so it never touches sos_triggered/status."""
    data = request.get_json() or {}
    device_serial = data.get('device_serial')
    lat = data.get('lat')
    lng = data.get('lng')
    address = data.get('address')

    if not device_serial or lat is None or lng is None:
        return jsonify({"error": "device_serial, lat and lng are required"}), 400

    if (not address) and lat is not None and lng is not None:
        address = reverse_geocode(float(lat), float(lng))

    conn = get_db()
    device = conn.execute("SELECT id FROM devices WHERE device_serial=?", (device_serial,)).fetchone()
    if not device:
        conn.close()
        return jsonify({"error": "Unknown device_serial"}), 404

    conn.execute('''
        UPDATE devices SET last_lat=?, last_lng=?, last_address=?, last_activity=? WHERE id=?
    ''', (lat, lng, address, now(), device["id"]))
    conn.commit()
    conn.close()

    log_audit("Hardware Device", "device", "Reported location (remote locate command)", f"{device_serial} @ {address}", "ESP32 Hardware")
    return jsonify({"message": "Location refresh recorded"}), 200


# ==============================================================================
# ADMIN: USERS DIRECTORY
# ==============================================================================
@app.route('/api/users', methods=['GET'])
@admin_required
def api_list_users():
    conn = get_db()
    rows = conn.execute("SELECT id, username, fullname, address, contactnumber, role, status FROM users ORDER BY id").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows]), 200


@app.route('/api/users/<int:user_id>', methods=['PUT'])
@admin_required
def api_update_user(user_id):
    data = request.get_json() or {}
    conn = get_db()
    target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not target:
        conn.close()
        return jsonify({"error": "User not found"}), 404

    role = data.get('role', target['role'])
    status = data.get('status', target['status'])
    conn.execute("UPDATE users SET role=?, status=? WHERE id=?", (role, status, user_id))
    conn.commit()
    conn.close()
    log_audit(session["username"], "admin", "Updated user record", f"{target['fullname']} -> role={role}, status={status}", "User Profiles")
    return jsonify({"message": "User updated"}), 200


@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@admin_required
def api_delete_user(user_id):
    conn = get_db()
    target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if target and target["username"] == "admin":
        conn.close()
        return jsonify({"error": "The default administrator account cannot be deleted."}), 400
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    if target:
        log_audit(session["username"], "admin", "Removed user account", target["fullname"], "User Profiles")
    return jsonify({"message": "User removed"}), 200


# ==============================================================================
# ACCOUNT SETTINGS (self-service)
# ==============================================================================
@app.route('/api/account', methods=['GET'])
@login_required
def api_get_account():
    conn = get_db()
    row = conn.execute("SELECT id, username, fullname, address, contactnumber, role FROM users WHERE id=?", (session["user_id"],)).fetchone()
    conn.close()
    return jsonify(dict(row)), 200


@app.route('/api/account', methods=['PUT'])
@login_required
def api_update_account():
    data = request.get_json() or {}
    fullname = data.get('fullname', '').strip()
    contactnumber = data.get('contactnumber', '').strip()
    current_password = data.get('current_password')
    new_password = data.get('new_password')

    conn = get_db()
    cursor = conn.cursor()

    if fullname or contactnumber:
        row = cursor.execute("SELECT fullname, contactnumber FROM users WHERE id=?", (session["user_id"],)).fetchone()
        cursor.execute('''
            UPDATE users SET fullname=?, contactnumber=? WHERE id=?
        ''', (fullname or row["fullname"], contactnumber or row["contactnumber"], session["user_id"]))
        session["username"] = fullname or row["fullname"]

    if new_password:
        row = cursor.execute("SELECT password FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if not current_password or not check_password_hash(row["password"], current_password):
            conn.close()
            return jsonify({"error": "Current password is incorrect."}), 400
        cursor.execute("UPDATE users SET password=? WHERE id=?", (generate_password_hash(new_password), session["user_id"]))

    conn.commit()
    conn.close()
    log_audit(session["username"], session["role"], "Updated account profile", "", "Account Settings")
    return jsonify({"message": "Account updated", "name": session["username"]}), 200


# ==============================================================================
# AUDIT TRAIL (admin view)
# ==============================================================================
@app.route('/api/audit', methods=['GET'])
@admin_required
def api_audit():
    conn = get_db()
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows]), 200


# ==============================================================================
# ADMIN DASHBOARD SUMMARY
# ==============================================================================
@app.route('/api/dashboard/summary', methods=['GET'])
@admin_required
def api_dashboard_summary():
    conn = get_db()
    total_users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    total_devices = conn.execute("SELECT COUNT(*) c FROM devices").fetchone()["c"]
    open_alerts = conn.execute("SELECT COUNT(*) c FROM alerts WHERE status='open'").fetchone()["c"]
    recent_alerts = conn.execute('''
        SELECT alerts.*, users.fullname FROM alerts
        JOIN users ON users.id = alerts.user_id
        ORDER BY alerts.id DESC LIMIT 5
    ''').fetchall()
    recent_audit = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 6").fetchall()
    devices = conn.execute('''
        SELECT devices.*, users.fullname FROM devices
        JOIN users ON users.id = devices.user_id
        ORDER BY devices.id DESC LIMIT 6
    ''').fetchall()
    conn.close()

    return jsonify({
        "total_users": total_users,
        "total_devices": total_devices,
        "open_alerts": open_alerts,
        "recent_alerts": [dict(r) for r in recent_alerts],
        "recent_audit": [dict(r) for r in recent_audit],
        "devices": [dict(r) for r in devices],
    }), 200


# ==============================================================================
# SUPPORT TICKETS
# ==============================================================================
@app.route('/api/support/tickets', methods=['GET'])
@login_required
def api_get_tickets():
    conn = get_db()
    if session["role"] == "admin":
        rows = conn.execute('''
            SELECT support_tickets.*, users.fullname FROM support_tickets
            JOIN users ON users.id = support_tickets.user_id
            ORDER BY support_tickets.id DESC
        ''').fetchall()
    else:
        rows = conn.execute('''
            SELECT support_tickets.*, users.fullname FROM support_tickets
            JOIN users ON users.id = support_tickets.user_id
            WHERE user_id=? ORDER BY support_tickets.id DESC
        ''', (session["user_id"],)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows]), 200


@app.route('/api/support/tickets', methods=['POST'])
@login_required
def api_create_ticket():
    data = request.get_json() or {}
    category = data.get('category', 'General').strip()
    description = data.get('description', '').strip()
    if not description:
        return jsonify({"error": "Please describe the issue before submitting."}), 400

    conn = get_db()
    conn.execute('''
        INSERT INTO support_tickets (user_id, category, description, severity, status, created_at)
        VALUES (?, ?, ?, 'Medium', 'open', ?)
    ''', (session["user_id"], category, description, now()))
    conn.commit()
    conn.close()
    log_audit(session["username"], session["role"], "Submitted support ticket", category, "Support Portal")
    return jsonify({"message": "Ticket submitted"}), 201


@app.route('/api/support/tickets/<int:ticket_id>/resolve', methods=['POST'])
@admin_required
def api_resolve_ticket(ticket_id):
    conn = get_db()
    conn.execute("UPDATE support_tickets SET status='resolved', resolved_at=? WHERE id=?", (now(), ticket_id))
    conn.commit()
    conn.close()
    log_audit(session["username"], "admin", "Resolved support ticket", f"Ticket #{ticket_id}", "Support Portal")
    return jsonify({"message": "Ticket resolved"}), 200


# ==============================================================================
# LIVE SERVER PING (real round-trip check instead of a simulated one)
# ==============================================================================
@app.route('/api/ping', methods=['GET'])
def api_ping():
    return jsonify({"status": "online", "server_time": now()}), 200

# ==============================================================================
# TELEMETRY ENDPOINT
#
# This is where the ESP32 checks in routinely (heartbeat). It intentionally
# does NOT write last_lat/last_lng anymore -- GNSS coordinates are only ever
# recorded when the physical SOS button is pressed (see /api/trigger-hardware
# above). This keeps continuous hardware activity from silently populating
# a live position on the tracking console.
# ==============================================================================
@app.route('/api/iot/telemetry', methods=['POST'])
def api_iot_telemetry():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data received"}), 400

    device_serial = data.get('device_serial')

    conn = get_db()
    cursor = conn.cursor()

    # Check if the device exists in the database
    cursor.execute("SELECT id FROM devices WHERE device_serial = ?", (device_serial,))
    device = cursor.fetchone()

    if not device:
        conn.close()
        return jsonify({"error": "Unknown device_serial. Pair this device from the dashboard first."}), 404

    # Only refresh the heartbeat timestamp -- no GNSS fields are touched here
    cursor.execute('''
        UPDATE devices
        SET last_activity = ?
        WHERE device_serial = ?
    ''', (now(), device_serial))

    # ------------------------------------------------------------------
    # ADMIN -> DEVICE COORDINATION: piggy-back on this routine heartbeat
    # to hand the ESP32 (a) a one-time "help is on the way" acknowledgement
    # for its own open alert, and (b) any pending remote commands. This
    # keeps the device a simple polling client -- no extra endpoint needed.
    # ------------------------------------------------------------------
    ack = False
    ack_message = ""
    cursor.execute('''
        SELECT id, acknowledged_by FROM alerts
        WHERE user_id = (SELECT user_id FROM devices WHERE id = ?)
          AND status = 'open' AND acknowledged = 1 AND device_notified = 0
        ORDER BY id DESC LIMIT 1
    ''', (device["id"],))
    pending_ack = cursor.fetchone()
    if pending_ack:
        ack = True
        ack_message = f"Help is on the way. {pending_ack['acknowledged_by']} is responding."
        cursor.execute("UPDATE alerts SET device_notified=1 WHERE id=?", (pending_ack["id"],))

    cursor.execute('''
        SELECT id, command FROM device_commands
        WHERE device_id = ? AND status = 'pending'
        ORDER BY id ASC
    ''', (device["id"],))
    pending_commands = cursor.fetchall()
    commands = [row["command"] for row in pending_commands]
    if pending_commands:
        cursor.execute('''
            UPDATE device_commands SET status='delivered', delivered_at=?
            WHERE device_id=? AND status='pending'
        ''', (now(), device["id"]))

    conn.commit()
    conn.close()

    print(f"✅ Heartbeat received from {device_serial} (ack={ack}, commands={commands})")
    return jsonify({
        "status": "success",
        "message": "Heartbeat recorded",
        "ack": ack,
        "ack_message": ack_message,
        "commands": commands
    }), 200


# ==============================================================================
# ONE-TIME BACKFILL
#
# Runs once at startup. Any device row whose last_address is still a raw
# GNSS fix string (stored before the geocoding logic above existed) gets
# converted to a proper place name here, so old data displays correctly
# without needing to re-trigger the physical button.
# ==============================================================================
def backfill_raw_gnss_addresses():
    conn = get_db()
    rows = conn.execute("SELECT id, last_lat, last_lng, last_address FROM devices").fetchall()
    for row in rows:
        if row["last_address"] and parse_gnss_string(row["last_address"]) and row["last_lat"] is not None and row["last_lng"] is not None:
            fixed_address = reverse_geocode(row["last_lat"], row["last_lng"])
            conn.execute("UPDATE devices SET last_address=? WHERE id=?", (fixed_address, row["id"]))
            print(f"🔧 Backfilled device #{row['id']} address -> {fixed_address}")
    conn.commit()
    conn.close()


if __name__ == '__main__':
    init_db()
    backfill_raw_gnss_addresses()
    # Adding host='0.0.0.0' allows external devices (like your ESP32) to connect
    app.run(host='0.0.0.0', port=5000, debug=True)