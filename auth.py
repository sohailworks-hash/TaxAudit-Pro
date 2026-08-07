import os, secrets, string
from fastapi import Header, HTTPException, Request
import db

FREE_TRIAL_LIMIT = 3
ADMIN_KEY = os.environ.get("ADMIN_KEY")
if not ADMIN_KEY:
    # Generates a random key each restart so no one can guess a default.
    # Set the ADMIN_KEY env var in production to a fixed secret value.
    ADMIN_KEY = secrets.token_urlsafe(24)
    print(f"[auth] WARNING: ADMIN_KEY env var not set. Using random key for this run: {ADMIN_KEY}")


def get_device(request: Request, x_device_id: str = Header(None)):
    if not x_device_id:
        raise HTTPException(status_code=400, detail="Missing device ID.")
    device = db.get_or_create_device(x_device_id)
    device["_ip"] = request.client.host if request.client else None
    return device


def enforce_trial_limit(device: dict):
    if device["is_paid"]:
        return
    ip = device.get("_ip")
    ip_count = db.get_ip_trial_count(ip) if ip else 0
    if device["trial_used"] >= FREE_TRIAL_LIMIT or ip_count >= FREE_TRIAL_LIMIT:
        raise HTTPException(
            status_code=402,
            detail=f"Free trial limit ({FREE_TRIAL_LIMIT} checks) reached. Contact +91-8955377472 or email sohailkhan902314@gmail.com with your access code request to unlock full access.",
        )
    db.increment_device_trial(device["device_id"])
    if ip:
        db.increment_ip_trial(ip)


def generate_code() -> str:
    chars = string.ascii_uppercase + string.digits
    return "GST-" + "".join(secrets.choice(chars) for _ in range(8))
