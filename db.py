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
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                trial_used INTEGER DEFAULT 0, is_paid INTEGER DEFAULT 0,
                paid_until TEXT,
                created_at TEXT
            )
        """)
        try:
            conn.execute("ALTER TABLE devices ADD COLUMN paid_until TEXT")
        except Exception:
            pass  # column already exists
        try:
            conn.execute("ALTER TABLE access_codes ADD COLUMN duration_days INTEGER DEFAULT 30")
        except Exception:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ip_trials (
                ip TEXT PRIMARY KEY,
                trial_used INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS access_codes (
                code TEXT PRIMARY KEY,
                used INTEGER DEFAULT 0, used_by_device TEXT,
                duration_days INTEGER DEFAULT 30,
                created_at TEXT, used_at TEXT
            )
        """)

def get_or_create_device(device_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        if row:
            d = dict(row)
            # auto-expire: subscription lapsed but is_paid flag still 1
            if d["is_paid"] and d.get("paid_until"):
                if datetime.utcnow().isoformat() > d["paid_until"]:
                    conn.execute("UPDATE devices SET is_paid = 0 WHERE device_id = ?", (device_id,))
                    d["is_paid"] = 0
            return d
        conn.execute("INSERT INTO devices (device_id, trial_used, is_paid, paid_until, created_at) VALUES (?,0,0,NULL,?)",
                      (device_id, datetime.utcnow().isoformat()))
        return {"device_id": device_id, "trial_used": 0, "is_paid": 0, "paid_until": None}

def increment_device_trial(device_id: str):
    with get_conn() as conn:
        conn.execute("UPDATE devices SET trial_used = trial_used + 1 WHERE device_id = ?", (device_id,))

def get_ip_trial_count(ip: str) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT trial_used FROM ip_trials WHERE ip = ?", (ip,)).fetchone()
        return row["trial_used"] if row else 0

def increment_ip_trial(ip: str):
    with get_conn() as conn:
        row = conn.execute("SELECT ip FROM ip_trials WHERE ip = ?", (ip,)).fetchone()
        if row:
            conn.execute("UPDATE ip_trials SET trial_used = trial_used + 1 WHERE ip = ?", (ip,))
        else:
            conn.execute("INSERT INTO ip_trials (ip, trial_used, created_at) VALUES (?,1,?)",
                          (ip, datetime.utcnow().isoformat()))

def redeem_access_code(device_id: str, code: str) -> bool:
    from datetime import timedelta
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM access_codes WHERE code = ?", (code.strip().upper(),)).fetchone()
        if not row or row["used"]:
            return False
        duration_days = row["duration_days"] or 30
        conn.execute("UPDATE access_codes SET used = 1, used_by_device = ?, used_at = ? WHERE code = ?",
                      (device_id, datetime.utcnow().isoformat(), code.strip().upper()))
        # extend from existing paid_until if still active, else from now
        existing = conn.execute("SELECT paid_until, is_paid FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        base = datetime.utcnow()
        if existing and existing["is_paid"] and existing["paid_until"] and existing["paid_until"] > base.isoformat():
            base = datetime.fromisoformat(existing["paid_until"])
        new_until = (base + timedelta(days=duration_days)).isoformat()
        conn.execute("UPDATE devices SET is_paid = 1, paid_until = ? WHERE device_id = ?", (new_until, device_id))
        return True

def generate_access_code(code: str, duration_days: int = 30):
    with get_conn() as conn:
        conn.execute("INSERT INTO access_codes (code, used, duration_days, created_at) VALUES (?,0,?,?)",
                      (code.strip().upper(), duration_days, datetime.utcnow().isoformat()))

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
