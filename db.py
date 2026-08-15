import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional
import psycopg2
import psycopg2.extras
import psycopg2.pool

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool = None
if DATABASE_URL:
    _pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL, sslmode="require")

@contextmanager
def get_conn():
    if not _pool:
        raise RuntimeError("DATABASE_URL env var not set — cannot connect to database.")
    conn = _pool.getconn()
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)

def _dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

def init_db():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS validation_logs (
                id SERIAL PRIMARY KEY,
                device_id TEXT,
                gstin TEXT, invoice_number TEXT, overall_severity TEXT,
                is_valid INTEGER, transaction_type TEXT, flag_count INTEGER,
                created_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS match_summaries (
                id SERIAL PRIMARY KEY,
                device_id TEXT,
                total INTEGER, matched INTEGER, mismatched INTEGER,
                missing_in_gstr2b INTEGER, source TEXT, created_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                trial_used INTEGER DEFAULT 0, is_paid INTEGER DEFAULT 0,
                paid_until TEXT,
                created_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ip_trials (
                ip TEXT PRIMARY KEY,
                trial_used INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS access_codes (
                code TEXT PRIMARY KEY,
                used INTEGER DEFAULT 0, used_by_device TEXT,
                duration_days INTEGER DEFAULT 30,
                created_at TEXT, used_at TEXT
            )
        """)
        
        # Migrations for existing deployments
        cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS paid_until TEXT")
        cur.execute("ALTER TABLE access_codes ADD COLUMN IF NOT EXISTS duration_days INTEGER DEFAULT 30")
        cur.execute("ALTER TABLE validation_logs ADD COLUMN IF NOT EXISTS device_id TEXT")
        cur.execute("ALTER TABLE match_summaries ADD COLUMN IF NOT EXISTS device_id TEXT")

def get_or_create_device(device_id: str):
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM devices WHERE device_id = %s", (device_id,))
        row = cur.fetchone()
        if row:
            d = dict(row)
            if d["is_paid"] and d.get("paid_until"):
                if datetime.utcnow().isoformat() > d["paid_until"]:
                    cur.execute("UPDATE devices SET is_paid = 0 WHERE device_id = %s", (device_id,))
                    d["is_paid"] = 0
            return d
        cur.execute(
            "INSERT INTO devices (device_id, trial_used, is_paid, paid_until, created_at) VALUES (%s,0,0,NULL,%s)",
            (device_id, datetime.utcnow().isoformat())
        )
        return {"device_id": device_id, "trial_used": 0, "is_paid": 0, "paid_until": None}

def increment_device_trial(device_id: str):
    with get_conn() as conn:
        conn.cursor().execute("UPDATE devices SET trial_used = trial_used + 1 WHERE device_id = %s", (device_id,))

def get_ip_trial_count(ip: str) -> int:
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT trial_used FROM ip_trials WHERE ip = %s", (ip,))
        row = cur.fetchone()
        return row["trial_used"] if row else 0

def increment_ip_trial(ip: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT ip FROM ip_trials WHERE ip = %s", (ip,))
        if cur.fetchone():
            cur.execute("UPDATE ip_trials SET trial_used = trial_used + 1 WHERE ip = %s", (ip,))
        else:
            cur.execute("INSERT INTO ip_trials (ip, trial_used, created_at) VALUES (%s,1,%s)",
                         (ip, datetime.utcnow().isoformat()))

def redeem_access_code(device_id: str, code: str) -> bool:
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM access_codes WHERE code = %s", (code.strip().upper(),))
        row = cur.fetchone()
        if not row or row["used"]:
            return False
        duration_days = row["duration_days"] or 30
        cur.execute("UPDATE access_codes SET used = 1, used_by_device = %s, used_at = %s WHERE code = %s",
                     (device_id, datetime.utcnow().isoformat(), code.strip().upper()))
        cur.execute("SELECT paid_until, is_paid FROM devices WHERE device_id = %s", (device_id,))
        existing = cur.fetchone()
        base = datetime.utcnow()
        if existing and existing["is_paid"] and existing["paid_until"] and existing["paid_until"] > base.isoformat():
            base = datetime.fromisoformat(existing["paid_until"])
        new_until = (base + timedelta(days=duration_days)).isoformat()
        cur.execute("UPDATE devices SET is_paid = 1, paid_until = %s WHERE device_id = %s", (new_until, device_id))
        return True

def generate_access_code(code: str, duration_days: int = 30):
    with get_conn() as conn:
        conn.cursor().execute(
            "INSERT INTO access_codes (code, used, duration_days, created_at) VALUES (%s,0,%s,%s)",
            (code.strip().upper(), duration_days, datetime.utcnow().isoformat())
        )

def log_validation(device_id: str, gstin: str, invoice_number: Optional[str], severity: str,
                    is_valid: bool, tx_type: str, flag_count: int):
    try:
        with get_conn() as conn:
            conn.cursor().execute(
                "INSERT INTO validation_logs (device_id, gstin, invoice_number, overall_severity, is_valid, transaction_type, flag_count, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (device_id, gstin, invoice_number, severity, int(is_valid), tx_type, flag_count, datetime.utcnow().isoformat())
            )
    except Exception as e:
        print(f"[db] validation log failed: {e}")

def log_match_summary(device_id: str, total: int, matched: int, mismatched: int, missing: int, source: str):
    try:
        with get_conn() as conn:
            conn.cursor().execute(
                "INSERT INTO match_summaries (device_id, total, matched, mismatched, missing_in_gstr2b, source, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (device_id, total, matched, mismatched, missing, source, datetime.utcnow().isoformat())
            )
    except Exception as e:
        print(f"[db] match summary log failed: {e}")

def get_recent_validations(device_id: str, limit: int = 20):
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM validation_logs WHERE device_id = %s ORDER BY id DESC LIMIT %s", (device_id, limit))
        return [dict(r) for r in cur.fetchall()]

def get_recent_matches(device_id: str, limit: int = 20):
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM match_summaries WHERE device_id = %s ORDER BY id DESC LIMIT %s", (device_id, limit))
        return [dict(r) for r in cur.fetchall()]

def _date_where(date_from, date_to, clauses, params):
    if date_from:
        clauses.append("created_at >= %s"); params.append(date_from)
    if date_to:
        clauses.append("created_at <= %s"); params.append(date_to + "T23:59:59")

def query_validations(device_id: str, limit=20, offset=0, severity=None, date_from=None, date_to=None, search=None):
    # device_id is compulsory now
    clauses = ["device_id = %s"]
    params = [device_id]
    
    if severity: clauses.append("overall_severity = %s"); params.append(severity)
    if search:
        clauses.append("(gstin LIKE %s OR invoice_number LIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])
    _date_where(date_from, date_to, clauses, params)
    
    where = f"WHERE {' AND '.join(clauses)}"
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute(f"SELECT COUNT(*) AS c FROM validation_logs {where}", params)
        total = cur.fetchone()["c"]
        cur.execute(
            f"SELECT * FROM validation_logs {where} ORDER BY id DESC LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
        return total, [dict(r) for r in cur.fetchall()]

def query_matches(device_id: str, limit=20, offset=0, date_from=None, date_to=None):
    # device_id is compulsory now
    clauses = ["device_id = %s"]
    params = [device_id]
    
    _date_where(date_from, date_to, clauses, params)
    where = f"WHERE {' AND '.join(clauses)}"
    
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute(f"SELECT COUNT(*) AS c FROM match_summaries {where}", params)
        total = cur.fetchone()["c"]
        cur.execute(
            f"SELECT * FROM match_summaries {where} ORDER BY id DESC LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
        return total, [dict(r) for r in cur.fetchall()]
