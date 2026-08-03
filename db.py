import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

DB_PATH = "audit_assistant.db"

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS validation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gstin TEXT, invoice_number TEXT, overall_severity TEXT,
                is_valid INTEGER, transaction_type TEXT, flag_count INTEGER,
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS match_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                total INTEGER, matched INTEGER, mismatched INTEGER,
                missing_in_gstr2b INTEGER, source TEXT, created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE, password_hash TEXT,
                is_paid INTEGER DEFAULT 0, trial_used INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT
            )
        """)

def create_user(email: str, password_hash: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (email, password_hash, is_paid, trial_used, created_at) VALUES (?,?,0,0,?)",
            (email.lower().strip(), password_hash, datetime.utcnow().isoformat())
        )

def get_user_by_email(email: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),)).fetchone()
        return dict(row) if row else None

def get_user_by_token(token: str):
    with get_conn() as conn:
        row = conn.execute("""
            SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?
        """, (token,)).fetchone()
        return dict(row) if row else None

def create_session(token: str, user_id: int):
    with get_conn() as conn:
        conn.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?,?,?)",
                      (token, user_id, datetime.utcnow().isoformat()))

def increment_trial_used(user_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE users SET trial_used = trial_used + 1 WHERE id = ?", (user_id,))

def set_user_paid(email: str, is_paid: bool = True):
    with get_conn() as conn:
        cur = conn.execute("UPDATE users SET is_paid = ? WHERE email = ?", (int(is_paid), email.lower().strip()))
        return cur.rowcount > 0

def log_validation(gstin: str, invoice_number: Optional[str], severity: str,
                    is_valid: bool, tx_type: str, flag_count: int):
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO validation_logs (gstin, invoice_number, overall_severity, is_valid, transaction_type, flag_count, created_at) VALUES (?,?,?,?,?,?,?)",
                (gstin, invoice_number, severity, int(is_valid), tx_type, flag_count, datetime.utcnow().isoformat())
            )
    except Exception as e:
        print(f"[db] validation log failed: {e}")

def log_match_summary(total: int, matched: int, mismatched: int, missing: int, source: str):
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO match_summaries (total, matched, mismatched, missing_in_gstr2b, source, created_at) VALUES (?,?,?,?,?,?)",
                (total, matched, mismatched, missing, source, datetime.utcnow().isoformat())
            )
    except Exception as e:
        print(f"[db] match summary log failed: {e}")

def get_recent_validations(limit: int = 20):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM validation_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

def get_recent_matches(limit: int = 20):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM match_summaries ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

def _date_where(date_from, date_to, clauses, params):
    if date_from:
        clauses.append("created_at >= ?"); params.append(date_from)
    if date_to:
        clauses.append("created_at <= ?"); params.append(date_to + "T23:59:59")

def query_validations(limit=20, offset=0, severity=None, date_from=None, date_to=None, search=None):
    clauses, params = [], []
    if severity: clauses.append("overall_severity = ?"); params.append(severity)
    if search:
        clauses.append("(gstin LIKE ? OR invoice_number LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    _date_where(date_from, date_to, clauses, params)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM validation_logs {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM validation_logs {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset]
        ).fetchall()
        return total, [dict(r) for r in rows]

def query_matches(limit=20, offset=0, date_from=None, date_to=None):
    clauses, params = [], []
    _date_where(date_from, date_to, clauses, params)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM match_summaries {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM match_summaries {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset]
        ).fetchall()
        return total, [dict(r) for r in rows]
