import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_auth, get_admin_mode

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")


class AdminNotFound(Exception):
    pass


async def require_admin(request: Request):
    user = await require_auth(request)
    if not user.is_admin:
        raise AdminNotFound()
    return user


@router.post("/admin/toggle")
async def toggle_admin_mode(request: Request):
    user = await require_auth(request)
    if not user.is_admin:
        raise AdminNotFound()
    current = get_admin_mode(request)
    request.session["admin_mode"] = not current
    return JSONResponse({"ok": True, "admin_mode": not current})


from routes.admin.dashboard import router as dashboard_router  # noqa: E402
from routes.admin.users import router as users_router  # noqa: E402
from routes.admin.user_detail import router as user_detail_router  # noqa: E402
from routes.admin.analytics import router as analytics_router  # noqa: E402
from routes.admin.logs import router as logs_router  # noqa: E402

router.include_router(dashboard_router)
router.include_router(users_router)
router.include_router(user_detail_router)
router.include_router(analytics_router)
router.include_router(logs_router)
