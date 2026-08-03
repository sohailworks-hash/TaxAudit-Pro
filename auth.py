import os, secrets, string
from fastapi import Header, HTTPException
import db

FREE_TRIAL_LIMIT = 3
ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme123")


def get_device(x_device_id: str = Header(None)):
    if not x_device_id:
        raise HTTPException(status_code=400, detail="Missing device ID.")
    return db.get_or_create_device(x_device_id)


def enforce_trial_limit(device: dict):
    if device["is_paid"]:
        return
    if device["trial_used"] >= FREE_TRIAL_LIMIT:
        raise HTTPException(
            status_code=402,
            detail=f"Free trial limit ({FREE_TRIAL_LIMIT} checks) reached. Contact +91-8955377472 or email sohailkhan902314@gmail.com with your access code request to unlock full access.",
        )
    db.increment_device_trial(device["device_id"])


def generate_code() -> str:
    chars = string.ascii_uppercase + string.digits
    return "GST-" + "".join(secrets.choice(chars) for _ in range(8))
