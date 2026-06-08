# Security Hardening — Tech Spec

## Context

Security audit on 2026-06-08 found 10 vulnerabilities across the ZENITH trading bot platform. Two are critical (actively exploitable with zero authentication), two require a logged-in victim to visit a malicious link. This spec covers the fixes in priority order.

Platform handles real money (Binance Futures) and stores encrypted API keys for multiple users. A breach means real financial loss.

---

## P0 — Critical (exploitable now, no auth needed) ✅ FIXED

### 1. WebSocket Authentication Bypass ✅

**Vulnerability**: Two WebSocket endpoints have no/broken authentication.

**`routes/ws.py`** — Legacy `/ws` endpoint accepts any connection, no session check at all. Receives all broadcast data for user ID 1.

**`app/bot/websocket.py:58-63`** — Main `/ws/{user_id}` endpoint has a logic flaw:
```python
session_user = ws.session.get("user_id") if hasattr(ws, "session") else None
if session_user is not None and session_user != user_id:
    await ws.close(code=4003)
    return
```
When `session_user is None` (no session), the `if` is False — connection proceeds. Anyone can connect to `/ws/ANY_ID`.

**Fix**:

1. **Delete `routes/ws.py`** entirely and remove its import/mount from `main.py`. It's a legacy endpoint that should not exist.

2. **Fix `/ws/{user_id}` auth** in `app/bot/websocket.py`:
```python
@router.websocket("/ws/{user_id}")
async def websocket_endpoint(ws: WebSocket, user_id: int):
    session_user = ws.session.get("user_id") if hasattr(ws, "session") else None
    if session_user is None or session_user != user_id:
        await ws.close(code=4003)
        return
    # ... rest unchanged
```
Change `is not None and` to `is None or` — reject if no session OR if user mismatch.

**Files**: `routes/ws.py` (delete), `main.py` (remove import/mount), `app/bot/websocket.py` (fix condition), `app/bot/__init__.py` (remove `register_ws`/`unregister_ws` if unused after deletion)

**Test**: Connect to `/ws/1` from an unauthenticated browser tab or `wscat` — should get closed with 4003.

---

### 2. CSRF Protection ✅

**Vulnerability**: All POST endpoints lack CSRF tokens. A malicious page can trigger actions on behalf of a logged-in user: emergency close positions, delete API keys, stop bot, approve/reject users.

**Fix**: Add CSRF middleware to the FastAPI app.

**Approach**: Use a double-submit cookie pattern (no server-side state needed):
1. On every response, set a `csrf_token` cookie (SameSite=Lax, HttpOnly=False so JS can read it)
2. On every POST/PUT/DELETE, require an `X-CSRF-Token` header matching the cookie
3. For HTML forms (non-fetch), add a hidden `<input name="_csrf_token">` populated from the cookie

**Implementation**:

Create `app/middleware/csrf.py`:
- Generate a random token per session (store in session, not just cookie)
- Middleware checks: if method is POST/PUT/DELETE, compare `request.headers["x-csrf-token"]` or `form._csrf_token` against `request.session["csrf_token"]`
- Exempt: WebSocket upgrade requests, `/auth/callback` (OAuth redirect)
- Return 403 on mismatch

Update `templates/base.html`:
- Add `<meta name="csrf-token" content="{{ csrf_token }}">` in `<head>`
- Update all `fetch()` calls to include `headers: {"X-CSRF-Token": document.querySelector('meta[name=csrf-token]').content}`

Update standalone templates (`login.html`, `setup.html`):
- Add hidden input to forms: `<input type="hidden" name="_csrf_token" value="{{ csrf_token }}">`

Update `main.py`:
- Add CSRF middleware after session middleware

**Files**: `app/middleware/csrf.py` (new), `main.py`, `templates/base.html`, `templates/setup.html`, `templates/settings.html`

**Test**: 
- Submit a form normally → works
- Submit from a different origin (curl without token) → 403
- Fetch POST from JS on the page → works (token in header)

---

## P1 — High (requires compromised session or leaked secrets)

### 3. Validate SESSION_SECRET Is Not Default ✅

**Vulnerability**: `SESSION_SECRET` defaults to `"change-me-in-production"`. If not overridden, sessions can be forged.

**Fix**: 
- On startup in `main.py`, check if `SESSION_SECRET == "change-me-in-production"` and **refuse to start** (or log a CRITICAL warning and auto-generate a random one)
- Add a startup check:
```python
if SESSION_SECRET == "change-me-in-production":
    import secrets
    log.critical("SESSION_SECRET is default! Generating random one. Set it in .env for persistent sessions.")
    actual_secret = secrets.token_hex(32)
else:
    actual_secret = SESSION_SECRET
```

**Files**: `main.py`

**Test**: Start without `SESSION_SECRET` in env → see critical log warning.

---

### 4. Sanitize Error Responses

**Vulnerability**: `emergency_close` and potentially other endpoints return raw `str(e)` to clients, leaking internal paths and state.

**Fix**: Replace all `str(e)` in client-facing JSON responses with generic messages:
```python
return JSONResponse({"ok": False, "error": "Internal error. Check server logs."}, status_code=500)
```

Audit all routes for `str(e)` in responses. Keep detailed logging server-side.

**Files**: `routes/settings.py`, `routes/bot_control.py`, `routes/admin/users.py`, `routes/admin/user_detail.py`

**Test**: Trigger an error → response should say "Internal error", not a Python traceback.

---

### 5. Reduce API Key Preview Exposure

**Vulnerability**: Settings page shows first 8 characters of the raw Binance API key.

**Fix**: Show only first 4 and last 4 characters:
```python
api_key_preview = raw_key[:4] + "••••••••" + raw_key[-4:]
```

**Files**: `routes/settings.py`

---

### 6. Restrict Admin API Key Access

**Vulnerability**: Admin reconciliation endpoint decrypts any user's API keys. The anomaly scanner does the same for all users.

**Fix** (defense in depth):
- Add audit logging: every time a user's API keys are decrypted for reconciliation, log the admin who triggered it and the target user
- Rate limit reconciliation: max 1 per user per 5 minutes
- Consider making the anomaly scanner use a dedicated service key instead of user keys (longer-term)

**Files**: `routes/admin/user_detail.py`, `app/bot/websocket.py`

---

## P2 — Medium (defense in depth)

### 7. Rate Limiting

**Fix**: Add rate limiting middleware using an in-memory token bucket (no Redis needed at current scale).

Limits:
- `/auth/*`: 10 requests/minute per IP
- `/settings/api-keys`: 5 requests/minute per user
- `/api/emergency-close`: 3 requests/minute per user
- `/admin/users/*`: 20 requests/minute per user
- `/api/backtest*`: 5 requests/minute per user (CPU-intensive)
- WebSocket connections: max 5 per user

**Implementation**: Create `app/middleware/rate_limit.py` with a simple in-memory dict of `{key: (count, window_start)}`. Apply as FastAPI dependency on specific route groups.

**Files**: `app/middleware/rate_limit.py` (new), route files (add dependency)

---

### 8. Security Headers

**Fix**: Add a middleware or response hook that sets:
```
Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://unpkg.com; style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data: https://lh3.googleusercontent.com; connect-src 'self' wss://*.zenithbot.org
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
Strict-Transport-Security: max-age=31536000; includeSubDomains
Referrer-Policy: strict-origin-when-cross-origin
```

**Files**: `main.py` (add middleware)

---

### 9. Session Regeneration After Login

**Fix**: In `routes/auth.py`, after successful OAuth callback, regenerate the session:
```python
request.session.clear()
request.session["user_id"] = user.id
```
This prevents session fixation attacks.

**Files**: `routes/auth.py`

---

### 10. Secure Cookie Settings ✅

**Fix**: Ensure session cookie has proper flags:
```python
app.add_middleware(
    SessionMiddleware,
    secret_key=actual_secret,
    max_age=86400,
    https_only=True,       # only send over HTTPS
    same_site="lax",       # CSRF protection layer
)
```

`https_only=True` prevents cookie theft over HTTP. `same_site="lax"` is an additional CSRF defense layer.

**Files**: `main.py`

---

## Implementation Order

| Phase | Items | Effort | Risk reduced |
|-------|-------|--------|-------------|
| **Phase 1** | #1 WebSocket auth, #2 CSRF, #3 Session secret, #10 Secure cookies | ~2-3 hours | Eliminates all critical + most high vulnerabilities |
| **Phase 2** | #4 Error sanitization, #5 API key preview, #9 Session regeneration | ~30 min | Reduces information leakage |
| **Phase 3** | #7 Rate limiting, #8 Security headers, #6 Admin audit logging | ~1-2 hours | Defense in depth |

Phase 1 should be deployed immediately. Phases 2 and 3 can follow in the next session.

---

## Files Changed Summary

| File | Change |
|------|--------|
| `routes/ws.py` | DELETE |
| `main.py` | Remove legacy WS, add CSRF/security middlewares, session secret check, secure cookies |
| `app/bot/websocket.py` | Fix WS auth condition |
| `app/bot/__init__.py` | Remove unused register_ws/unregister_ws |
| `app/middleware/csrf.py` | NEW — CSRF double-submit cookie |
| `app/middleware/rate_limit.py` | NEW — In-memory rate limiter |
| `templates/base.html` | Add CSRF meta tag, update fetch headers |
| `templates/setup.html` | Add CSRF hidden input |
| `templates/settings.html` | Add CSRF hidden input |
| `routes/settings.py` | Sanitize errors, reduce key preview |
| `routes/bot_control.py` | Sanitize errors |
| `routes/auth.py` | Session regeneration |
| `routes/admin/user_detail.py` | Audit logging, sanitize errors |
| `routes/admin/users.py` | Sanitize errors |
