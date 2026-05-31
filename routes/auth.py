import logging

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import require_auth, encrypt
from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
import database as db

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
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html")


@router.get("/auth/google")
async def google_login(request: Request):
    redirect_uri = request.url_for("google_callback")
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

    cfg = await db.get_user_config(user.id)
    if not cfg or not cfg.binance_api_key_enc:
        return RedirectResponse("/setup", status_code=303)

    return RedirectResponse("/", status_code=303)


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
        "testnet": cfg.binance_testnet if cfg else True,
    })


@router.post("/setup")
async def save_setup(
    request: Request,
    api_key: str = Form(...),
    api_secret: str = Form(...),
    testnet: str = Form("on"),
):
    user = await require_auth(request)

    api_key_enc = encrypt(api_key.strip())
    api_secret_enc = encrypt(api_secret.strip())

    await db.save_user_config(
        user_id=user.id,
        api_key_enc=api_key_enc,
        api_secret_enc=api_secret_enc,
        testnet=(testnet == "on"),
    )

    manager = request.app.state.bot_manager
    from auth import decrypt
    from main import _build_user_config
    config = _build_user_config(api_key.strip(), api_secret.strip(), testnet == "on")

    if manager.get_worker(user.id) is None:
        from bot import make_broadcast_fn
        await manager.start_bot(user.id, config, broadcast_fn=make_broadcast_fn(user.id))

    log.info("User %d configured API keys", user.id)
    return RedirectResponse("/?setup=ok", status_code=303)
