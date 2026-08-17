import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional
import psycopg2
import psycopg2.extras
import psycopg2.pool

GSTIN_PATTERN = r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"
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
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE,
                password_hash TEXT,
                trial_used INTEGER DEFAULT 0,
                is_paid INTEGER DEFAULT 0,
                paid_until TEXT,
                created_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                gstin TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS vendors (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                gstin TEXT NOT NULL,
                trade_name TEXT,
                first_seen_date TEXT NOT NULL,
                last_seen_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (user_id, gstin)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS validation_logs (
                id SERIAL PRIMARY KEY,
                device_id TEXT,
                user_id INTEGER,
                gstin TEXT, invoice_number TEXT, overall_severity TEXT,
                is_valid INTEGER, transaction_type TEXT, flag_count INTEGER,
                created_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS match_summaries (
                id SERIAL PRIMARY KEY,
                device_id TEXT,
                user_id INTEGER,
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
                used INTEGER DEFAULT 0,
                used_by_device TEXT,
                used_by_user_id INTEGER,
                duration_days INTEGER DEFAULT 30,
                created_at TEXT, used_at TEXT
            )
        """)

        # Migrations
        cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS paid_until TEXT")
        cur.execute("ALTER TABLE access_codes ADD COLUMN IF NOT EXISTS duration_days INTEGER DEFAULT 30")
        cur.execute("ALTER TABLE access_codes ADD COLUMN IF NOT EXISTS used_by_user_id INTEGER")
        cur.execute("ALTER TABLE validation_logs ADD COLUMN IF NOT EXISTS device_id TEXT")
        cur.execute("ALTER TABLE match_summaries ADD COLUMN IF NOT EXISTS device_id TEXT")
        cur.execute("ALTER TABLE validation_logs ADD COLUMN IF NOT EXISTS user_id INTEGER")
        cur.execute("ALTER TABLE match_summaries ADD COLUMN IF NOT EXISTS user_id INTEGER")

        # Vendor match-run counters (feeds risk score from GSTR-2B match runs, not just validate-invoice)
        cur.execute("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS match_total INTEGER DEFAULT 0")
        cur.execute("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS match_mismatch INTEGER DEFAULT 0")

        # Safely add client_id with Foreign Key
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='validation_logs' AND column_name='client_id'
                ) THEN
                    ALTER TABLE validation_logs ADD COLUMN client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE;
                END IF;
            END $$;
        """)

        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='match_summaries' AND column_name='client_id'
                ) THEN
                    ALTER TABLE match_summaries ADD COLUMN client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE;
                END IF;
            END $$;
        """)

        # Safely add vendor_id with ON DELETE SET NULL and Index
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='validation_logs' AND column_name='vendor_id'
                ) THEN
                    ALTER TABLE validation_logs ADD COLUMN vendor_id INTEGER REFERENCES vendors(id) ON DELETE SET NULL;
                    CREATE INDEX idx_validation_logs_vendor_id ON validation_logs(vendor_id);
                END IF;
            END $$;
        """)


# --- USER AUTHENTICATION FUNCTIONS ---
def create_user(email: str, password_hash: str):
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        try:
            cur.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (%s, %s, %s) RETURNING id, email",
                (email.lower(), password_hash, datetime.utcnow().isoformat())
            )
            return dict(cur.fetchone())
        except psycopg2.errors.UniqueViolation:
            return None

def get_user_by_email(email: str):
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM users WHERE email = %s", (email.lower(),))
        return cur.fetchone()

def get_user_by_id(user_id: int):
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if row:
            d = dict(row)
            if d["is_paid"] and d.get("paid_until"):
                if datetime.utcnow().isoformat() > d["paid_until"]:
                    cur.execute("UPDATE users SET is_paid = 0 WHERE id = %s", (user_id,))
                    d["is_paid"] = 0
            return d
        return None

def increment_user_trial(user_id: int):
    with get_conn() as conn:
        conn.cursor().execute("UPDATE users SET trial_used = trial_used + 1 WHERE id = %s", (user_id,))

# --- CLIENT MANAGEMENT FUNCTIONS ---
def create_client(user_id: int, name: str, gstin: str):
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute(
            "INSERT INTO clients (user_id, name, gstin, created_at) VALUES (%s, %s, %s, %s) RETURNING *",
            (user_id, name.strip(), gstin.strip().upper(), datetime.utcnow().isoformat())
        )
        return dict(cur.fetchone())

def get_clients_by_user(user_id: int):
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM clients WHERE user_id = %s ORDER BY id DESC", (user_id,))
        return [dict(r) for r in cur.fetchall()]

def get_client_by_id(client_id: int, user_id: int):
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM clients WHERE id = %s AND user_id = %s", (client_id, user_id))
        row = cur.fetchone()
        return dict(row) if row else None

def delete_client(client_id: int, user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM clients WHERE id = %s AND user_id = %s RETURNING id", (client_id, user_id))
        return cur.fetchone() is not None

# --- VENDOR TRACKING: CORE LINKING ---
def get_or_create_vendor(user_id: int, gstin: str, trade_name: str = None) -> int:
    if not gstin:
        return None

    gstin = gstin.strip().upper()
    if not re.match(GSTIN_PATTERN, gstin):
        return None  # Gracefully reject junk GSTINs

    now = datetime.utcnow().isoformat()
    date_only = now[:10]

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO vendors (user_id, gstin, trade_name, first_seen_date, last_seen_date, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, gstin)
            DO UPDATE SET last_seen_date = GREATEST(vendors.last_seen_date, EXCLUDED.last_seen_date)
            RETURNING id
        """, (user_id, gstin, trade_name, date_only, date_only, now))

        return cur.fetchone()[0]

def link_vendors_from_matches(user_id: int, results: list):
    """Non-fatal: creates/updates vendor for each supplier_gstin seen in a GSTR-2B match run,
    and bumps match_total / match_mismatch so match-run activity feeds into vendor risk too
    (not just validate-invoice logs)."""
    if not user_id or not results:
        return
    for r in results:
        gstin = getattr(r, "supplier_gstin", None)
        if not gstin:
            continue
        status = getattr(r, "status", None)
        status_val = getattr(status, "value", status)
        is_mismatch = status_val != "MATCHED"
        try:
            vendor_id = get_or_create_vendor(user_id, gstin)
            if not vendor_id:
                continue
            with get_conn() as conn:
                conn.cursor().execute(
                    """UPDATE vendors
                       SET match_total = match_total + 1,
                           match_mismatch = match_mismatch + %s
                       WHERE id = %s""",
                    (1 if is_mismatch else 0, vendor_id)
                )
        except Exception as ve:
            print(f"[db] match vendor link failed (non-fatal): {ve}")

# --- VENDOR TRACKING: PHASE 2 READ/UPDATE ---
def compute_vendor_risk(total: int, mismatch_count: int, months_active: int) -> str:
    if total < 3:
        return "INSUFFICIENT_DATA"
    pct = (mismatch_count / total) * 100
    if pct > 25 and months_active >= 3:
        return "HIGH"
    if pct >= 10:
        return "MEDIUM"
    return "LOW"

def get_vendors_by_user(user_id: int):
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT v.id, v.gstin, v.trade_name, v.last_seen_date, v.first_seen_date,
                   v.match_total, v.match_mismatch,
                   COUNT(vl.id) AS total_invoices,
                   SUM(CASE WHEN vl.overall_severity != 'GREEN' THEN 1 ELSE 0 END) AS mismatch_count
            FROM vendors v
            LEFT JOIN validation_logs vl ON vl.vendor_id = v.id
            WHERE v.user_id = %s
            GROUP BY v.id
            ORDER BY v.last_seen_date DESC
        """, (user_id,))
        rows = [dict(r) for r in cur.fetchall()]

    results = []
    for r in rows:
        total = (r["total_invoices"] or 0) + (r["match_total"] or 0)
        mismatch = (r["mismatch_count"] or 0) + (r["match_mismatch"] or 0)
        pct = round((mismatch / total) * 100, 1) if total else 0.0
        try:
            months_active = max(1, (datetime.fromisoformat(r["last_seen_date"]) - datetime.fromisoformat(r["first_seen_date"])).days // 30)
        except Exception:
            months_active = 1
        r["total_invoices"] = total
        r["mismatch_count"] = mismatch
        r["mismatch_pct"] = pct
        r["risk_level"] = compute_vendor_risk(total, mismatch, months_active)
        results.append(r)
    return results

def get_vendor_detail(vendor_id: int, user_id: int):
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM vendors WHERE id = %s AND user_id = %s", (vendor_id, user_id))
        vendor = cur.fetchone()
        if not vendor:
            return None
        vendor = dict(vendor)

        cur.execute("""
            SELECT id, invoice_number, overall_severity, is_valid, transaction_type,
                   flag_count, client_id, created_at
            FROM validation_logs
            WHERE vendor_id = %s AND user_id = %s
            ORDER BY created_at DESC
            LIMIT 200
        """, (vendor_id, user_id))
        validations = [dict(r) for r in cur.fetchall()]

    val_total = len(validations)
    val_mismatch = sum(1 for v in validations if v["overall_severity"] != "GREEN")
    total = val_total + (vendor.get("match_total") or 0)
    mismatch = val_mismatch + (vendor.get("match_mismatch") or 0)
    pct = round((mismatch / total) * 100, 1) if total else 0.0
    try:
        months_active = max(1, (datetime.fromisoformat(vendor["last_seen_date"]) - datetime.fromisoformat(vendor["first_seen_date"])).days // 30)
    except Exception:
        months_active = 1

    vendor["total_invoices"] = total
    vendor["mismatch_count"] = mismatch
    vendor["mismatch_pct"] = pct
    vendor["risk_level"] = compute_vendor_risk(total, mismatch, months_active)
    vendor["validations"] = validations
    return vendor

def get_vendor_trend(vendor_id: int, user_id: int):
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT id FROM vendors WHERE id = %s AND user_id = %s", (vendor_id, user_id))
        if not cur.fetchone():
            return None
        cur.execute("""
            SELECT TO_CHAR(created_at::timestamp, 'YYYY-MM') AS month,
                   COUNT(*) AS total_invoices,
                   SUM(CASE WHEN overall_severity != 'GREEN' THEN 1 ELSE 0 END) AS mismatch_count
            FROM validation_logs
            WHERE vendor_id = %s AND user_id = %s
            GROUP BY month
            ORDER BY month ASC
        """, (vendor_id, user_id))
        rows = [dict(r) for r in cur.fetchall()]

    for r in rows:
        total = r["total_invoices"] or 0
        mismatch = r["mismatch_count"] or 0
        r["mismatch_pct"] = round((mismatch / total) * 100, 1) if total else 0.0
    return rows

def update_vendor_trade_name(vendor_id: int, user_id: int, trade_name: str) -> bool:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE vendors SET trade_name = %s WHERE id = %s AND user_id = %s RETURNING id",
            (trade_name, vendor_id, user_id)
        )
        return cur.fetchone() is not None


# --- EXISTING DEVICE & CODE FUNCTIONS ---
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

def redeem_access_code(user_id: int, code: str) -> bool:
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute(
            """
            UPDATE access_codes
            SET used = 1, used_by_user_id = %s, used_at = %s
            WHERE code = %s AND used = 0
            RETURNING duration_days
            """,
            (user_id, datetime.utcnow().isoformat(), code.strip().upper())
        )
        row = cur.fetchone()

        if not row:
            return False

        duration_days = row["duration_days"] or 30

        # FOR UPDATE locks this user's row until commit, preventing a lost-update
        # race when two access codes are redeemed for the same user concurrently.
        cur.execute("SELECT paid_until, is_paid FROM users WHERE id = %s FOR UPDATE", (user_id,))
        existing = cur.fetchone()
        base = datetime.utcnow()
        if existing and existing["is_paid"] and existing["paid_until"] and existing["paid_until"] > base.isoformat():
            base = datetime.fromisoformat(existing["paid_until"])

        new_until = (base + timedelta(days=duration_days)).isoformat()
        cur.execute("UPDATE users SET is_paid = 1, paid_until = %s WHERE id = %s", (new_until, user_id))
        return True

def generate_access_code(code: str, duration_days: int = 30):
    with get_conn() as conn:
        conn.cursor().execute(
            "INSERT INTO access_codes (code, used, duration_days, created_at) VALUES (%s,0,%s,%s)",
            (code.strip().upper(), duration_days, datetime.utcnow().isoformat())
        )

# --- LOGGING & QUERYING ---
def log_validation(gstin: str, invoice_number: Optional[str], severity: str,
                    is_valid: bool, tx_type: str, flag_count: int,
                    device_id: str = None, user_id: int = None, client_id: int = None):

    # --- VENDOR LOGIC (non-fatal — never blocks the core log write) ---
    vendor_id = None
    if user_id and gstin:
        try:
            vendor_id = get_or_create_vendor(user_id, gstin)
        except Exception as ve:
            print(f"[db] vendor link failed (non-fatal): {ve}")
    # ------------------------

    try:
        with get_conn() as conn:
            conn.cursor().execute(
                """INSERT INTO validation_logs
                (device_id, user_id, client_id, vendor_id, gstin, invoice_number, overall_severity, is_valid, transaction_type, flag_count, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (device_id, user_id, client_id, vendor_id, gstin, invoice_number, severity, int(is_valid), tx_type, flag_count, datetime.utcnow().isoformat())
            )
    except Exception as e:
        print(f"[db] validation log failed: {e}")

def log_match_summary(total: int, matched: int, mismatched: int, missing: int, source: str,
                      device_id: str = None, user_id: int = None, client_id: int = None):
    try:
        with get_conn() as conn:
            conn.cursor().execute(
                """INSERT INTO match_summaries
                (device_id, user_id, client_id, total, matched, mismatched, missing_in_gstr2b, source, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (device_id, user_id, client_id, total, matched, mismatched, missing, source, datetime.utcnow().isoformat())
            )
    except Exception as e:
        print(f"[db] match summary log failed: {e}")

def _date_where(date_from, date_to, clauses, params):
    if date_from:
        clauses.append("created_at >= %s"); params.append(date_from)
    if date_to:
        clauses.append("created_at <= %s"); params.append(date_to + "T23:59:59")

def query_validations(user_id: int = None, device_id: str = None, limit=20, offset=0,
                      severity=None, date_from=None, date_to=None, search=None):
    clauses = []
    params = []

    if user_id is not None:
        clauses.append("user_id = %s"); params.append(user_id)
    elif device_id is not None:
        clauses.append("device_id = %s"); params.append(device_id)

    if severity: clauses.append("overall_severity = %s"); params.append(severity)
    if search:
        clauses.append("(gstin LIKE %s OR invoice_number LIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])
    _date_where(date_from, date_to, clauses, params)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute(f"SELECT COUNT(*) AS c FROM validation_logs {where}", params)
        total = cur.fetchone()["c"]
        cur.execute(
            f"SELECT * FROM validation_logs {where} ORDER BY id DESC LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
        return total, [dict(r) for r in cur.fetchall()]

def query_matches(user_id: int = None, device_id: str = None, limit=20, offset=0, date_from=None, date_to=None):
    clauses = []
    params = []
    if user_id is not None:
        clauses.append("user_id = %s"); params.append(user_id)
    elif device_id is not None:
        clauses.append("device_id = %s"); params.append(device_id)

    _date_where(date_from, date_to, clauses, params)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with get_conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute(f"SELECT COUNT(*) AS c FROM match_summaries {where}", params)
        total = cur.fetchone()["c"]
        cur.execute(
            f"SELECT * FROM match_summaries {where} ORDER BY id DESC LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
        return total, [dict(r) for r in cur.fetchall()]