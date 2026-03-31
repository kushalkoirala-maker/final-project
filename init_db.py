import sqlite3
import os

# Path to your database file
DB_PATH = os.path.join("instance", "netops.db")
if not os.path.exists(DB_PATH):
    # Try the alternate path if instance folder isn't used
    DB_PATH = "netops.db"

def inject_degraded_column():
    print(f"Connecting to database at: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        # Check if the column already exists to avoid errors
        cursor.execute("PRAGMA table_info(device)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'degraded_status' not in columns:
            print("Injecting 'degraded_status' column into 'device' table...")
            # We add it as an integer (0 or 1) mirroring is_up
            cursor.execute("ALTER TABLE device ADD COLUMN degraded_status BOOLEAN DEFAULT 0")
            conn.commit()
            print("Success: Column injected.")
        else:
            print("Notice: 'degraded_status' column already exists.")

    except Exception as e:
        print(f"Error during injection: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    inject_degraded_column()