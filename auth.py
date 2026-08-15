import os
import secrets
import string
from datetime import datetime, timedelta
from fastapi import Header, HTTPException, Request, Depends
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from jose import JWTError, jwt
import db

FREE_TRIAL_LIMIT = 3
ADMIN_KEY = os.environ.get("ADMIN_KEY")
if not ADMIN_KEY:
    ADMIN_KEY = secrets.token_urlsafe(24)
    print(f"[auth] WARNING: ADMIN_KEY env var not set. Using random key for this run: {ADMIN_KEY}")

# --- STRICT JWT SECRET CHECK ---
JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY")
if not JWT_SECRET_KEY:
    raise RuntimeError("CRITICAL ERROR: JWT_SECRET_KEY environment variable is not set. Cannot start the application safely.")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password)


def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def get_client_ip(request: Request) -> str | None:
    """Proxy-safe IP extraction"""
    if not request:
        return None
    # X-Forwarded-For can be a comma-separated list of IPs, the first one is the original client
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else None


# --- DEPENDENCIES ---

def get_current_user(token: str = Depends(oauth2_scheme), request: Request = None):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[ALGORITHM])
        user_id: int = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.get_user_by_id(int(user_id))
    if user is None:
        raise credentials_exception

    user["_ip"] = get_client_ip(request)
    return user


def enforce_trial_limit(user: dict):
    if user.get("is_paid"):
        return

    ip = user.get("_ip")
    ip_count = db.get_ip_trial_count(ip) if ip else 0
    if user.get("trial_used", 0) >= FREE_TRIAL_LIMIT or ip_count >= FREE_TRIAL_LIMIT:
        raise HTTPException(
            status_code=402,
            detail=f"Free trial limit ({FREE_TRIAL_LIMIT} checks) reached. Please upgrade to unlock full access.",
        )
    db.increment_user_trial(user["id"])
    if ip:
        db.increment_ip_trial(ip)


# Keeping for backward compatibility if needed elsewhere
def get_device(request: Request, x_device_id: str = Header(None)):
    if not x_device_id:
        raise HTTPException(status_code=400, detail="Missing device ID.")
    device = db.get_or_create_device(x_device_id)
    device["_ip"] = get_client_ip(request)
    return device


def generate_code() -> str:
    chars = string.ascii_uppercase + string.digits
    return "GST-" + "".join(secrets.choice(chars) for _ in range(8))