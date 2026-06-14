"""TOTP two-factor auth helpers + the step-up gate.

2FA is opt-in. Once a user enables it, sensitive actions (API-key changes, bot
start/stop, settings save, emergency close) require a fresh TOTP verification
within VERIFY_WINDOW_SECONDS, tracked in the session.
"""

from __future__ import annotations

import hashlib
import io
import json
import secrets
import time

import pyotp
import qrcode
import qrcode.image.svg
from starlette.responses import JSONResponse

ISSUER = "ZENITH"
BACKUP_CODE_COUNT = 10
VERIFY_WINDOW_SECONDS = 15 * 60  # re-prompt for a code after 15 min

# Brute-force protection for code checks. In-memory is fine for the single
# uvicorn process; it resets on restart (acceptable — it only slows attackers).
MAX_CODE_ATTEMPTS = 5
ATTEMPT_WINDOW_SECONDS = 300
_failed_attempts: dict[int, list[float]] = {}


def rate_limited(user_id: int) -> bool:
    """True if the user has too many recent failed code attempts."""
    now = time.time()
    fails = [t for t in _failed_attempts.get(user_id, []) if now - t < ATTEMPT_WINDOW_SECONDS]
    _failed_attempts[user_id] = fails
    return len(fails) >= MAX_CODE_ATTEMPTS


def record_code_failure(user_id: int) -> None:
    _failed_attempts.setdefault(user_id, []).append(time.time())


def clear_code_failures(user_id: int) -> None:
    _failed_attempts.pop(user_id, None)


def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email or "account", issuer_name=ISSUER)


def verify_code(secret: str, code: str) -> bool:
    """True if `code` is a valid TOTP for `secret` (±1 step to tolerate clock skew)."""
    if not secret or not code:
        return False
    try:
        return pyotp.TOTP(secret).verify(str(code).strip().replace(" ", ""), valid_window=1)
    except Exception:
        return False


# --- One-time backup recovery codes (self-service recovery) ---

def generate_backup_codes(n: int = BACKUP_CODE_COUNT) -> list[str]:
    """Human-readable one-time codes, e.g. ``a1b2c-d3e4f`` (40 bits each)."""
    codes = []
    for _ in range(n):
        raw = secrets.token_hex(5)  # 10 hex chars
        codes.append(raw[:5] + "-" + raw[5:])
    return codes


def _normalize_code(code: str) -> str:
    return "".join(ch for ch in str(code).lower() if ch.isalnum())


def hash_backup_code(code: str) -> str:
    return hashlib.sha256(_normalize_code(code).encode()).hexdigest()


def hash_backup_codes(codes: list[str]) -> str:
    """JSON list of sha256 hashes, ready to store in users.totp_backup_codes."""
    return json.dumps([hash_backup_code(c) for c in codes])


def backup_codes_remaining(stored_json: str | None) -> int:
    try:
        return len(json.loads(stored_json or "[]"))
    except Exception:
        return 0


def consume_backup_code(stored_json: str | None, code: str) -> tuple[bool, str]:
    """If `code` matches a stored hash, remove it. Returns (matched, new_json)."""
    try:
        hashes = json.loads(stored_json or "[]")
    except Exception:
        hashes = []
    h = hash_backup_code(code)
    if h in hashes:
        hashes.remove(h)
        return True, json.dumps(hashes)
    return False, stored_json or "[]"


def verify_totp_or_backup(secret: str, backup_json: str | None, code: str) -> tuple[bool, bool, str]:
    """Accept a TOTP code OR a one-time backup code. Returns
    (ok, used_backup, new_backup_json). On a backup match the code is consumed
    (removed from new_backup_json)."""
    if verify_code(secret, code):
        return True, False, backup_json or "[]"
    matched, new_json = consume_backup_code(backup_json, code)
    if matched:
        return True, True, new_json
    return False, False, backup_json or "[]"


def qr_svg(uri: str) -> str:
    """Inline SVG QR for the provisioning URI (no Pillow dependency)."""
    qr = qrcode.QRCode(box_size=10, border=2, image_factory=qrcode.image.svg.SvgPathImage)
    qr.add_data(uri)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image().save(buf)
    return buf.getvalue().decode()


def is_2fa_fresh(request) -> bool:
    ts = request.session.get("twofa_verified_at", 0)
    try:
        return (time.time() - float(ts)) < VERIFY_WINDOW_SECONDS
    except Exception:
        return False


def mark_2fa_verified(request) -> None:
    request.session["twofa_verified_at"] = int(time.time())


def require_2fa(request, user) -> JSONResponse | None:
    """Step-up gate. Returns a 401 JSON (``twofa_required``) when the user has 2FA
    enabled and isn't freshly verified; otherwise None (allow).

    Usage in a route:  ``chal = require_2fa(request, user)``  →  ``if chal: return chal``
    """
    if not getattr(user, "totp_enabled", False):
        return None
    if is_2fa_fresh(request):
        return None
    return JSONResponse(
        {"ok": False, "twofa_required": True, "error": "Two-factor verification required."},
        status_code=401,
    )
