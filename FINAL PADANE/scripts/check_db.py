import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "instance", "database.db")
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Get the schema information for the 'devices' table
cursor.execute("PRAGMA table_info(devices)")
columns = cursor.fetchall()

print("--- Database Detective Report ---")
if not columns:
    print("Could not find a table named 'devices'. Check your table name!")
else:
    print("Columns in the 'devices' table:")
    for col in columns:
        print(f" -> Column Name: '{col[1]}'")  # col[1] is the name

conn.close()
