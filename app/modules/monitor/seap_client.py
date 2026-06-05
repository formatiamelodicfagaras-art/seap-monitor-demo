import asyncio
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp
from loguru import logger

from app.config import get_settings

settings = get_settings()

BASE_URL = "https://e-licitatie.ro"
API_PUB = f"{BASE_URL}/api-pub"

HEADERS = {
    "Content-Type": "application/json;charset=UTF-8",
    "Referer": f"{BASE_URL}/pub/notices/contract-notices/list/0/0",
    "User-Agent": "SEAPMonitor/1.0",
}

# Notice type IDs from SEAP (verified against live API)
# Anunturi de participare (open tenders) — endpoint: GetCNoticeList
NOTICE_TYPES_PARTICIPARE = {
    2: "CN",      # Anunt de participare (Contract Notice)
    17: "SCN",    # Anunt simplificat / Procedura simplificata
    7: "RFQ",     # Invitatie de participare (Request for Quotation)
    6: "PC",      # Anunt de concesionare (Concession)
    12: "DC",     # Concurs de solutii (Design Contest)
    19: "LR",     # Licitatie restransa (Restricted Tender)
}

# Anunturi de atribuire (award notices) — endpoint: GetCANoticeList
NOTICE_TYPES_ATRIBUIRE = {
    3: "CAN",     # Anunt de atribuire CN
    13: "SCAN",   # Anunt de atribuire SCN
    18: "PCAN",   # Anunt de atribuire PC
    16: "RFQAN",  # Anunt de atribuire RFQ
    8: "DCAN",    # Anunt de atribuire DC
    20: "LRAN",   # Anunt de atribuire LR (Licitatie restransa)
}

# All notice types
ALL_NOTICE_TYPE_IDS = list(NOTICE_TYPES_PARTICIPARE.keys()) + list(NOTICE_TYPES_ATRIBUIRE.keys())
ALL_NOTICE_TYPES = {**NOTICE_TYPES_PARTICIPARE, **NOTICE_TYPES_ATRIBUIRE}

# Semaphore for rate limiting (1 request at a time)
_rate_semaphore = asyncio.Semaphore(1)
_last_request_time: float = 0


async def _rate_limited_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    **kwargs,
) -> dict | None:
    """Make a rate-limited request to SEAP API."""
    global _last_request_time

    async with _rate_semaphore:
        now = asyncio.get_event_loop().time()
        elapsed = now - _last_request_time
        if elapsed < settings.seap_request_delay:
            await asyncio.sleep(settings.seap_request_delay - elapsed)

        for attempt in range(1, settings.seap_max_retries + 1):
            try:
                async with session.request(method, url, headers=HEADERS, **kwargs) as resp:
                    _last_request_time = asyncio.get_event_loop().time()

                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", 30))
                        logger.warning(f"SEAP rate limited, waiting {retry_after}s")
                        await asyncio.sleep(retry_after)
                        continue
                    else:
                        text = await resp.text()
                        logger.warning(f"SEAP API {resp.status}: {text[:200]}")
                        if attempt < settings.seap_max_retries:
                            await asyncio.sleep(2 ** attempt)
                        continue

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"SEAP request error (attempt {attempt}): {e}")
                if attempt < settings.seap_max_retries:
                    await asyncio.sleep(2 ** attempt)

        logger.error(f"SEAP request failed after {settings.seap_max_retries} attempts: {url}")
        return None


async def get_notices(
    start_date: date,
    end_date: date,
    notice_type_ids: list[int] | None = None,
    page_index: int = 0,
    page_size: int = 100,
    endpoint: str = "GetCNoticeList",
) -> dict | None:
    """Fetch notices from SEAP.

    endpoint: 'GetCNoticeList' for participare, 'GetCANoticeList' for atribuire.
    """
    if notice_type_ids is None:
        notice_type_ids = ALL_NOTICE_TYPE_IDS

    payload = {
        "sysNoticeTypeIds": notice_type_ids,
        "sortProperties": [],
        "pageSize": page_size,
        "sysNoticeStateId": None,
        "contractingAuthorityId": None,
        "winnerId": None,
        "cPVCategoryId": None,
        "sysContractAssigmentTypeId": None,
        "cPVId": None,
        "assignedUserId": None,
        "sysAcquisitionContractTypeId": None,
        "pageIndex": page_index,
        "startPublicationDate": start_date.isoformat(),
        "endPublicationDate": end_date.isoformat(),
    }

    async with aiohttp.ClientSession() as session:
        return await _rate_limited_request(
            session, "POST",
            f"{API_PUB}/NoticeCommon/{endpoint}/",
            json=payload,
        )


async def get_all_notices_paginated(
    start_date: date,
    end_date: date,
    notice_type_ids: list[int] | None = None,
    max_pages: int = 50,
    endpoint: str = "GetCNoticeList",
) -> list[dict]:
    """Fetch ALL notices across all pages."""
    all_items = []
    page = 0

    while page < max_pages:
        data = await get_notices(
            start_date=start_date,
            end_date=end_date,
            notice_type_ids=notice_type_ids,
            page_index=page,
            page_size=100,
            endpoint=endpoint,
        )

        if not data or "items" not in data:
            break

        items = data["items"]
        if not items:
            break

        all_items.extend(items)
        total = data.get("total", 0)
        logger.info(f"SEAP page {page}: got {len(items)} items (total: {total}, fetched so far: {len(all_items)})")

        if len(all_items) >= total:
            break

        page += 1

    return all_items


async def get_direct_acquisition_list(
    start_date: date,
    end_date: date | None = None,
    page_index: int = 0,
    page_size: int = 100,
    cpv_code_id: int | None = None,
    use_finalization_date: bool = False,
    show_ongoing: bool = False,
) -> dict | None:
    """Fetch direct acquisitions from SEAP.

    use_finalization_date: if True, filter by finalizationDate instead of publicationDate.
    cpv_code_id: SEAP internal CPV ID (from ComboPub/searchCpvs).
    show_ongoing: if True, show only ongoing (active) acquisitions.
    """
    if end_date is None:
        end_date = start_date

    payload = {
        "pageSize": page_size,
        "showOngoingDa": show_ongoing,
        "cookieContext": None,
        "pageIndex": page_index,
        "sysDirectAcquisitionStateId": None,
        "cpvCodeId": cpv_code_id,
        "contractingAuthorityId": None,
        "supplierId": None,
        "cpvCategoryId": None,
    }

    if use_finalization_date:
        payload["publicationDateStart"] = None
        payload["publicationDateEnd"] = None
        payload["finalizationDateStart"] = start_date.isoformat()
        payload["finalizationDateEnd"] = end_date.isoformat()
    else:
        payload["publicationDateStart"] = start_date.isoformat()
        payload["publicationDateEnd"] = end_date.isoformat()

    async with aiohttp.ClientSession() as session:
        return await _rate_limited_request(
            session, "POST",
            f"{API_PUB}/DirectAcquisitionCommon/GetDirectAcquisitionList/",
            json=payload,
        )


async def resolve_cpv_code_id(cpv_code: str) -> int | None:
    """Resolve a CPV code (e.g. '79341000') to SEAP internal ID via ComboPub API."""
    async with aiohttp.ClientSession() as session:
        result = await _rate_limited_request(
            session, "GET",
            f"{API_PUB}/ComboPub/searchCpvs?filter={cpv_code}&pageIndex=0&pageSize=5&parentId=",
        )
        if result and result.get("items"):
            for item in result["items"]:
                # Match exact CPV prefix
                item_text = item.get("text", "")
                if item_text.startswith(cpv_code):
                    return item["id"]
            # Fallback: return first result
            return result["items"][0]["id"]
    return None


async def get_all_direct_acquisitions_paginated(
    start_date: date,
    end_date: date,
    max_pages: int = 50,
) -> list[dict]:
    """Fetch ALL direct acquisitions by scanning day-by-day to avoid 2000 limit."""
    all_items = []
    current = start_date

    while current <= end_date:
        page = 0
        while page < max_pages:
            data = await get_direct_acquisition_list(
                start_date=current,
                end_date=current,
                page_index=page,
                page_size=100,
            )

            if not data or "items" not in data:
                break

            items = data["items"]
            if not items:
                break

            all_items.extend(items)
            total = data.get("total", 0)

            if page == 0:
                logger.info(f"SEAP DA {current}: {total} total items")

            if len(items) < 100 or (page + 1) * 100 >= total:
                break

            page += 1

        current += timedelta(days=1)

    logger.info(f"SEAP DA total fetched: {len(all_items)} items ({start_date} to {end_date})")
    return all_items


async def get_adv_notice_list(
    start_date: date,
    end_date: date | None = None,
    page_index: int = 0,
    page_size: int = 100,
    cpv_code_id: int | None = None,
) -> dict | None:
    """Fetch ADV notices (anunturi publicitare achizitii directe) from SEAP.

    This uses the AdvNoticeCommon endpoint which returns ADV codes (ADV152xxxx)
    instead of the DirectAcquisition endpoint which returns DA codes.
    Supports cpvCodeId filtering.
    """
    if end_date is None:
        end_date = start_date

    payload = {
        "pageSize": page_size,
        "pageIndex": page_index,
        "sysAdvNoticeTypeId": None,
        "publicationDateStart": start_date.isoformat(),
        "publicationDateEnd": end_date.isoformat(),
        "cpvCodeId": cpv_code_id,
    }

    async with aiohttp.ClientSession() as session:
        return await _rate_limited_request(
            session, "POST",
            f"{API_PUB}/AdvNoticeCommon/GetAdvNoticeList/",
            json=payload,
        )


async def get_notice_details(ca_notice_id: str) -> dict | None:
    """Fetch detailed information about a specific notice."""
    async with aiohttp.ClientSession() as session:
        return await _rate_limited_request(
            session, "GET",
            f"{API_PUB}/C_PUBLIC_CANotice/get/{ca_notice_id}",
        )


async def download_file(file_url: str, dest_path: str) -> bool:
    """Download a file from SEAP."""
    global _last_request_time
    try:
        async with aiohttp.ClientSession() as session:
            async with _rate_semaphore:
                now = asyncio.get_event_loop().time()
                elapsed = now - _last_request_time
                if elapsed < settings.seap_request_delay:
                    await asyncio.sleep(settings.seap_request_delay - elapsed)

                async with session.get(file_url, headers=HEADERS) as resp:
                    _last_request_time = asyncio.get_event_loop().time()
                    if resp.status == 200:
                        content = await resp.read()
                        if len(content) > 50 * 1024 * 1024:
                            logger.warning(f"File too large ({len(content)} bytes): {file_url}")
                            return False

                        with open(dest_path, "wb") as f:
                            f.write(content)
                        logger.info(f"Downloaded: {dest_path}")
                        return True
                    else:
                        logger.warning(f"Download failed {resp.status}: {file_url}")
                        return False
    except Exception as e:
        logger.error(f"Download error: {e}")
        return False


def get_notice_type_label(sys_notice_type_id: int) -> str:
    """Get human-readable label for a notice type."""
    return ALL_NOTICE_TYPES.get(sys_notice_type_id, f"TIP-{sys_notice_type_id}")


def get_notice_type_category(sys_notice_type_id: int) -> str:
    """Get category: 'participare', 'atribuire', or 'achizitie_directa'."""
    if sys_notice_type_id in NOTICE_TYPES_PARTICIPARE:
        return "participare"
    elif sys_notice_type_id in NOTICE_TYPES_ATRIBUIRE:
        return "atribuire"
    return "altele"
