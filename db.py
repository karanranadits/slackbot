"""
db.py — PostgreSQL persistence layer for the DSR Slack Bot.

Tables:
  users        — registered Slack users with their portal credentials
  chat_history — per-user conversation history (capped at 50 messages)
  bot_config   — key-value store for bot settings
"""

import os
import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.pool
import psycopg2.extras

log = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]  # required, set in .env

# Maximum messages to retain per user in chat history
HISTORY_CAP = 50


class UserDB:
    """Thread-safe PostgreSQL wrapper for user registry and chat history."""

    def __init__(self, database_url: str = DATABASE_URL):
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=database_url,
            connect_timeout=10,
        )
        self._init_db()
        log.info("Database initialised (PostgreSQL)")

    # ── Internal helpers ───────────────────────────────────────────────────

    @contextmanager
    def _conn(self):
        """Yield a connection from the pool with auto-commit on success."""
        conn = self._pool.getconn()
        conn.autocommit = False
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def _init_db(self):
        """Create tables if they don't already exist."""
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    slack_user_id   TEXT PRIMARY KEY,
                    display_name    TEXT NOT NULL,
                    portal_email    TEXT NOT NULL,
                    portal_password TEXT NOT NULL,
                    registered_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_history (
                    id            BIGSERIAL PRIMARY KEY,
                    slack_user_id TEXT NOT NULL,
                    role          TEXT NOT NULL,
                    content       TEXT NOT NULL,
                    timestamp     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(slack_user_id) REFERENCES users(slack_user_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_config (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            # Index to speed up per-user history queries
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_chat_history_user
                ON chat_history(slack_user_id, id)
            """)

    # ── User registry ──────────────────────────────────────────────────────

    def is_registered(self, slack_user_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM users WHERE slack_user_id = %s",
                (slack_user_id,)
            )
            return cur.fetchone() is not None

    def register_user(
        self,
        slack_user_id: str,
        display_name: str,
        portal_email: str,
        portal_password: str,
    ) -> None:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO users (slack_user_id, display_name, portal_email, portal_password)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (slack_user_id) DO UPDATE SET
                    display_name    = EXCLUDED.display_name,
                    portal_email    = EXCLUDED.portal_email,
                    portal_password = EXCLUDED.portal_password
                """,
                (slack_user_id, display_name, portal_email, portal_password),
            )
        log.info(f"User registered/updated: {slack_user_id} ({display_name})")

    def get_user(self, slack_user_id: str) -> dict | None:
        with self._conn() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                "SELECT * FROM users WHERE slack_user_id = %s",
                (slack_user_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_all_users(self) -> list[dict]:
        with self._conn() as conn:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM users")
            return [dict(r) for r in cur.fetchall()]

    # ── Chat history ───────────────────────────────────────────────────────

    def save_message(self, slack_user_id: str, role: str, content: str) -> None:
        """
        Persist a single message and prune oldest so cap stays at HISTORY_CAP.
        role: 'human' | 'ai' | 'system'
        """
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO chat_history (slack_user_id, role, content) VALUES (%s, %s, %s)",
                (slack_user_id, role, content),
            )
            # Prune: delete oldest rows beyond HISTORY_CAP for this user
            cur.execute(
                """
                DELETE FROM chat_history
                WHERE slack_user_id = %s
                  AND id NOT IN (
                      SELECT id FROM chat_history
                      WHERE slack_user_id = %s
                      ORDER BY id DESC
                      LIMIT %s
                  )
                """,
                (slack_user_id, slack_user_id, HISTORY_CAP),
            )

    def get_history(self, slack_user_id: str) -> list[tuple[str, str]]:
        """Return [(role, content), ...] ordered oldest → newest."""
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT role, content FROM chat_history WHERE slack_user_id = %s ORDER BY id ASC",
                (slack_user_id,)
            )
            return [(r[0], r[1]) for r in cur.fetchall()]

    def clear_history(self, slack_user_id: str) -> None:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM chat_history WHERE slack_user_id = %s",
                (slack_user_id,)
            )
        log.info(f"Cleared chat history for {slack_user_id}")

    def get_history_count(self, slack_user_id: str) -> int:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM chat_history WHERE slack_user_id = %s",
                (slack_user_id,)
            )
            return cur.fetchone()[0]

    # ── Bot config ─────────────────────────────────────────────────────────

    def set_config(self, key: str, value: str) -> None:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO bot_config (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                (key, value)
            )

    def get_config(self, key: str, default: str = None) -> str | None:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM bot_config WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else default
