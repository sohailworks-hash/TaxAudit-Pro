import hashlib, hmac, os, secrets
from fastapi import Header, HTTPException
import db

FREE_TRIAL_LIMIT = 3
ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme123")


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
            detail=f"Free trial limit ({FREE_TRIAL_LIMIT} checks) reached. Contact admin to unlock full access.",
        )
    db.increment_trial_used(user["id"])
