from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.auth import require_auth, get_trading_mode
import app.db as db
from app.core.context import get_global_context

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/alerts")
async def alerts_page(request: Request):
    user = await require_auth(request)
    mode = get_trading_mode(request)
    is_paper = (mode == "paper")
    events = await db.get_recent_events(100, user_id=user.id, is_paper=is_paper)
    ctx = await get_global_context(user.id, mode)
    ctx.update({"user": user, "events": events, "page": "alerts"})
    return templates.TemplateResponse(request, "alerts.html", ctx)
