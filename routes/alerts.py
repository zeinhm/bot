from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

import database as db
from template_context import get_global_context

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/alerts")
async def alerts_page(request: Request):
    events = await db.get_recent_events(100)
    ctx = await get_global_context()
    ctx.update({"events": events, "page": "alerts"})
    return templates.TemplateResponse(request, "alerts.html", ctx)
