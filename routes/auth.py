import logging

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_auth, encrypt, decrypt
from app.bot import make_broadcast_fn, build_user_config
from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
import app.db as db

router = APIRouter()
templates = Jinja2Templates(directory="templates")
log = logging.getLogger(__name__)

oauth = OAuth()
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


@router.get("/login")
async def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "login.html")


@router.get("/auth/google")
async def google_login(request: Request):
    redirect_uri = str(request.url_for("google_callback"))
    if request.headers.get("x-forwarded-proto") == "https":
        redirect_uri = redirect_uri.replace("http://", "https://", 1)
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback")
async def google_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo")
    if not userinfo:
        return RedirectResponse("/login?error=auth_failed", status_code=303)

    user = await db.upsert_user_from_google(
        google_id=userinfo["sub"],
        email=userinfo["email"],
        name=userinfo.get("name", ""),
        avatar_url=userinfo.get("picture", ""),
    )

    request.session["user_id"] = user.id
    log.info("User logged in: %s (%s)", user.email, user.id)

    # Re-read user from DB to ensure we have latest state
    fresh_user = await db.get_user(user.id)
    if fresh_user:
        user = fresh_user

    if user.is_rejected:
        from sqlalchemy import select, update
        from app.db.models import RejectionLog
        from app.db.models import User as UserModel
        async with db.get_session() as session:
            latest = await session.execute(
                select(RejectionLog)
                .where(RejectionLog.user_id == user.id)
                .order_by(RejectionLog.rejected_at.desc())
                .limit(1)
            )
            latest_entry = latest.scalar_one_or_none()
            if latest_entry and latest_entry.status == "allowed":
                await session.execute(
                    update(UserModel).where(UserModel.id == user.id)
                    .values(is_rejected=False, is_approved=False)
                )
                await session.commit()
                user.is_rejected = False
                log.info("User %d (%s) re-registered after rejection allow", user.id, user.email)
            else:
                return RedirectResponse("/rejected", status_code=303)

    if user.is_approved and user.paper_bot_started:
        manager = request.app.state.bot_manager
        if manager.get_worker(user.id, "paper") is None:
            from app.bot import build_paper_config, make_broadcast_fn
            shared_market = request.app.state.shared_market
            paper_config = build_paper_config()
            try:
                await manager.start_bot(user.id, "paper", paper_config,
                                        broadcast_fn=make_broadcast_fn(user.id, "paper"),
                                        shared_market=shared_market)
            except Exception as e:
                log.error("Failed to start paper bot for user %d: %s", user.id, e)

    cfg = await db.get_user_config(user.id)
    if not cfg or not cfg.binance_api_key_enc:
        return RedirectResponse("/setup", status_code=303)

    return RedirectResponse("/dashboard", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/setup")
async def setup_page(request: Request):
    user = await require_auth(request)
    cfg = await db.get_user_config(user.id)
    return templates.TemplateResponse(request, "setup.html", {
        "user": user,
        "has_keys": cfg is not None and cfg.binance_api_key_enc is not None,
    })


@router.post("/setup")
async def save_setup(
    request: Request,
    api_key: str = Form(...),
    api_secret: str = Form(...),
):
    user = await require_auth(request)

    api_key_enc = encrypt(api_key.strip())
    api_secret_enc = encrypt(api_secret.strip())

    await db.save_user_config(
        user_id=user.id,
        api_key_enc=api_key_enc,
        api_secret_enc=api_secret_enc,
    )

    manager = request.app.state.bot_manager
    config = build_user_config(api_key.strip(), api_secret.strip())

    if manager.get_worker(user.id) is None:
        shared_market = request.app.state.shared_market
        await manager.start_bot(user.id, "live", config, broadcast_fn=make_broadcast_fn(user.id),
                                shared_market=shared_market)

    log.info("User %d configured API keys", user.id)
    return RedirectResponse("/dashboard?setup=ok", status_code=303)


@router.get("/pending")
async def pending_page(request: Request):
    from app.auth import get_current_user
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.is_rejected:
        return RedirectResponse("/rejected", status_code=303)
    if user.is_approved:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "pending.html", {"user": user})


@router.get("/rejected")
async def rejected_page(request: Request):
    from app.auth import get_current_user
    user = await get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not user.is_rejected:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "rejected.html", {"user": user})
