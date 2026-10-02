"""Accounts, password hashing and sessions.

Design decisions worth knowing:

* **Passwords are hashed with scrypt** (stdlib `hashlib.scrypt`), each with its
  own 16-byte salt. Verification is constant-time. A plaintext password never
  leaves the request handler and is never logged -- the logging layer's
  denylist blocks the field name regardless.

* **Sessions are stateless, signed cookies.** The cookie carries the user id,
  role and an expiry, signed with HMAC-SHA256. Nothing is stored server-side,
  which matters on Cloud Run: an in-memory session table would log a user out
  the moment their next request landed on a different instance. A signed
  cookie is verified identically by every instance.

* **Logs carry a hashed user id and a role, never an email address.** The
  email is the PII; the role and a stable pseudonymous id are what an
  operator actually needs in order to answer "who was affected".

The seeded demo accounts exist so a reviewer can sign in immediately. Their
credentials are printed on the sign-in page on purpose -- this is a
demonstration application with no real data behind it.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from typing import Any, Dict, Optional, Tuple

SESSION_COOKIE = "ck_session"
SESSION_TTL_S = 8 * 3600

ROLE_CUSTOMER = "customer"
ROLE_STAFF = "staff"

# scrypt is preferred, but hashlib.scrypt requires OpenSSL 1.1+ and is absent
# on Pythons linked against LibreSSL (macOS system Python, for one). The
# container has it; a developer laptop may not. Rather than fail on one and
# work on the other, we use scrypt where available and PBKDF2-HMAC-SHA256
# otherwise, and record which scheme produced each hash so either verifies.
_SCRYPT = {"n": 16384, "r": 8, "p": 1, "dklen": 32}
_PBKDF2_ROUNDS = 200_000
_HAS_SCRYPT = hasattr(hashlib, "scrypt")


def _derive(password: bytes, salt: bytes, scheme: str) -> bytes:
    if scheme == "scrypt":
        return hashlib.scrypt(password, salt=salt, **_SCRYPT)
    return hashlib.pbkdf2_hmac("sha256", password, salt, _PBKDF2_ROUNDS, dklen=32)


def kdf_scheme() -> str:
    return "scrypt" if _HAS_SCRYPT else "pbkdf2"

# Signing key for session cookies. Set SESSION_SECRET in production so that a
# redeploy does not invalidate every signed-in session; a random key is fine
# for a demo and is strictly better than a hard-coded one.
_SECRET = os.environ.get("SESSION_SECRET", "").strip().encode() or secrets.token_bytes(32)

_lock = threading.Lock()
_users_by_email: Dict[str, Dict[str, Any]] = {}
_users_by_id: Dict[str, Dict[str, Any]] = {}


# --- password hashing -----------------------------------------------------
def hash_password(password: str) -> str:
    scheme = kdf_scheme()
    salt = secrets.token_bytes(16)
    dk = _derive(password.encode("utf-8"), salt, scheme)
    return "%s$%s$%s" % (scheme, base64.b64encode(salt).decode(),
                         base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, dk_b64 = stored.split("$", 2)
        if scheme not in ("scrypt", "pbkdf2"):
            return False
        if scheme == "scrypt" and not _HAS_SCRYPT:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except Exception:
        return False
    actual = _derive(password.encode("utf-8"), salt, scheme)
    return hmac.compare_digest(actual, expected)


def user_hash(user_id: str) -> str:
    """Stable pseudonymous id for logs. Never reversible to an email."""
    return hashlib.sha256(("cognikart|" + user_id).encode()).hexdigest()[:16]


# --- accounts -------------------------------------------------------------
def create_user(email: str, password: str, name: str,
                role: str = ROLE_CUSTOMER) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Returns (user, error_code)."""
    email = (email or "").strip().lower()
    if "@" not in email or len(email) < 5:
        return None, "INVALID_EMAIL"
    if len(password or "") < 8:
        return None, "WEAK_PASSWORD"
    if not (name or "").strip():
        return None, "INVALID_NAME"
    with _lock:
        if email in _users_by_email:
            return None, "EMAIL_TAKEN"
        uid = "user-" + uuid.uuid4().hex[:10]
        user = {
            "id": uid,
            "email": email,
            "name": name.strip()[:60],
            "role": role if role in (ROLE_CUSTOMER, ROLE_STAFF) else ROLE_CUSTOMER,
            "passwordHash": hash_password(password),
            "createdAt": time.time(),
        }
        _users_by_email[email] = user
        _users_by_id[uid] = user
    return user, None


def authenticate(email: str, password: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    email = (email or "").strip().lower()
    user = _users_by_email.get(email)
    if not user:
        # Do the work anyway so a missing account and a wrong password take a
        # similar amount of time.
        _derive(b"timing-equalizer", b"0123456789abcdef", kdf_scheme())
        return None, "NO_SUCH_ACCOUNT"
    if not verify_password(password or "", user["passwordHash"]):
        return None, "BAD_PASSWORD"
    return user, None


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    return _users_by_id.get(user_id)


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": user["id"], "name": user["name"], "email": user["email"],
            "role": user["role"], "userHash": user_hash(user["id"])}


# --- sessions -------------------------------------------------------------
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_session(user: Dict[str, Any]) -> str:
    payload = {
        "uid": user["id"],
        "role": user["role"],
        "name": user["name"],
        "exp": int(time.time()) + SESSION_TTL_S,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(_SECRET, body.encode(), hashlib.sha256).digest())
    return "%s.%s" % (body, sig)


def read_session(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token or "." not in token:
        return None
    body, _, sig = token.rpartition(".")
    try:
        expected = _b64e(hmac.new(_SECRET, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64d(body))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < time.time():
        return None
    return payload


# --- seeded demo accounts -------------------------------------------------
DEMO_ACCOUNTS = [
    {"email": "customer@cognikart.demo", "password": "ShopPass2026!",
     "name": "Sarah Jenkins", "role": ROLE_CUSTOMER},
    {"email": "staff@cognikart.demo", "password": "DeskPass2026!",
     "name": "Alex Vance", "role": ROLE_STAFF},
]


def seed_demo_accounts() -> int:
    created = 0
    for a in DEMO_ACCOUNTS:
        user, err = create_user(a["email"], a["password"], a["name"], a["role"])
        if user:
            created += 1
    return created


def demo_account_hints() -> Any:
    """Shown on the sign-in page. Safe: this is a demo application with no real
    data, and surfacing the credentials is how a reviewer gets in."""
    return [{"role": a["role"], "email": a["email"], "password": a["password"],
             "name": a["name"]} for a in DEMO_ACCOUNTS]


def stats() -> Dict[str, Any]:
    with _lock:
        return {"users": len(_users_by_id), "kdf": kdf_scheme(),
                "byRole": {
                    r: sum(1 for u in _users_by_id.values() if u["role"] == r)
                    for r in (ROLE_CUSTOMER, ROLE_STAFF)}}
