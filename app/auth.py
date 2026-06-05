import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from passlib.hash import bcrypt
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings

settings = get_settings()

# File-based session store (survives restarts)
SESSION_FILE = Path(settings.storage_path) / ".sessions.json"

_sessions: dict[str, dict] = {}


def _load_sessions():
    global _sessions
    try:
        if SESSION_FILE.exists():
            data = json.loads(SESSION_FILE.read_text())
            for token, sess in data.items():
                sess["created_at"] = datetime.fromisoformat(sess["created_at"])
            _sessions = data
    except Exception:
        _sessions = {}


def _save_sessions():
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        for token, sess in _sessions.items():
            data[token] = {
                "username": sess["username"],
                "created_at": sess["created_at"].isoformat() if isinstance(sess["created_at"], datetime) else sess["created_at"],
            }
        SESSION_FILE.write_text(json.dumps(data))
    except Exception:
        pass


# Load sessions on import
_load_sessions()


# Rate limiting for login
_login_attempts: dict[str, list[datetime]] = {}
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW = timedelta(minutes=1)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.verify(plain_password, hashed_password)


def hash_password(password: str) -> str:
    return bcrypt.hash(password)


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "username": username,
        "created_at": datetime.utcnow(),
    }
    _save_sessions()
    return token


def get_session(token: str) -> dict | None:
    session = _sessions.get(token)
    if not session:
        return None
    created = session["created_at"]
    if isinstance(created, str):
        created = datetime.fromisoformat(created)
        session["created_at"] = created
    age = datetime.utcnow() - created
    if age.total_seconds() > settings.session_max_age:
        _sessions.pop(token, None)
        _save_sessions()
        return None
    return session


def delete_session(token: str):
    _sessions.pop(token, None)
    _save_sessions()


def check_rate_limit(ip: str) -> bool:
    now = datetime.utcnow()
    attempts = _login_attempts.get(ip, [])
    attempts = [a for a in attempts if now - a < LOGIN_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) < MAX_LOGIN_ATTEMPTS


def record_login_attempt(ip: str):
    now = datetime.utcnow()
    if ip not in _login_attempts:
        _login_attempts[ip] = []
    _login_attempts[ip].append(now)


def authenticate(username: str, password: str) -> bool:
    if username != settings.admin_username:
        return False
    return password == settings.admin_password


def get_current_user(request: Request) -> dict | None:
    token = request.cookies.get("session_token")
    if not token:
        return None
    return get_session(token)


# Public paths that don't require authentication
PUBLIC_PATHS = {"/login", "/static"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Allow public paths
        if path == "/login" or path.startswith("/static"):
            return await call_next(request)

        # Check session
        user = get_current_user(request)
        if not user:
            return RedirectResponse(url="/login", status_code=302)

        request.state.user = user
        return await call_next(request)
