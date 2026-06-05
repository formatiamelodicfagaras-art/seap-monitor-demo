from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loguru import logger

from app.models import Notice, NoticeDocument, CPVCode, MonitoredAuthority, MonitorSettings, ScanLog
from app.modules.monitor import seap_client
from app.services.file_service import get_downloads_path


async def scan_notices(db: AsyncSession, days_back: int = 90) -> dict:
    """Scan ALL notices from SEAP for the given period."""
    results = {"total_found": 0, "new_notices": 0, "matched_notices": 0, "errors": []}

    today = date.today()
    start = today - timedelta(days=days_back)

    # Load active CPV codes for filtering
    active_cpvs = (await db.execute(
        select(CPVCode.code).where(CPVCode.is_active == True)
    )).scalars().all()
    # Extract just the numeric prefix (e.g., "79341000" from "79341000-6")
    cpv_prefixes = set()
    for code in active_cpvs:
        prefix = code.split("-")[0] if "-" in code else code
        cpv_prefixes.add(prefix)

    logger.info(f"Active CPV prefixes for filtering: {len(cpv_prefixes)}")

    all_new_notices = []

    # ── 1. Anunturi de participare (licitatii deschise) ──────────────
    scan_log = ScanLog(
        scan_type="participare",
        filter_value=f"toate ({days_back} zile)",
        started_at=datetime.utcnow(),
    )
    db.add(scan_log)

    try:
        items = await seap_client.get_all_notices_paginated(
            start_date=start,
            end_date=today,
            notice_type_ids=list(seap_client.NOTICE_TYPES_PARTICIPARE.keys()),
            endpoint="GetCNoticeList",
        )
        scan_log.notices_found = len(items)
        new_notices = await _process_notices(db, items, "participare")
        scan_log.new_notices = len(new_notices)
        results["total_found"] += len(items)
        results["new_notices"] += len(new_notices)
        all_new_notices.extend(new_notices)
        scan_log.status = "success"
        scan_log.finished_at = datetime.utcnow()
        logger.info(f"Participare scan: {len(items)} found, {len(new_notices)} new")
    except Exception as e:
        logger.error(f"Error scanning participare: {e}")
        scan_log.status = "error"
        scan_log.error_message = str(e)
        scan_log.finished_at = datetime.utcnow()
        results["errors"].append(str(e))

    await db.commit()

    # ── 2. Anunturi de atribuire ─────────────────────────────────────
    scan_log = ScanLog(
        scan_type="atribuire",
        filter_value=f"toate ({days_back} zile)",
        started_at=datetime.utcnow(),
    )
    db.add(scan_log)

    try:
        items = await seap_client.get_all_notices_paginated(
            start_date=start,
            end_date=today,
            notice_type_ids=list(seap_client.NOTICE_TYPES_ATRIBUIRE.keys()),
            endpoint="GetCANoticeList",
        )
        scan_log.notices_found = len(items)
        new_notices = await _process_notices(db, items, "atribuire")
        scan_log.new_notices = len(new_notices)
        results["total_found"] += len(items)
        results["new_notices"] += len(new_notices)
        all_new_notices.extend(new_notices)
        scan_log.status = "success"
        scan_log.finished_at = datetime.utcnow()
        logger.info(f"Atribuire scan: {len(items)} found, {len(new_notices)} new")
    except Exception as e:
        logger.error(f"Error scanning atribuire: {e}")
        scan_log.status = "error"
        scan_log.error_message = str(e)
        scan_log.finished_at = datetime.utcnow()
        results["errors"].append(str(e))

    await db.commit()

    # ── 3. Anunturi ADV (achizitii directe) — AdvNoticeCommon endpoint ────
    adv_days = min(days_back, 30)
    adv_start = today - timedelta(days=adv_days)
    scan_log = ScanLog(
        scan_type="achizitie_directa",
        filter_value=f"CPV filtrat ({adv_days} zile)",
        started_at=datetime.utcnow(),
    )
    db.add(scan_log)

    try:
        total_scanned = 0
        adv_all_items = []

        # Resolve CPV codes to SEAP internal IDs
        cpv_id_map = {}
        for prefix in cpv_prefixes:
            cpv_id = await seap_client.resolve_cpv_code_id(prefix)
            if cpv_id:
                cpv_id_map[prefix] = cpv_id
                logger.info(f"CPV {prefix} -> SEAP ID {cpv_id}")

        # Query ADV per CPV code using AdvNoticeCommon endpoint
        for prefix, cpv_id in cpv_id_map.items():
            page = 0
            while page < 30:
                data = await seap_client.get_adv_notice_list(
                    start_date=adv_start,
                    end_date=today,
                    page_index=page,
                    page_size=100,
                    cpv_code_id=cpv_id,
                )
                if not data or "items" not in data:
                    break
                items = data["items"]
                if not items:
                    break
                adv_all_items.extend(items)
                total_scanned += len(items)
                total = data.get("total", 0)
                if page == 0 and total > 0:
                    logger.info(f"ADV CPV {prefix} (id={cpv_id}): {total} total")
                if len(items) < 100 or (page + 1) * 100 >= total:
                    break
                page += 1

        scan_log.notices_found = total_scanned
        new_notices = await _process_notices(db, adv_all_items, "achizitie_directa")
        scan_log.new_notices = len(new_notices)
        results["total_found"] += total_scanned
        results["new_notices"] += len(new_notices)
        all_new_notices.extend(new_notices)
        scan_log.status = "success"
        scan_log.finished_at = datetime.utcnow()
        logger.info(f"ADV scan: {total_scanned} found across {len(cpv_id_map)} CPVs, {len(new_notices)} new saved")
    except Exception as e:
        logger.error(f"Error scanning ADV notices: {e}")
        scan_log.status = "error"
        scan_log.error_message = str(e)
        scan_log.finished_at = datetime.utcnow()
        results["errors"].append(str(e))

    await db.commit()

    # ── 4. Cleanup expired notices ──────────────────────────────────
    expired_count = await _cleanup_expired(db)
    if expired_count:
        logger.info(f"Cleanup: archived {expired_count} expired notices")

    # ── 5. Filter by CPV and send notifications ─────────────────────
    if all_new_notices and cpv_prefixes:
        matched = _filter_by_cpv(all_new_notices, cpv_prefixes)
        results["matched_notices"] = len(matched)
        logger.info(f"CPV filter: {len(matched)} matched out of {len(all_new_notices)} new notices")

        if matched:
            await _send_notification(db, matched)

    return results


async def _cleanup_expired(db: AsyncSession) -> int:
    """Archive notices with expired deadlines and finalized ADVs."""
    now = datetime.utcnow()
    count = 0

    # Archive participare notices with passed deadline
    expired = (await db.execute(
        select(Notice)
        .where(Notice.notice_type.in_(["CN", "SCN", "RFQ", "PC", "DC", "LR"]))
        .where(Notice.status == "new")
        .where(Notice.deadline != None)
        .where(Notice.deadline < now)
    )).scalars().all()

    for n in expired:
        n.status = "archived"
        count += 1

    # Archive ADV notices with expired deadlines only
    advs = (await db.execute(
        select(Notice)
        .where(Notice.notice_type == "ADV")
        .where(Notice.status == "new")
        .where(Notice.deadline != None)
        .where(Notice.deadline < now)
    )).scalars().all()

    for n in advs:
        n.status = "archived"
        count += 1

    if count:
        await db.commit()

    return count


def _filter_by_cpv(notices: list[dict], cpv_prefixes: set[str]) -> list[dict]:
    """Filter notices that match any of the configured CPV codes."""
    matched = []
    for n in notices:
        notice_cpv = n.get("cpv_code", "")
        if not notice_cpv:
            continue
        # Extract numeric prefix from notice CPV (e.g., "79341000" from "79341000-6 - Servicii de publicitate")
        cpv_num = ""
        for ch in notice_cpv:
            if ch.isdigit():
                cpv_num += ch
            elif ch == "-" and len(cpv_num) >= 5:
                break
            elif ch == " ":
                break
        if not cpv_num:
            continue
        # Match: check if any configured CPV prefix matches
        for prefix in cpv_prefixes:
            # Match if the notice CPV starts with the configured prefix
            # or the configured prefix starts with the notice CPV (hierarchical match)
            if cpv_num.startswith(prefix) or prefix.startswith(cpv_num):
                matched.append(n)
                break
    return matched


async def _send_notification(db: AsyncSession, matched_notices: list[dict]):
    """Send email notification with matched notices + Excel attachment."""
    from app.services.email_service import send_email, build_new_notices_email, build_notices_excel

    # Get notification settings
    monitor = await db.scalar(select(MonitorSettings).limit(1))
    if not monitor or not monitor.notification_emails:
        logger.info("No notification emails configured, skipping")
        return

    emails = monitor.notification_emails
    if not emails:
        return

    # Build email
    subject, html_body = build_new_notices_email(matched_notices)

    # Build Excel attachment
    try:
        excel_bytes = build_notices_excel(matched_notices)
        today_str = date.today().strftime("%Y-%m-%d")
        attachments = [{
            "name": f"licitatii_SEAP_{today_str}.xlsx",
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "content_bytes": excel_bytes,
        }]
    except Exception as e:
        logger.error(f"Failed to build Excel attachment: {e}")
        attachments = None

    try:
        await send_email(emails, subject, html_body, attachments=attachments)
        logger.info(f"Notification sent to {emails}: {len(matched_notices)} matched notices")
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")


async def send_cpv_report(db: AsyncSession):
    """Send email report with ALL active CPV-matched notices (not just new ones)."""
    from sqlalchemy import or_
    from app.services.email_service import send_email, build_cpv_report_email, build_notices_excel

    # Load CPV codes
    active_cpvs = (await db.execute(
        select(CPVCode.code).where(CPVCode.is_active == True)
    )).scalars().all()
    cpv_prefixes = set()
    for code in active_cpvs:
        prefix = code.split("-")[0] if "-" in code else code
        cpv_prefixes.add(prefix)

    # Load monitored authorities
    monitored_auths = (await db.execute(
        select(MonitoredAuthority.seap_id).where(MonitoredAuthority.is_active == True)
    )).scalars().all()
    auth_ids = set(str(a) for a in monitored_auths)

    if not cpv_prefixes and not auth_ids:
        return 0

    # Get notices from last 24h only (for daily email report)
    now = datetime.utcnow()
    last_24h = (now - timedelta(hours=24))
    notices = (await db.execute(
        select(Notice)
        .where(Notice.status != "archived")
        .where(Notice.created_at >= last_24h)
    )).scalars().all()

    # Filter by CPV OR monitored authority
    matched = []
    for n in notices:
        # Check authority match
        if str(n.authority_seap_id) in auth_ids:
            rd = n.raw_data or {}
            notice_no = rd.get("noticeNo") or rd.get("uniqueIdentificationCode") or ""
            matched.append({
                "notice_no": notice_no,
                "notice_type": n.notice_type,
                "title": n.title,
                "contracting_authority": n.contracting_authority,
                "cpv_code": n.cpv_code,
                "estimated_value": str(n.estimated_value) if n.estimated_value else "",
                "currency": n.currency or "RON",
                "deadline": str(n.deadline) if n.deadline else "",
                "publication_date": str(n.publication_date) if n.publication_date else "",
                "seap_url": n.seap_url or "",
                "id": n.id,
            })
            continue

        cpv = n.cpv_code or ""
        cpv_num = ""
        for ch in cpv:
            if ch.isdigit():
                cpv_num += ch
            elif ch == "-" and len(cpv_num) >= 5:
                break
            elif ch == " ":
                break
        if not cpv_num:
            continue
        for prefix in cpv_prefixes:
            if cpv_num.startswith(prefix) or prefix.startswith(cpv_num):
                rd = n.raw_data or {}
                notice_no = rd.get("noticeNo") or rd.get("uniqueIdentificationCode") or ""
                matched.append({
                    "notice_no": notice_no,
                    "notice_type": n.notice_type,
                    "title": n.title,
                    "contracting_authority": n.contracting_authority,
                    "cpv_code": n.cpv_code,
                    "estimated_value": str(n.estimated_value) if n.estimated_value else "",
                    "currency": n.currency or "RON",
                    "deadline": str(n.deadline) if n.deadline else "",
                    "publication_date": str(n.publication_date) if n.publication_date else "",
                    "seap_url": n.seap_url or "",
                    "id": n.id,
                })
                break

    if not matched:
        logger.info("No CPV-matched notices to report")
        return 0

    # Send
    monitor = await db.scalar(select(MonitorSettings).limit(1))
    if not monitor or not monitor.notification_emails:
        return 0

    subject, html_body = build_cpv_report_email(matched)
    try:
        excel_bytes = build_notices_excel(matched)
        today_str = date.today().strftime("%Y-%m-%d")
        attachments = [{
            "name": f"licitatii_active_SEAP_{today_str}.xlsx",
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "content_bytes": excel_bytes,
        }]
    except Exception as e:
        logger.error(f"Failed to build Excel: {e}")
        attachments = None

    await send_email(monitor.notification_emails, subject, html_body, attachments=attachments)
    logger.info(f"CPV report sent: {len(matched)} notices to {monitor.notification_emails}")
    return len(matched)


async def _process_notices(
    db: AsyncSession,
    raw_notices: list[dict],
    category: str,  # "participare" / "atribuire"
) -> list[dict]:
    """Process raw notice data from SEAP API and save new ones to DB.

    Returns list of dicts with notice data for the newly added notices.
    """
    new_notices = []

    for raw in raw_notices:
        seap_id = str(raw.get("caNoticeId") or raw.get("cNoticeId") or raw.get("noticeId") or raw.get("advNoticeId") or raw.get("directAcquisitionId") or raw.get("id", ""))
        if not seap_id:
            continue

        # Check if already exists
        existing = await db.scalar(
            select(Notice.id).where(Notice.seap_id == seap_id)
        )
        if existing:
            continue

        # Determine notice type label (CN, SCN, RFQ, ADV, etc.)
        if category == "achizitie_directa":
            type_label = "ADV"
        else:
            sys_type_id = raw.get("sysNoticeTypeId")
            type_label = seap_client.get_notice_type_label(sys_type_id) if sys_type_id else category

        # Procedure type
        proc_type = ""
        pt = raw.get("sysProcedureType")
        if isinstance(pt, dict):
            proc_type = pt.get("text", "")
        elif isinstance(pt, str):
            proc_type = pt

        # Contract type (Furnizare/Servicii/Lucrari)
        contract_type = ""
        ct = raw.get("sysAcquisitionContractType") or raw.get("sysAdvertisingNotice")
        if isinstance(ct, dict):
            contract_type = ct.get("text", "")
        elif isinstance(ct, str):
            contract_type = ct

        cpv_code = raw.get("cpvCodeAndName") or raw.get("cpvCode") or ""
        est_value = raw.get("ronContractValue") or raw.get("estimatedValueRon") or raw.get("estimatedValue") or raw.get("closingValue") or None
        pub_date = _parse_date(raw.get("noticeStateDate") or raw.get("publicationDate"))
        deadline = _parse_datetime(
            raw.get("tenderReceiptDeadline")
            or raw.get("minTenderReceiptDeadline")
            or raw.get("maxTenderReceiptDeadline")
            or raw.get("caDecisionDeadline")
        )
        notice_no = raw.get("noticeNo") or raw.get("uniqueIdentificationCode") or ""
        seap_url = _build_seap_url(seap_id, notice_no, category, type_label)
        title = raw.get("contractTitle") or raw.get("contractObject") or raw.get("directAcquisitionName") or "N/A"
        authority = raw.get("contractingAuthorityNameAndFN") or raw.get("caName") or raw.get("contractingAuthority") or ""

        # Ensure noticeNo is in raw_data for template display
        if "noticeNo" not in raw and notice_no:
            raw["noticeNo"] = notice_no

        notice = Notice(
            seap_id=seap_id,
            notice_type=type_label,
            title=title,
            contracting_authority=authority,
            authority_seap_id=raw.get("contractingAuthorityId"),
            cpv_code=cpv_code,
            estimated_value=est_value,
            currency=raw.get("currencyCode") or "RON",
            publication_date=pub_date,
            deadline=deadline,
            seap_url=seap_url,
            raw_data=raw,
            status="new",
        )
        db.add(notice)

        # Collect data for notification
        new_notices.append({
            "id": None,
            "seap_id": seap_id,
            "notice_no": notice_no,
            "notice_type": type_label,
            "title": title,
            "contracting_authority": authority,
            "cpv_code": cpv_code,
            "estimated_value": str(est_value) if est_value else "",
            "currency": raw.get("currencyCode") or "RON",
            "publication_date": str(pub_date) if pub_date else "",
            "deadline": str(deadline) if deadline else "",
            "seap_url": seap_url,
        })

        # Commit in batches of 50
        if len(new_notices) % 50 == 0:
            await db.flush()

    if new_notices:
        await db.flush()

    return new_notices


async def download_notice_documents(db: AsyncSession, notice: Notice) -> int:
    """Download all documents attached to a notice."""
    if not notice.raw_data:
        details = await seap_client.get_notice_details(notice.seap_id)
        if details:
            notice.raw_data = details
            await db.commit()

    docs_data = []
    if notice.raw_data:
        for key in ["caDocuments", "documents", "noticeDocuments", "documentList"]:
            if key in notice.raw_data:
                docs_data = notice.raw_data[key]
                break

    downloaded = 0
    for doc in docs_data:
        filename = doc.get("fileName") or doc.get("name") or "document"
        download_url = doc.get("downloadUrl") or doc.get("url") or ""

        if not download_url:
            continue

        if download_url.startswith("/"):
            download_url = f"https://sicap-prod.e-licitatie.ro{download_url}"

        dest_dir = get_downloads_path(notice.id)
        dest_path = str(dest_dir / filename)

        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "unknown"

        notice_doc = NoticeDocument(
            notice_id=notice.id,
            filename=filename,
            file_path=dest_path,
            file_type=ext,
            download_url=download_url,
        )

        success = await seap_client.download_file(download_url, dest_path)
        if success:
            import os
            notice_doc.file_size = os.path.getsize(dest_path)
            notice_doc.downloaded_at = datetime.utcnow()
            downloaded += 1

        db.add(notice_doc)

    await db.commit()
    return downloaded


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, str):
        # Handle timezone offset like +02:00
        clean = value.split("+")[0].split("Z")[0]
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d.%m.%Y"):
            try:
                return datetime.strptime(clean.split(".")[0] if "T" in clean else clean, fmt).date()
            except ValueError:
                continue
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, str):
        clean = value.split("+")[0].split("Z")[0]
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(clean, fmt)
            except ValueError:
                continue
    return None


def _build_seap_url(seap_id: str, notice_no: str = "", category: str = "", notice_type: str = "") -> str:
    if category == "achizitie_directa" or notice_type == "ADV":
        return f"https://e-licitatie.ro/pub/adv-notices/view/{seap_id}"
    if category == "participare" or notice_type in ("CN", "SCN", "RFQ", "PC", "DC", "LR"):
        return f"https://e-licitatie.ro/pub/notices/c-notice/v2/view/{seap_id}"
    return f"https://e-licitatie.ro/pub/notices/ca-notices/view/{seap_id}"
