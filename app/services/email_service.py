import aiohttp
import base64
from datetime import date
from io import BytesIO

from loguru import logger

from app.config import get_settings

settings = get_settings()

# Microsoft Graph API token cache
_token_cache: dict = {"access_token": None, "expires_at": 0}


async def _get_graph_token() -> str | None:
    """Get an OAuth2 token for Microsoft Graph API using client credentials."""
    import time

    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["access_token"]

    url = f"https://login.microsoftonline.com/{settings.ms_tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": settings.ms_client_id,
        "client_secret": settings.ms_client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    _token_cache["access_token"] = result["access_token"]
                    _token_cache["expires_at"] = time.time() + result.get("expires_in", 3600)
                    return result["access_token"]
                else:
                    text = await resp.text()
                    logger.error(f"Failed to get Graph token: {resp.status} {text}")
                    return None
    except Exception as e:
        logger.error(f"Graph token error: {e}")
        return None


async def send_email(
    to_addresses: list[str],
    subject: str,
    html_body: str,
    attachments: list[dict] | None = None,
):
    """Send email via Microsoft Graph API.

    attachments: list of {"name": "file.xlsx", "content_type": "application/...", "content_bytes": b"..."}
    """
    if not settings.ms_client_id or not settings.ms_client_secret or not settings.mail_from:
        logger.warning("Microsoft Graph not configured, skipping email send")
        return

    token = await _get_graph_token()
    if not token:
        logger.error("Could not obtain Graph API token, skipping email")
        return

    url = f"https://graph.microsoft.com/v1.0/users/{settings.mail_from}/sendMail"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    message = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": html_body,
            },
            "toRecipients": [
                {"emailAddress": {"address": addr}} for addr in to_addresses
            ],
        },
        "saveToSentItems": "false",
    }

    if attachments:
        message["message"]["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": att["name"],
                "contentType": att["content_type"],
                "contentBytes": base64.b64encode(att["content_bytes"]).decode("utf-8"),
            }
            for att in attachments
        ]

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=message) as resp:
                if resp.status == 202:
                    logger.info(f"Email sent to {to_addresses}: {subject}")
                else:
                    text = await resp.text()
                    logger.error(f"Graph sendMail failed: {resp.status} {text}")
                    raise Exception(f"Graph API error {resp.status}: {text}")
    except aiohttp.ClientError as e:
        logger.error(f"Failed to send email: {e}")
        raise


def build_notices_excel(notices: list[dict]) -> bytes:
    """Build a styled Excel file matching the ForFuture format."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side, numbers

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Licitatii SEAP"

    # Styles
    header_font = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
    cell_font = Font(name="Calibri", size=10)
    cell_font_link = Font(name="Calibri", size=9, color="2563EB", underline="single")
    thin_border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9"),
    )
    even_fill = PatternFill(start_color="F2F7FB", end_color="F2F7FB", fill_type="solid")
    odd_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")

    headers = [
        "Numar anunt",
        "Autoritate contractanta",
        "Denumire contract",
        "Cod si denumire CPV",
        "Data publicare",
        "Tip procedura",
        "Data limita depunere",
        "Valoare estimata",
        "Link SEAP",
    ]
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = thin_border
    ws.row_dimensions[1].height = 30

    for row_idx, n in enumerate(notices, 2):
        notice_no = n.get("notice_no", "")
        tip = n.get("notice_type", "")
        if tip == "ADV":
            tip_proc = "Achizitie directa"
        elif tip == "CN":
            tip_proc = "Licitatie deschisa"
        elif tip == "SCN":
            tip_proc = "Procedura simplificata"
        elif tip == "RFQ":
            tip_proc = "Cerere de oferte"
        else:
            tip_proc = tip

        val = n.get("estimated_value", "")
        val_str = ""
        if val:
            try:
                val_str = f"{float(val):,.2f} RON"
            except (ValueError, TypeError):
                val_str = str(val)

        seap_url = n.get("seap_url", "")

        values = [
            notice_no,
            n.get("contracting_authority", ""),
            n.get("title", ""),
            n.get("cpv_code", ""),
            str(n.get("publication_date", ""))[:10] if n.get("publication_date") else "",
            tip_proc,
            str(n.get("deadline", ""))[:16] if n.get("deadline") else "",
            val_str,
            seap_url,
        ]

        row_fill = even_fill if row_idx % 2 == 0 else odd_fill
        for col, val in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col, value=val)
            cell.border = thin_border
            cell.fill = row_fill
            cell.alignment = Alignment(vertical="center", wrap_text=(col in (2, 3, 4)))
            if col == 9 and seap_url:
                cell.font = cell_font_link
                cell.hyperlink = seap_url
                cell.value = "Acceseza"
            elif col == 8:
                cell.font = cell_font
                cell.alignment = Alignment(horizontal="right", vertical="center")
            else:
                cell.font = cell_font

        ws.row_dimensions[row_idx].height = 28

    # Column widths
    col_widths = [18, 35, 45, 38, 14, 22, 18, 18, 14]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w

    # Freeze header row
    ws.freeze_panes = "A2"

    # Auto-filter
    ws.auto_filter.ref = f"A1:I{len(notices) + 1}"

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


def build_new_notices_email(notices: list[dict]) -> tuple[str, str]:
    """Build email for newly found notices (after scan)."""
    today = date.today().strftime("%d.%m.%Y")
    subject = f"[SEAP Monitor] {len(notices)} licitatii noi gasite — {today}"
    html_body = _build_notices_html_table(
        notices,
        title="Licitatii noi potrivite CPV-urilor tale",
        subtitle=f"Au fost gasite <strong>{len(notices)}</strong> licitatii noi care corespund codurilor CPV monitorizate.",
    )
    return subject, html_body


def build_cpv_report_email(notices: list[dict]) -> tuple[str, str]:
    """Build daily report email with ALL active CPV-matched notices."""
    today = date.today().strftime("%d.%m.%Y")
    subject = f"[SEAP Monitor] Raport zilnic — {len(notices)} licitatii active — {today}"
    html_body = _build_notices_html_table(
        notices,
        title=f"Raport licitatii active — {today}",
        subtitle=f"Sunt <strong>{len(notices)}</strong> licitatii active care corespund codurilor CPV monitorizate.",
    )
    return subject, html_body


def _build_notices_html_table(notices: list[dict], title: str, subtitle: str) -> str:
    """Build HTML email with notices table matching ForFuture format."""
    th = "padding: 6px 8px; border: 1px solid #ccc; text-align: left; font-size: 12px;"
    td = "padding: 6px 8px; border: 1px solid #ddd; font-size: 12px;"

    rows = ""
    for n in notices:
        notice_no = n.get("notice_no", "")
        tip = n.get("notice_type", "")
        if tip == "ADV":
            tip_proc = "Achizitie directa"
        elif tip == "CN":
            tip_proc = "Licitatie deschisa"
        elif tip == "SCN":
            tip_proc = "Procedura simplificata"
        elif tip == "RFQ":
            tip_proc = "Cerere de oferte"
        else:
            tip_proc = tip

        val = n.get("estimated_value", "")
        val_str = ""
        if val:
            try:
                val_str = f"{float(val):,.2f} RON"
            except (ValueError, TypeError):
                val_str = str(val)

        pub_date = str(n.get("publication_date", ""))[:16] if n.get("publication_date") else ""
        deadline = str(n.get("deadline", ""))[:16] if n.get("deadline") else ""
        seap_url = n.get("seap_url", "#")

        rows += f"""
        <tr>
            <td style="{td}"><a href="{seap_url}" style="color: #2563eb;">{notice_no}</a></td>
            <td style="{td}">{n.get('contracting_authority', '')}</td>
            <td style="{td}">{n.get('title', '')}</td>
            <td style="{td}">{n.get('cpv_code', '')}</td>
            <td style="{td}">{pub_date}</td>
            <td style="{td}">{tip_proc}</td>
            <td style="{td}">{deadline}</td>
            <td style="{td} text-align: right; white-space: nowrap;">{val_str}</td>
        </tr>
        """

    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333;">
        <h2 style="color: #1e3a5f;">{title}</h2>
        <p>{subtitle}</p>
        <p><em>Fisierul Excel cu toate detaliile este atasat.</em></p>
        <table style="border-collapse: collapse; width: 100%; font-size: 12px;">
            <thead>
                <tr style="background: #2563eb; color: white;">
                    <th style="{th} color: white;">Numar anunt</th>
                    <th style="{th} color: white;">Autoritate contractanta</th>
                    <th style="{th} color: white;">Denumire contract</th>
                    <th style="{th} color: white;">Cod si denumire CPV</th>
                    <th style="{th} color: white;">Data publicare</th>
                    <th style="{th} color: white;">Tip procedura</th>
                    <th style="{th} color: white;">Data limita depunere</th>
                    <th style="{th} color: white;">Valoare estimata</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
        <p style="margin-top: 20px; color: #888; font-size: 11px;">
            Trimis automat de SEAP Monitor — <a href="{settings.app_url}">{settings.app_url}</a>
        </p>
    </body>
    </html>
    """
