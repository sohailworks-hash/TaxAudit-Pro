import hashlib, hmac, os, secrets, smtplib, random
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from fastapi import Header, HTTPException
import db

FREE_TRIAL_LIMIT = 3
ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme123")
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")


def hash_password(password: str, salt: str = None) -> str:
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt, _ = stored.split("$")
    return hmac.compare_digest(hash_password(password, salt), stored)


def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Login required.")
    token = authorization.split(" ", 1)[1]
    user = db.get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session. Please login again.")
    return user


def enforce_trial_limit(user: dict):
    if user["is_paid"]:
        return
    if user["trial_used"] >= FREE_TRIAL_LIMIT:
        raise HTTPException(
            status_code=402,
            detail=f"Free trial limit ({FREE_TRIAL_LIMIT} checks) reached. Contact +91-8955377472 or email sohailkhan902314@gmail.com to unlock full access.",
        )
    db.increment_trial_used(user["id"])


def generate_and_send_otp(email: str):
    otp = str(random.randint(100000, 999999))
    expiry = (datetime.utcnow() + timedelta(minutes=10)).isoformat()
    db.set_otp(email, otp, expiry)

    if not SMTP_EMAIL or not SMTP_PASSWORD:
        print(f"[auth] SMTP not configured. OTP for {email}: {otp}")
        return

    msg = MIMEText(
        f"Hi,\n\n"
        f"Your verification code for GST Audit Assistant is: {otp}\n\n"
        f"This code is valid for 10 minutes. If you did not request this, please ignore this email.\n\n"
        f"— GST Audit Assistant Team\n"
        f"Contact: +91-8955377472 · sohailkhan902314@gmail.com"
    )
    msg["Subject"] = "Your GST Audit Assistant verification code"
    msg["From"] = f"GST Audit Assistant <{SMTP_EMAIL}>"
    msg["To"] = email

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.sendmail(SMTP_EMAIL, [email], msg.as_string())
    except Exception as e:
        print(f"[auth] Failed to send OTP email: {e}")
