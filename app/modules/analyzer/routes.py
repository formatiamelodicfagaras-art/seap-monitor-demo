from datetime import datetime

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, desc, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Notice, NoticeAnalysis, CPVCode, MonitoredAuthority

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _notice_matches_cpv(notice_cpv: str, cpv_prefixes: set[str]) -> bool:
    """Check if a notice CPV matches any configured CPV prefix."""
    if not notice_cpv:
        return False
    cpv_num = ""
    for ch in notice_cpv:
        if ch.isdigit():
            cpv_num += ch
        elif ch == "-" and len(cpv_num) >= 5:
            break
        elif ch == " ":
            break
    if not cpv_num:
        return False
    for prefix in cpv_prefixes:
        if cpv_num.startswith(prefix) or prefix.startswith(cpv_num):
            return True
    return False


@router.get("/", response_class=HTMLResponse)
async def analyzer_home(
    request: Request,
    db: AsyncSession = Depends(get_db),
    notice_type: str = "",
):
    """List CPV-matched notices grouped by day, plus completed analyses."""
    from datetime import timedelta
    from collections import OrderedDict

    # Completed analyses
    result = await db.execute(
        select(NoticeAnalysis)
        .options(selectinload(NoticeAnalysis.notice))
        .order_by(desc(NoticeAnalysis.analyzed_at))
    )
    analyses = result.scalars().all()

    # Load active CPV prefixes
    active_cpvs = (await db.execute(
        select(CPVCode.code).where(CPVCode.is_active == True)
    )).scalars().all()
    cpv_prefixes = set()
    for code in active_cpvs:
        prefix = code.split("-")[0] if "-" in code else code
        cpv_prefixes.add(prefix)

    # Get notices: participare with valid deadline + ADV from last 30 days
    now = datetime.now()
    participare_types = ["CN", "SCN", "RFQ", "PC", "DC", "LR"]
    participare_result = await db.execute(
        select(Notice)
        .options(selectinload(Notice.analysis))
        .where(Notice.notice_type.in_(participare_types))
        .where(Notice.status != "archived")
        .where(or_(Notice.deadline.is_(None), Notice.deadline >= now))
        .order_by(desc(Notice.publication_date))
    )
    adv_result = await db.execute(
        select(Notice)
        .options(selectinload(Notice.analysis))
        .where(Notice.notice_type == "ADV")
        .where(Notice.status != "archived")
        .order_by(desc(Notice.publication_date))
    )
    adv_all = list(adv_result.scalars().all())
    # Filter ADV: only active ones (deadline in future or no deadline)
    adv_finished_states = {"Oferta acceptata", "Oferta refuzata", "Conditii refuzate",
                           "Oferta neacceptata in termen", "Conditii neacceptate la termen"}
    adv_filtered = []
    for n in adv_all:
        # Skip if deadline passed
        if n.deadline and n.deadline < now:
            continue
        # Skip if known finished state
        rd = n.raw_data or {}
        state = rd.get("sysDirectAcquisitionState", {})
        state_text = state.get("text", "") if isinstance(state, dict) else ""
        if state_text in adv_finished_states:
            continue
        adv_filtered.append(n)
    all_notices = list(participare_result.scalars().all()) + adv_filtered

    # Load monitored authorities
    monitored_auths = (await db.execute(
        select(MonitoredAuthority.seap_id).where(MonitoredAuthority.is_active == True)
    )).scalars().all()
    auth_ids = set(str(a) for a in monitored_auths)

    # Filter by CPV OR monitored authority
    if cpv_prefixes or auth_ids:
        all_notices = [
            n for n in all_notices
            if _notice_matches_cpv(n.cpv_code or "", cpv_prefixes)
            or str(n.authority_seap_id) in auth_ids
        ]

    # Filter by notice_type if specified
    if notice_type:
        all_notices = [n for n in all_notices if n.notice_type == notice_type]

    # Group by month -> day
    months_grouped = OrderedDict()
    for n in sorted(all_notices, key=lambda x: x.publication_date or datetime.min.date(), reverse=True):
        if n.publication_date:
            month_key = n.publication_date.strftime("%Y-%m")
            day_key = n.publication_date.strftime("%Y-%m-%d")
        else:
            month_key = "necunoscut"
            day_key = "necunoscut"
        if month_key not in months_grouped:
            months_grouped[month_key] = OrderedDict()
        if day_key not in months_grouped[month_key]:
            months_grouped[month_key][day_key] = []
        months_grouped[month_key][day_key].append(n)

    # Collect available notice types for filter
    available_types = sorted(set(n.notice_type for n in all_notices if n.notice_type))

    return templates.TemplateResponse("analyzer/analysis.html", {
        "request": request,
        "active_page": "analyzer",
        "analyses": analyses,
        "months_grouped": months_grouped,
        "total_notices": len(all_notices),
        "notice_type_filter": notice_type,
        "available_types": available_types,
    })


@router.get("/download-excel", response_class=HTMLResponse)
async def download_excel(request: Request, db: AsyncSession = Depends(get_db)):
    """Download all active CPV-matched notices as Excel."""
    from datetime import timedelta
    from fastapi.responses import Response

    # Load CPV prefixes
    active_cpvs = (await db.execute(
        select(CPVCode.code).where(CPVCode.is_active == True)
    )).scalars().all()
    cpv_prefixes = set()
    for code in active_cpvs:
        cpv_prefixes.add(code.split("-")[0] if "-" in code else code)

    now = datetime.now()
    participare_types = ["CN", "SCN", "RFQ", "PC", "DC", "LR"]
    participare = (await db.execute(
        select(Notice)
        .where(Notice.notice_type.in_(participare_types))
        .where(Notice.status != "archived")
        .where(or_(Notice.deadline.is_(None), Notice.deadline >= now))
    )).scalars().all()

    adv_all = (await db.execute(
        select(Notice)
        .where(Notice.notice_type == "ADV")
        .where(Notice.status != "archived")
    )).scalars().all()

    adv_finished_states = {"Oferta acceptata", "Oferta refuzata", "Conditii refuzate",
                           "Oferta neacceptata in termen", "Conditii neacceptate la termen"}
    adv_filtered = []
    for n in adv_all:
        if n.deadline and n.deadline < now:
            continue
        rd = n.raw_data or {}
        state = rd.get("sysDirectAcquisitionState", {})
        state_text = state.get("text", "") if isinstance(state, dict) else ""
        if state_text not in adv_finished_states:
            adv_filtered.append(n)

    all_notices = list(participare) + adv_filtered

    # Load monitored authorities
    monitored_auths = (await db.execute(
        select(MonitoredAuthority.seap_id).where(MonitoredAuthority.is_active == True)
    )).scalars().all()
    auth_ids = set(str(a) for a in monitored_auths)

    if cpv_prefixes or auth_ids:
        all_notices = [
            n for n in all_notices
            if _notice_matches_cpv(n.cpv_code or "", cpv_prefixes)
            or str(n.authority_seap_id) in auth_ids
        ]

    # Build Excel using same format as email
    from app.services.email_service import build_notices_excel

    notice_dicts = []
    for n in sorted(all_notices, key=lambda x: x.publication_date or datetime.min.date(), reverse=True):
        rd = n.raw_data or {}
        notice_dicts.append({
            "notice_no": rd.get("noticeNo") or rd.get("uniqueIdentificationCode") or "",
            "notice_type": n.notice_type,
            "title": n.title or "",
            "contracting_authority": n.contracting_authority or "",
            "cpv_code": n.cpv_code or "",
            "estimated_value": str(n.estimated_value) if n.estimated_value else "",
            "publication_date": str(n.publication_date) if n.publication_date else "",
            "deadline": str(n.deadline) if n.deadline else "",
            "seap_url": n.seap_url or "",
        })

    excel_bytes = build_notices_excel(notice_dicts)
    today_str = datetime.now().strftime("%Y-%m-%d")
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="licitatii_active_SEAP_{today_str}.xlsx"'},
    )


@router.post("/analyze/{notice_id}")
async def start_analysis(notice_id: int, db: AsyncSession = Depends(get_db)):
    """Trigger AI analysis for a notice."""
    notice = await db.get(Notice, notice_id)
    if not notice:
        return RedirectResponse(url="/analyzer/", status_code=302)

    from app.modules.analyzer.tasks import run_analysis
    await run_analysis(notice_id)

    return RedirectResponse(url=f"/analyzer/view/{notice_id}", status_code=302)


@router.post("/delete/{analysis_id}")
async def delete_analysis(analysis_id: int, db: AsyncSession = Depends(get_db)):
    """Delete an analysis."""
    analysis = await db.get(NoticeAnalysis, analysis_id)
    if analysis:
        # Reset notice status back to "new"
        notice = await db.get(Notice, analysis.notice_id)
        if notice and notice.status == "analyzed":
            notice.status = "new"
        await db.delete(analysis)
        await db.commit()
    return RedirectResponse(url="/analyzer/", status_code=302)


@router.get("/view/{notice_id}", response_class=HTMLResponse)
async def view_analysis(
    request: Request,
    notice_id: int,
    db: AsyncSession = Depends(get_db),
):
    """View the analysis result for a notice."""
    notice = await db.get(
        Notice, notice_id,
        options=[selectinload(Notice.analysis), selectinload(Notice.documents)],
    )
    if not notice:
        return RedirectResponse(url="/analyzer/", status_code=302)

    analysis = notice.analysis

    return templates.TemplateResponse("analyzer/compare.html", {
        "request": request,
        "active_page": "analyzer",
        "notice": notice,
        "analysis": analysis,
    })
