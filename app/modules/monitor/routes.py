from datetime import date, datetime, timedelta

from fastapi import APIRouter, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import (
    Notice, NoticeDocument, CPVCode, MonitoredAuthority,
    MonitorSettings, ScanLog,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ─── Settings ─────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    cpv_codes = (await db.execute(
        select(CPVCode).order_by(CPVCode.code)
    )).scalars().all()

    authorities = (await db.execute(
        select(MonitoredAuthority).order_by(MonitoredAuthority.name)
    )).scalars().all()

    monitor = await db.scalar(select(MonitorSettings).limit(1))

    # Recent scan logs
    scan_logs = (await db.execute(
        select(ScanLog).order_by(ScanLog.started_at.desc()).limit(10)
    )).scalars().all()

    return templates.TemplateResponse("monitor/settings.html", {
        "request": request,
        "active_page": "monitor_settings",
        "cpv_codes": cpv_codes,
        "authorities": authorities,
        "monitor": monitor,
        "scan_logs": scan_logs,
    })


@router.post("/settings/cpv/add")
async def add_cpv(
    request: Request,
    code: str = Form(...),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    code = code.strip()
    existing = await db.scalar(select(CPVCode).where(CPVCode.code == code))
    if not existing:
        db.add(CPVCode(code=code, description=description.strip(), is_active=True))
        await db.commit()
    return RedirectResponse(url="/monitor/settings", status_code=302)


@router.post("/settings/cpv/delete/{cpv_id}")
async def delete_cpv(cpv_id: int, db: AsyncSession = Depends(get_db)):
    cpv = await db.get(CPVCode, cpv_id)
    if cpv:
        await db.delete(cpv)
        await db.commit()
    return RedirectResponse(url="/monitor/settings", status_code=302)


@router.post("/settings/cpv/toggle/{cpv_id}")
async def toggle_cpv(cpv_id: int, db: AsyncSession = Depends(get_db)):
    cpv = await db.get(CPVCode, cpv_id)
    if cpv:
        cpv.is_active = not cpv.is_active
        await db.commit()
    return RedirectResponse(url="/monitor/settings", status_code=302)


@router.post("/settings/cpv/upload")
async def upload_cpv_excel(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Import CPV codes from an Excel file (columns: Cod CPV, Denumire)."""
    import re
    import openpyxl
    from io import BytesIO
    from loguru import logger

    content = await file.read()
    logger.info(f"CPV upload: file={file.filename}, size={len(content)} bytes")

    try:
        wb = openpyxl.load_workbook(BytesIO(content))
        ws = wb.active
    except Exception as e:
        logger.error(f"Failed to open Excel: {e}")
        return RedirectResponse(url="/monitor/settings", status_code=302)

    added = 0
    # CPV code pattern: 8 digits, optionally followed by dash and check digit
    cpv_re = re.compile(r'(\d{5,8}(?:-\d)?)')

    logger.info(f"Sheet: {ws.title}, max_row={ws.max_row}, max_col={ws.max_column}")

    for row in ws.iter_rows(values_only=True):
        if not row:
            continue

        # Log first few rows for debugging
        if added == 0:
            non_none = [c for c in row if c is not None]
            if non_none:
                logger.info(f"Sample row: {repr(row)[:200]}")

        # Scan every cell in the row for a CPV code
        code = None
        description = ""
        for i, cell in enumerate(row):
            if cell is None:
                continue
            cell_str = str(cell).strip()
            match = cpv_re.search(cell_str)
            if match and not code:
                code = match.group(1)
                # Grab description from the next non-empty cell
                for j in range(i + 1, len(row)):
                    if row[j] is not None and str(row[j]).strip():
                        description = str(row[j]).strip()
                        break
                break  # Found the code, stop scanning cells

        if not code:
            continue

        existing = await db.scalar(select(CPVCode).where(CPVCode.code == code))
        if not existing:
            db.add(CPVCode(code=code, description=description, is_active=True))
            added += 1

    wb.close()

    if added:
        await db.commit()
    logger.info(f"CPV upload complete: {added} codes added from {file.filename}")

    return RedirectResponse(url="/monitor/settings", status_code=302)


@router.post("/settings/authority/add")
async def add_authority(
    request: Request,
    seap_id: int = Form(...),
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.scalar(
        select(MonitoredAuthority).where(MonitoredAuthority.seap_id == seap_id)
    )
    if not existing:
        db.add(MonitoredAuthority(seap_id=seap_id, name=name.strip(), is_active=True))
        await db.commit()
    return RedirectResponse(url="/monitor/settings", status_code=302)


@router.post("/settings/authority/delete/{auth_id}")
async def delete_authority(auth_id: int, db: AsyncSession = Depends(get_db)):
    authority = await db.get(MonitoredAuthority, auth_id)
    if authority:
        await db.delete(authority)
        await db.commit()
    return RedirectResponse(url="/monitor/settings", status_code=302)


@router.post("/settings/monitor/save")
async def save_monitor_settings(
    request: Request,
    scan_interval_hours: int = Form(6),
    is_active: bool = Form(False),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    monitor = await db.scalar(select(MonitorSettings).limit(1))
    emails = []
    for i in range(5):
        val = form.get(f"notification_email_{i}", "").strip()
        if val:
            emails.append(val)

    if monitor:
        monitor.scan_interval_hours = scan_interval_hours
        monitor.notification_emails = emails
        monitor.is_active = is_active
    else:
        db.add(MonitorSettings(
            scan_interval_hours=scan_interval_hours,
            notification_emails=emails,
            is_active=is_active,
        ))

    await db.commit()
    return RedirectResponse(url="/monitor/settings", status_code=302)


# ─── Scan Now ─────────────────────────────────────────────────────────

@router.post("/scan-now")
async def trigger_scan(request: Request, db: AsyncSession = Depends(get_db)):
    import asyncio
    from app.modules.monitor.scanner import scan_notices, send_cpv_report
    # Run scan in background task so we don't block
    asyncio.create_task(_run_scan_and_email())
    if request.headers.get("HX-Request"):
        return HTMLResponse("<span>Scanare pornita... vei primi email cand se termina.</span>")
    return RedirectResponse(url="/monitor/settings", status_code=302)


async def _run_scan_and_email(db_ignored=None):
    """Background: scan + send email report."""
    from app.database import async_session
    from app.modules.monitor.scanner import scan_notices, send_cpv_report
    try:
        async with async_session() as db:
            results = await scan_notices(db)
            report_count = await send_cpv_report(db)
            logger.info(f"Scan+email done: {results['new_notices']} new, {report_count} matched sent")
    except Exception as e:
        logger.error(f"Scan+email error: {e}")


@router.post("/send-report")
async def send_report(request: Request, db: AsyncSession = Depends(get_db)):
    """Send email report with current CPV-matched notices (no re-scan)."""
    from app.modules.monitor.scanner import send_cpv_report
    report_count = await send_cpv_report(db)
    if request.headers.get("HX-Request"):
        if report_count:
            return HTMLResponse(f"<span>Email trimis cu {report_count} licitatii potrivite CPV!</span>")
        return HTMLResponse("<span>Nu sunt licitatii potrivite CPV de trimis.</span>")
    return RedirectResponse(url="/monitor/settings", status_code=302)


# ─── Notices List ─────────────────────────────────────────────────────

@router.get("/notices", response_class=HTMLResponse)
async def notices_list(
    request: Request,
    status: str | None = None,
    cpv: str | None = None,
    search: str | None = None,
    notice_type: str | None = None,
    page: int = 1,
    db: AsyncSession = Depends(get_db),
):
    page_size = 20
    query = select(Notice).options(selectinload(Notice.analysis))

    if status:
        query = query.where(Notice.status == status)
    if cpv:
        query = query.where(Notice.cpv_code.contains(cpv))
    if search:
        query = query.where(Notice.title.ilike(f"%{search}%"))
    if notice_type:
        query = query.where(Notice.notice_type == notice_type)

    # Count total
    count_query = select(func.count()).select_from(query.subquery())
    total = await db.scalar(count_query) or 0
    total_pages = max(1, (total + page_size - 1) // page_size)

    # Fetch page
    result = await db.execute(
        query
        .order_by(desc(Notice.publication_date), desc(Notice.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    notices = result.scalars().all()

    return templates.TemplateResponse("monitor/notices.html", {
        "request": request,
        "active_page": "notices",
        "notices": notices,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "status_filter": status,
        "cpv_filter": cpv,
        "search_filter": search,
        "notice_type_filter": notice_type,
        "now": datetime.now(),
    })


# ─── Notice Detail ────────────────────────────────────────────────────

@router.get("/notices/{notice_id}", response_class=HTMLResponse)
async def notice_detail(
    request: Request,
    notice_id: int,
    db: AsyncSession = Depends(get_db),
):
    notice = await db.get(
        Notice, notice_id,
        options=[selectinload(Notice.documents), selectinload(Notice.analysis)],
    )
    if not notice:
        return RedirectResponse(url="/monitor/notices", status_code=302)

    return templates.TemplateResponse("monitor/detail.html", {
        "request": request,
        "active_page": "notices",
        "notice": notice,
    })


@router.post("/notices/{notice_id}/download-docs")
async def download_docs(notice_id: int, db: AsyncSession = Depends(get_db)):
    from app.modules.monitor.scanner import download_notice_documents
    notice = await db.get(Notice, notice_id)
    if notice:
        await download_notice_documents(db, notice)
    return RedirectResponse(url=f"/monitor/notices/{notice_id}", status_code=302)


@router.post("/notices/{notice_id}/upload-docs")
async def upload_docs(
    notice_id: int,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Upload documents manually for a notice."""
    from app.services.file_service import get_downloads_path
    from loguru import logger
    import os

    notice = await db.get(Notice, notice_id)
    if not notice:
        return RedirectResponse(url="/monitor/notices", status_code=302)

    dest_dir = get_downloads_path(notice_id)
    uploaded = 0

    for file in files:
        if not file.filename:
            continue

        content = await file.read()
        if len(content) > 50 * 1024 * 1024:
            continue

        safe_name = file.filename.replace("..", "").replace("/", "_").replace("\\", "_")
        dest_path = str(dest_dir / safe_name)

        with open(dest_path, "wb") as f:
            f.write(content)

        ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else "unknown"

        doc = NoticeDocument(
            notice_id=notice_id,
            filename=safe_name,
            file_path=dest_path,
            file_type=ext,
            file_size=len(content),
            download_url="manual_upload",
            downloaded_at=datetime.utcnow(),
        )
        db.add(doc)
        uploaded += 1
        logger.info(f"Uploaded: {safe_name} ({len(content)} bytes) for notice {notice_id}")

    if uploaded:
        await db.commit()

    return RedirectResponse(url=f"/monitor/notices/{notice_id}", status_code=302)


@router.post("/notices/{notice_id}/archive")
async def archive_notice(notice_id: int, db: AsyncSession = Depends(get_db)):
    notice = await db.get(Notice, notice_id)
    if notice:
        notice.status = "archived"
        await db.commit()
    return RedirectResponse(url="/monitor/notices", status_code=302)
