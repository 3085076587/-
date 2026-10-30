"""Ad Helper 千川方舟代理商 ERP 入口。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import get_settings
from app.core.database import Base, engine
from app.core.exceptions import register_exception_handlers
from app.core.redis_client import close_redis
from app.core.response import fail
from app.services import session_auth
from app.services.scheduler import token_refresh_loop
import app.models  # noqa: F401 注册 ORM 模型

logger = logging.getLogger("uvicorn.error")
settings = get_settings()
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
PAGES_DIR = STATIC_DIR / "pages"

_PUBLIC_EXACT = {
    "/login",
    "/favicon.ico",
    "/api/auth/login",
    "/api/oauth/callback",
}
_PUBLIC_PREFIX = ("/static/",)


def _render_page(name: str, *, inject_authorize: bool = False) -> HTMLResponse:
    html = (PAGES_DIR / name).read_text(encoding="utf-8")
    if inject_authorize:
        html = html.replace("__AUTHORIZE_URL__", settings.authorize_url)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIX)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.AUTO_CREATE_TABLES:
        Base.metadata.create_all(bind=engine)
        logger.info("数据库表已检查/创建")

    stop_event = asyncio.Event()
    refresh_task = asyncio.create_task(token_refresh_loop(stop_event))
    logger.info("已启动 token 自动刷新定时任务")
    try:
        yield
    finally:
        stop_event.set()
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass
        await close_redis()
        engine.dispose()


app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "巨量千川方舟代理商 ERP：查看投放数据 + 修改计划 ROI2 目标。"
        "统一响应 {code,msg,data}。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

register_exception_handlers(app)
app.include_router(api_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def login_gate(request: Request, call_next):
    path = request.url.path
    if _is_public(path):
        return await call_next(request)

    user = session_auth.read_session(request)
    if user:
        request.state.user = user
        return await call_next(request)

    if path.startswith("/api/"):
        return JSONResponse(
            status_code=200,
            content=fail(40100, "未登录或登录已过期"),
        )
    next_url = path if path != "/login" else "/"
    return RedirectResponse(url=f"/login?next={next_url}", status_code=302)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return RedirectResponse(url="/static/img/favicon.png", status_code=302)


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def page_login(request: Request):
    if session_auth.read_session(request):
        return RedirectResponse(url="/", status_code=302)
    return _render_page("login.html")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def page_auth():
    """授权账号信息。"""
    return _render_page("auth.html", inject_authorize=True)


@app.get("/advertisers", response_class=HTMLResponse, include_in_schema=False)
def page_advertisers():
    """子账户列表。"""
    return _render_page("advertisers.html")


@app.get("/plans", response_class=HTMLResponse, include_in_schema=False)
def page_plans():
    """计划与 ROI。"""
    return _render_page("plans.html")


@app.get("/reports", response_class=HTMLResponse, include_in_schema=False)
def page_reports():
    """投放报表。"""
    return _render_page("reports.html")
