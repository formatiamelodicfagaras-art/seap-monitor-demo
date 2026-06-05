import zipfile
from datetime import datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models import (
    Notice, NoticeAnalysis, BidTemplate, CompanyProfile,
    GeneratedBid, BidPackage,
)
from app.services.file_service import get_generated_path


async def generate_bid_document(notice_id: int, template_id: int):
    """Generate a single bid document from a template."""
    from app.modules.generator.doc_generator import generate_document, save_as_docx

    async with async_session() as db:
        notice = await db.get(Notice, notice_id, options=[selectinload(Notice.analysis)])
        template = await db.get(BidTemplate, template_id)
        company = await db.scalar(select(CompanyProfile).limit(1))

        if not notice or not template:
            logger.error(f"Notice {notice_id} or template {template_id} not found")
            return

        analysis_data = {}
        analysis_id = None
        if notice.analysis and notice.analysis.ai_analysis:
            analysis_data = notice.analysis.ai_analysis
            analysis_id = notice.analysis.id

        company_data = company.data if company else {}
        template_text = template.extracted_text or ""

        # Build notice data dict with all available fields
        rd = notice.raw_data or {}
        notice_data = {
            "title": notice.title,
            "notice_no": rd.get("noticeNo", ""),
            "contracting_authority": notice.contracting_authority or "",
            "cpv_code": notice.cpv_code or "",
            "estimated_value": str(notice.estimated_value) if notice.estimated_value else "",
            "currency": notice.currency or "RON",
            "deadline": str(notice.deadline) if notice.deadline else "",
            "publication_date": str(notice.publication_date) if notice.publication_date else "",
            "seap_url": notice.seap_url or "",
            "notice_type": notice.notice_type or "",
        }

        try:
            content = await generate_document(
                template_text=template_text,
                analysis_data=analysis_data,
                company_data=company_data,
                notice_data=notice_data,
            )

            # Build proper folder and filename
            notice_no = notice_data.get("notice_no", "") or notice.seap_id
            title_clean = (notice.title or "")[:80]
            title_clean = "".join(c if c.isalnum() or c in (" ", "_", "-") else "" for c in title_clean).strip().replace(" ", "_")

            # Folder: Achizitionarea_serviciilor_de_publicitate_ADV1521611/
            folder_name = f"{title_clean}_{notice_no}"
            folder_name = folder_name.rstrip("_")
            output_dir = get_generated_path(notice_id) / folder_name
            output_dir.mkdir(parents=True, exist_ok=True)

            # Filename based on template category
            cat_names = {
                "propunere_financiara": "Propunere_financiara",
                "propunere_tehnica": "Propunere_tehnica",
                "formular_oferta": "Formular_oferta",
                "declaratie_conflict": "Declaratie_conflict_interese",
                "declaratie_mediu_social": "Declaratie_mediu_social",
                "declaratie_beneficiari": "Declaratie_beneficiari_reali",
                "declaratie_dnsh": "Declaratie_DNSH",
                "other": template.name.replace(" ", "_"),
            }
            doc_name = cat_names.get(template.category, template.name.replace(" ", "_"))
            output_filename = f"{doc_name}_{notice_no}.docx"
            output_path = str(output_dir / output_filename)
            sig_path = company_data.get("signature_path", "")
            stamp_path = company_data.get("stamp_path", "")
            save_as_docx(content, output_path, signature_path=sig_path, stamp_path=stamp_path)

            # Save to DB
            bid = GeneratedBid(
                notice_id=notice_id,
                analysis_id=analysis_id,
                template_id=template_id,
                generated_content=content,
                output_file_path=output_path,
                output_format="docx",
                status="draft",
            )
            db.add(bid)

            # Update notice status
            if notice.status in ("new", "analyzed"):
                notice.status = "bid_generated"

            await db.commit()
            logger.info(f"Generated bid document {output_filename} for notice {notice_id}")
            return bid.id

        except Exception as e:
            logger.error(f"Document generation failed: {e}")
            raise


async def create_bid_package(notice_id: int, bid_ids: list[int]):
    """Create a ZIP package with all generated bid documents."""
    async with async_session() as db:
        notice = await db.get(Notice, notice_id)
        if not notice:
            return

        bids = []
        for bid_id in bid_ids:
            bid = await db.get(GeneratedBid, bid_id)
            if bid and bid.output_file_path:
                bids.append(bid)

        if not bids:
            logger.warning(f"No valid bids found for package, notice {notice_id}")
            return

        output_dir = get_generated_path(notice_id)
        zip_name = f"Oferta_{notice.seap_id}_{datetime.now().strftime('%Y%m%d')}.zip"
        zip_path = str(output_dir / zip_name)

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for bid in bids:
                filename = Path(bid.output_file_path).name
                zf.write(bid.output_file_path, filename)

        package = BidPackage(
            notice_id=notice_id,
            name=f"Ofertă {notice.title[:100]}",
            documents=bid_ids,
            zip_file_path=zip_path,
            status="complete",
        )
        db.add(package)
        await db.commit()

        logger.info(f"Created bid package: {zip_name}")
        return package.id
