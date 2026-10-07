# storage.py
from __future__ import annotations

import os
import sqlite3
import logging
import time
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Optional, Tuple, List, Iterable, Set, Iterator

logger = logging.getLogger("commit-quiz-bot")

DB_PATH = os.environ.get("DB_PATH", "quiz_scores.db")


# ----------------------------- Low-level utils ------------------------------

def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """
    Apply pragmatic settings once per connection.
    WAL + NORMAL = good durability/perf trade-off for small bots.
    """
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    except sqlite3.DatabaseError:
        # If DB is corrupt, these may fail; handled by integrity check.
        pass

def _connect() -> sqlite3.Connection:
    # check_same_thread=False allows use from PTB JobQueue/background tasks safely
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn

def _integrity_ok() -> bool:
    """
    Returns True if the DB file exists and PRAGMA integrity_check reports 'ok'.
    Uses read-only mode (won't create/modify the file).
    """
    if not os.path.exists(DB_PATH):
        return False
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
        try:
            row = con.execute("PRAGMA integrity_check;").fetchone()
            return bool(row) and str(row[0]).lower() == "ok"
        finally:
            con.close()
    except Exception:
        return False

def _try_rotate_or_remove() -> bool:
    """
    On Windows, files can be locked by other processes. Try to rotate (rename) the corrupt DB
    a few times with small backoff. If that fails, try to remove it. Return True if the file
    was handled (rotated/removed), False if still locked.
    """
    corrupt_path = DB_PATH + ".corrupt"
    # Try rotation with retries
    for i in range(5):
        try:
            if os.path.exists(corrupt_path):
                os.remove(corrupt_path)
            os.replace(DB_PATH, corrupt_path)
            logger.warning("Corrupt DB rotated to %s; a fresh DB will be created.", corrupt_path)
            return True
        except PermissionError:
            # File is locked by another process. Back off and retry.
            time.sleep(0.4 * (i + 1))
        except FileNotFoundError:
            # Already gone
            return True
        except Exception as e:
            logger.debug("Rotate attempt %d failed: %s", i + 1, e)

    # Fallback: try delete with retries
    for i in range(5):
        try:
            if os.path.exists(DB_PATH):
                os.remove(DB_PATH)
                logger.warning("Corrupt DB removed; a fresh DB will be created.")
            return True
        except PermissionError:
            time.sleep(0.4 * (i + 1))
        except Exception as e:
            logger.debug("Remove attempt %d failed: %s", i + 1, e)

    return False  # still locked


@contextmanager
def _db() -> sqlite3.Connection:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _table_columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    rows = conn.execute(f"PRAGMA table_info({table});").fetchall()
    return {r["name"] for r in rows}

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (table,)
    ).fetchone()
    return bool(row)

def _ensure_table_with_schema(conn: sqlite3.Connection, table: str, required_cols: Iterable[str], create_sql: str) -> None:
    if not _table_exists(conn, table):
        conn.execute(create_sql)
        logger.info("Created table %s", table)
        return

    have = _table_columns(conn, table)
    need = set(required_cols)
    if not need.issubset(have):
        logger.warning("Rebuilding table %s due to missing columns: %s", table, ", ".join(sorted(need - have)))
        conn.execute(f"DROP TABLE IF EXISTS {table};")
        conn.execute(create_sql)
        logger.info("Recreated table %s", table)


# ----------------------------- Schema DDL -----------------------------------

RESULTS_SQL = """
CREATE TABLE IF NOT EXISTS results (
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    ts          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    correct     INTEGER NOT NULL CHECK (correct IN (0,1))
);
"""

DAILY_SQL = """
CREATE TABLE IF NOT EXISTS daily_progress (
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    day         TEXT    NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id, day)
);
"""

STREAKS_SQL = """
CREATE TABLE IF NOT EXISTS streaks (
    chat_id         INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    current_streak  INTEGER NOT NULL DEFAULT 0,
    best_streak     INTEGER NOT NULL DEFAULT 0,
    last_day        TEXT,
    PRIMARY KEY (chat_id, user_id)
);
"""

PREFS_SQL = """
CREATE TABLE IF NOT EXISTS user_prefs (
    chat_id       INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    notify_hour   INTEGER,
    notify_minute INTEGER,
    tz            TEXT,
    PRIMARY KEY (chat_id, user_id)
);
"""

NAMES_SQL = """
CREATE TABLE IF NOT EXISTS user_names (
    chat_id       INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    display_name  TEXT,
    PRIMARY KEY (chat_id, user_id)
);
"""

def _create_or_migrate_schema(conn: sqlite3.Connection) -> None:
    _ensure_table_with_schema(conn, "results",
        required_cols=("chat_id","user_id","ts","correct"),
        create_sql=RESULTS_SQL,
    )
    _ensure_table_with_schema(conn, "daily_progress",
        required_cols=("chat_id","user_id","day","count"),
        create_sql=DAILY_SQL,
    )
    _ensure_table_with_schema(conn, "streaks",
        required_cols=("chat_id","user_id","current_streak","best_streak","last_day"),
        create_sql=STREAKS_SQL,
    )
    _ensure_table_with_schema(conn, "user_prefs",
        required_cols=("chat_id","user_id","notify_hour","notify_minute","tz"),
        create_sql=PREFS_SQL,
    )
    _ensure_table_with_schema(conn, "user_names",
        required_cols=("chat_id","user_id","display_name"),
        create_sql=NAMES_SQL,
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_results_user ON results(chat_id, user_id, ts);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_user ON daily_progress(chat_id, user_id, day);")


def init_db() -> None:
    """
    Ensure the DB exists and is valid. If it's corrupt, rotate/remove with retries and rebuild.
    NOTE (Windows): if the DB is locked by another process (editor/viewer), we will retry.
    If still locked, we log a clear error so you can close the locker.
    """
    # If file exists but is corrupt → rotate/remove with retries
    if os.path.exists(DB_PATH) and not _integrity_ok():
        if not _try_rotate_or_remove():
            logger.error(
                "The database file %s is locked by another process. "
                "Close any app using it (VS Code preview/DB Browser/another Python), then run again.",
                DB_PATH,
            )
            raise sqlite3.DatabaseError("quiz_scores.db locked by another process")

    # Create/connect and ensure schema
    conn = _connect()
    try:
        _create_or_migrate_schema(conn)
        conn.commit()
    finally:
        conn.close()


# ----------------------------- Quiz results ---------------------------------

def record_result(chat_id: int, user_id: int, is_correct: bool) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO results (chat_id, user_id, correct) VALUES (?,?,?)",
            (chat_id, user_id, 1 if is_correct else 0),
        )

def get_score(chat_id: int, user_id: int) -> Tuple[int, int]:
    with _db() as conn:
        row = conn.execute(
            "SELECT SUM(correct) AS correct, COUNT(*) AS total "
            "FROM results WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        correct = int(row["correct"] or 0)
        total = int(row["total"] or 0)
        return correct, total


# ----------------------------- Daily progress -------------------------------

def get_daily_count(chat_id: int, user_id: int, day: str) -> int:
    with _db() as conn:
        row = conn.execute(
            "SELECT count FROM daily_progress WHERE chat_id=? AND user_id=? AND day=?",
            (chat_id, user_id, day),
        ).fetchone()
        return int(row["count"]) if row else 0

def inc_daily_count(chat_id: int, user_id: int, day: str) -> int:
    with _db() as conn:
        conn.execute(
            "INSERT INTO daily_progress (chat_id, user_id, day, count) "
            "VALUES (?,?,?,1) "
            "ON CONFLICT(chat_id, user_id, day) DO UPDATE SET count = count + 1",
            (chat_id, user_id, day),
        )
        row = conn.execute(
            "SELECT count FROM daily_progress WHERE chat_id=? AND user_id=? AND day=?",
            (chat_id, user_id, day),
        ).fetchone()
        return int(row["count"]) if row else 0


# ----------------------------- Streaks --------------------------------------

def _iso_to_date(s: str) -> date:
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))

def mark_day_complete(chat_id: int, user_id: int, day: str) -> Tuple[int, int, str]:
    with _db() as conn:
        row = conn.execute(
            "SELECT current_streak, best_streak, last_day FROM streaks WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()

        if row:
            current = int(row["current_streak"])
            best = int(row["best_streak"])
            last = row["last_day"]
        else:
            current, best, last = 0, 0, None

        if last == day:
            return current, best, last

        today_d = _iso_to_date(day)
        yesterday = (today_d - timedelta(days=1)).isoformat() if today_d else None

        if last and last == yesterday:
            current += 1
        else:
            current = 1

        best = max(best, current)

        conn.execute(
            "INSERT INTO streaks (chat_id, user_id, current_streak, best_streak, last_day) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
            "  current_streak=excluded.current_streak, "
            "  best_streak=excluded.best_streak, "
            "  last_day=excluded.last_day",
            (chat_id, user_id, current, best, day),
        )
        return current, best, day

def get_streak(chat_id: int, user_id: int) -> Tuple[int, int, Optional[str]]:
    with _db() as conn:
        row = conn.execute(
            "SELECT current_streak, best_streak, last_day FROM streaks WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        if not row:
            return 0, 0, None
        return int(row["current_streak"]), int(row["best_streak"]), row["last_day"]


# ----------------------------- Names & prefs --------------------------------

def set_notify_time(chat_id: int, user_id: int, hour: int, minute: int, tz: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO user_prefs (chat_id, user_id, notify_hour, notify_minute, tz) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
            "  notify_hour=excluded.notify_hour, "
            "  notify_minute=excluded.notify_minute, "
            "  tz=excluded.tz",
            (chat_id, user_id, hour, minute, tz),
        )

def get_notify_time(chat_id: int, user_id: int) -> Optional[Tuple[int, int, str]]:
    with _db() as conn:
        row = conn.execute(
            "SELECT notify_hour, notify_minute, tz FROM user_prefs WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        if not row:
            return None
        return int(row["notify_hour"]), int(row["notify_minute"]), row["tz"]

def set_user_name(chat_id: int, user_id: int, display_name: str) -> None:
    if not display_name:
        return
    with _db() as conn:
        conn.execute(
            "INSERT INTO user_names (chat_id, user_id, display_name) VALUES (?,?,?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET display_name=excluded.display_name",
            (chat_id, user_id, display_name),
        )

def get_top_streaks(chat_id: int, limit: int = 10) -> List[Tuple[int, int, int, str]]:
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT s.user_id, s.current_streak, s.best_streak,
                   COALESCE(n.display_name, printf('User %d', s.user_id)) AS display_name
            FROM streaks s
            LEFT JOIN user_names n
              ON n.chat_id = ? AND n.user_id = s.user_id
            WHERE s.chat_id = ?
            ORDER BY s.current_streak DESC, s.best_streak DESC, s.user_id ASC
            LIMIT ?
            """,
            (chat_id, chat_id, limit),
        ).fetchall()
        return [(int(r["user_id"]), int(r["current_streak"]), int(r["best_streak"]), r["display_name"]) for r in rows]


# ----------------------------- NEW: Notify prefs iterator -------------------

def iter_all_notify_prefs() -> Iterator[Tuple[int, int, int, int, str]]:
    """
    Yield (chat_id, user_id, hour, minute, tz) for every user with a saved reminder.
    """
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT chat_id, user_id, notify_hour, notify_minute, tz
            FROM user_prefs
            WHERE notify_hour IS NOT NULL
              AND notify_minute IS NOT NULL
              AND tz IS NOT NULL
            """
        ).fetchall()
        for r in rows:
            yield (
                int(r["chat_id"]),
                int(r["user_id"]),
                int(r["notify_hour"]),
                int(r["notify_minute"]),
                r["tz"],
            )


def clear_notify_time(chat_id: int, user_id: int) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM user_prefs WHERE chat_id=? AND user_id=?", (chat_id, user_id))
