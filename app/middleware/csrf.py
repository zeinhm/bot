import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
EXEMPT_PATHS = {"/auth/callback"}


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if "csrf_token" not in request.session:
            request.session["csrf_token"] = secrets.token_hex(32)

        if request.method in SAFE_METHODS:
            return await call_next(request)
        if request.headers.get("upgrade", "").lower() == "websocket":
            return await call_next(request)
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        session_token = request.session.get("csrf_token", "")
        submitted = request.headers.get("x-csrf-token", "")

        if not submitted or submitted != session_token:
            return JSONResponse({"error": "CSRF token missing or invalid"}, status_code=403)

        return await call_next(request)
