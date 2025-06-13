import sqlite3
from datetime import datetime

DB_PATH = "fitness.db"

class DatabaseManager:
    """Handles SQLite schema and CRUD for activities & heart rate."""
    def __init__(self, path: str = DB_PATH):
        self.conn = sqlite3.connect(
            path,
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
        )
        self._ensure_schema()

    def _ensure_schema(self):
        c = self.conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS activities (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          start_time   TIMESTAMP NOT NULL,
          end_time     TIMESTAMP
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS heart_rate (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          activity_id  INTEGER NOT NULL REFERENCES activities(id),
          timestamp_ns INTEGER NOT NULL,
          bpm          INTEGER NOT NULL,
          rr_interval  REAL,
          energy_kj    REAL
        );
        """)
        self.conn.commit()

    def start_activity(self) -> int:
        now = datetime.utcnow()
        cur = self.conn.cursor()
        cur.execute("INSERT INTO activities (start_time) VALUES (?)", (now,))
        self.conn.commit()
        return cur.lastrowid

    def stop_activity(self, activity_id: int):
        now = datetime.utcnow()
        self.conn.execute(
            "UPDATE activities SET end_time = ? WHERE id = ?",
            (now, activity_id)
        )
        self.conn.commit()

    def insert_heart_rate(
        self,
        activity_id: int,
        timestamp_ns: int,
        bpm: int,
        rr: float | None,
        energy: float | None,
    ):
        self.conn.execute(
            """
            INSERT INTO heart_rate
              (activity_id, timestamp_ns, bpm, rr_interval, energy_kj)
            VALUES (?, ?, ?, ?, ?)
            """,
            (activity_id, timestamp_ns, bpm, rr, energy),
        )
        # commit can be batched by caller

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()
