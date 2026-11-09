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
from app.core.database import Base, SessionLocal, engine, ensure_columns
from app.core.exceptions import register_exception_handlers
from app.core.log_setup import get_task_logger
from app.core.redis_client import close_redis
from app.core.response import fail
from app.services import session_auth
from app.services.plugin_auth import PLUGIN_WRITE_PATHS
from app.services.product_report_service import seed_markers_from_daily
from app.services.scheduler import daily_sync_loop, overview_today_loop, token_refresh_loop
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
    "/api/plugin/login",
    "/api/plugin/page",
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

    added = ensure_columns()
    if added:
        logger.info("已补齐缺失列：%s", "、".join(added))

    # 给已入库的商品日数据补写同步标记（幂等、零接口），让历史区间秒变覆盖。
    try:
        seed_db = SessionLocal()
        try:
            seeded = seed_markers_from_daily(seed_db)
            if seeded:
                logger.info("已补写商品同步标记 %s 条", seeded)
        finally:
            seed_db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("补写商品同步标记失败：%s", exc)

    stop_event = asyncio.Event()
    task_logger = get_task_logger()
    refresh_task = asyncio.create_task(token_refresh_loop(stop_event))
    task_logger.info("已启动 token 自动刷新定时任务")
    daily_task = asyncio.create_task(daily_sync_loop(stop_event))
    task_logger.info("已启动每日 00:01 数据入库定时任务")
    today_task = asyncio.create_task(overview_today_loop(stop_event))
    task_logger.info("已启动今天数据总览 15 分钟刷新")
    try:
        yield
    finally:
        stop_event.set()
        refresh_task.cancel()
        daily_task.cancel()
        today_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass
        try:
            await daily_task
        except asyncio.CancelledError:
            pass
        try:
            await today_task
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
        request.state.credential = "web"
        return await call_next(request)

    plugin_user = session_auth.read_plugin_user(request)
    if plugin_user:
        if request.method != "POST" or path not in PLUGIN_WRITE_PATHS:
            return JSONResponse(
                status_code=200,
                content=fail(40303, "这个登录凭证只能修改佣金和实际退款率"),
            )
        request.state.user = plugin_user
        request.state.credential = "plugin"
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


@app.get("/overview", response_class=HTMLResponse, include_in_schema=False)
def page_overview():
    """数据总览。"""
    return _render_page("overview.html")


@app.get("/plans", response_class=HTMLResponse, include_in_schema=False)
def page_plans():
    """计划与 ROI。"""
    return _render_page("plans.html")


@app.get("/team", response_class=HTMLResponse, include_in_schema=False)
def page_team():
    """团队管理。"""
    return _render_page("team.html")


@app.get("/reports", response_class=HTMLResponse, include_in_schema=False)
def page_reports():
    """投放报表。"""
    return _render_page("reports.html")
