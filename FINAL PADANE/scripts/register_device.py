import os
import sqlite3

# Connect to the app's database
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "instance", "database.db")
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

try:
    cursor.execute(
        """
        INSERT INTO devices (device_serial, status, created_at)
        VALUES (?, ?, ?)
        """,
        ("PAD-123456", "active", "2026-07-07 11:00:00"),
    )
    conn.commit()
    print("✅ Success! Device PAD-123456 has been registered.")
except sqlite3.IntegrityError:
    print("ℹ️ Device PAD-123456 is already in the database.")
finally:
    conn.close()
