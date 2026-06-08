import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
EXEMPT_PATHS = {"/auth/callback"}


class CSRFMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)

        if "csrf_token" not in request.session:
            request.session["csrf_token"] = secrets.token_hex(32)

        if request.method in SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        if request.url.path in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        session_token = request.session.get("csrf_token", "")
        submitted = request.headers.get("x-csrf-token", "")

        if not submitted or submitted != session_token:
            response = JSONResponse({"error": "CSRF token missing or invalid"}, status_code=403)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
