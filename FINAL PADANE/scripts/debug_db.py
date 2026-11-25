import os
import sqlite3

# This is the exact same path/logic app.py uses
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "instance", "database.db")
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Get the list of all serials in the table
cursor.execute("SELECT device_serial FROM devices")
results = cursor.fetchall()

print(f"--- DATABASE DEBUG: Found {len(results)} devices ---")
for row in results:
    # Print the serial and its length to catch hidden spaces
    serial = row[0]
    print(f"Found Serial: '{serial}' (Length: {len(serial)})")

conn.close()
