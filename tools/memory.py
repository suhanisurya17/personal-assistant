import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class MemoryStore:
    def __init__(self, database_path: str = "assistant.db") -> None:
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database_path)
        self.connection.execute("CREATE TABLE IF NOT EXISTS memories (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
        self.connection.commit()

    def set(self, key: str, value: str) -> None:
        self.connection.execute("INSERT OR REPLACE INTO memories VALUES (?, ?, ?)",
                                (key, value, datetime.now(timezone.utc).isoformat()))
        self.connection.commit()

    def get(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM memories WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self.connection.close()
