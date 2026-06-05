from datetime import date, datetime, timedelta

from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import get_db
from app.models import Notice, NoticeAnalysis, GeneratedBid, ScanLog, MonitorSettings
from app.auth import (
    AuthMiddleware, authenticate, create_session, delete_session,
    check_rate_limit, record_login_attempt,
)

settings = get_settings()

app = FastAPI(title="SEAP Monitor", docs_url=None, redoc_url=None)


# ─── Background Scheduler ─────────────────────────────────────────────
import asyncio
from loguru import logger

_scheduler_task = None


async def _scan_scheduler():
    """Background task that runs scans at configured intervals."""
    from datetime import datetime, timedelta
    from app.database import async_session
    from sqlalchemy import select as sa_select, desc as sa_desc
    await asyncio.sleep(30)  # Wait for app to fully start

    while True:
        try:
            async with async_session() as db:
                monitor = await db.scalar(select(MonitorSettings).limit(1))

                if monitor and monitor.is_active:
                    interval_hours = monitor.scan_interval_hours or 6

                    # Check when last scan was done
                    from app.models import ScanLog
                    last_scan = await db.scalar(
                        sa_select(ScanLog.finished_at)
                        .where(ScanLog.status == "success")
                        .order_by(sa_desc(ScanLog.finished_at))
                        .limit(1)
                    )

                    if last_scan:
                        time_since = datetime.utcnow() - last_scan
                        remaining = timedelta(hours=interval_hours) - time_since
                        if remaining.total_seconds() > 60:
                            wait_secs = int(remaining.total_seconds())
                            logger.info(f"Scheduler: last scan was {time_since.total_seconds()/3600:.1f}h ago, waiting {wait_secs/3600:.1f}h")
                            await asyncio.sleep(min(wait_secs, 3600))  # Check at least every hour
                            continue

                    logger.info(f"Scheduler: starting auto-scan (interval={interval_hours}h)")

                    from app.modules.monitor.scanner import scan_notices, send_cpv_report
                    results = await scan_notices(db)
                    report_count = await send_cpv_report(db)
                    logger.info(
                        f"Scheduler scan complete: {results['new_notices']} new, "
                        f"{results.get('matched_notices', 0)} matched CPV, "
                        f"{report_count} in email report"
                    )

                    await asyncio.sleep(interval_hours * 3600)
                else:
                    await asyncio.sleep(300)
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            await asyncio.sleep(60)


@app.on_event("startup")
async def start_scheduler():
    global _scheduler_task
    _scheduler_task = asyncio.create_task(_scan_scheduler())
    logger.info("Background scan scheduler started")


@app.on_event("shutdown")
async def stop_scheduler():
    global _scheduler_task
    if _scheduler_task:
        _scheduler_task.cancel()
        logger.info("Background scan scheduler stopped")

# Static files & templates
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# Auth middleware
app.add_middleware(AuthMiddleware)

# Include module routers
from app.modules.monitor.routes import router as monitor_router
from app.modules.analyzer.routes import router as analyzer_router
from app.modules.generator.routes import router as generator_router

app.include_router(monitor_router, prefix="/monitor")
app.include_router(analyzer_router, prefix="/analyzer")
app.include_router(generator_router, prefix="/generator")


# ─── Auth routes ──────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    client_ip = request.client.host if request.client else "unknown"

    if not check_rate_limit(client_ip):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Prea multe încercări. Așteaptă un minut.",
        })

    record_login_attempt(client_ip)

    if not authenticate(username, password):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Utilizator sau parolă incorectă.",
        })

    token = create_session(username)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        max_age=settings.session_max_age,
        samesite="lax",
        secure=True,
    )
    return response


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get("session_token")
    if token:
        delete_session(token)
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session_token")
    return response


# ─── Dashboard ────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    today = date.today()
    week_ago = today - timedelta(days=7)
    week_ahead = today + timedelta(days=7)

    # Stats
    today_count = await db.scalar(
        select(func.count(Notice.id)).where(Notice.publication_date == today)
    ) or 0

    week_count = await db.scalar(
        select(func.count(Notice.id)).where(Notice.publication_date >= week_ago)
    ) or 0

    analyzed_count = await db.scalar(
        select(func.count(NoticeAnalysis.id)).where(NoticeAnalysis.status == "completed")
    ) or 0

    bids_count = await db.scalar(
        select(func.count(GeneratedBid.id))
    ) or 0

    # Recent notices
    recent_result = await db.execute(
        select(Notice)
        .options(selectinload(Notice.analysis))
        .order_by(Notice.created_at.desc())
        .limit(5)
    )
    recent_notices = recent_result.scalars().all()

    # Upcoming deadlines
    deadline_result = await db.execute(
        select(Notice)
        .where(
            and_(
                Notice.deadline.isnot(None),
                Notice.deadline >= datetime.now(),
                Notice.deadline <= datetime.now() + timedelta(days=7),
            )
        )
        .order_by(Notice.deadline.asc())
        .limit(5)
    )
    upcoming_deadlines = deadline_result.scalars().all()

    # Monitor status
    monitor_settings = await db.scalar(select(MonitorSettings).limit(1))
    monitor_active = monitor_settings.is_active if monitor_settings else False

    last_scan_result = await db.execute(
        select(ScanLog).order_by(ScanLog.started_at.desc()).limit(1)
    )
    last_scan = last_scan_result.scalar_one_or_none()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "active_page": "dashboard",
        "stats": {
            "today_count": today_count,
            "week_count": week_count,
            "analyzed_count": analyzed_count,
            "bids_count": bids_count,
        },
        "recent_notices": recent_notices,
        "upcoming_deadlines": upcoming_deadlines,
        "monitor_active": monitor_active,
        "last_scan": last_scan,
    })
