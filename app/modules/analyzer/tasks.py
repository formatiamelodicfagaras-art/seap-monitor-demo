from datetime import datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models import Notice, NoticeAnalysis


async def run_analysis(notice_id: int):
    """Extract text from notice documents and run AI analysis."""
    from app.modules.analyzer.extractor import extract_text
    from app.modules.analyzer.ai_analyzer import analyze_notice

    async with async_session() as db:
        notice = await db.get(
            Notice, notice_id,
            options=[selectinload(Notice.documents)],
        )
        if not notice:
            logger.error(f"Notice {notice_id} not found")
            return

        # Get or create analysis record
        analysis = await db.scalar(
            select(NoticeAnalysis).where(NoticeAnalysis.notice_id == notice_id)
        )
        if not analysis:
            analysis = NoticeAnalysis(notice_id=notice_id, status="extracting")
            db.add(analysis)
            await db.flush()
        else:
            analysis.status = "extracting"
            analysis.error_message = None

        await db.commit()

        # Step 1: Extract text from all documents
        try:
            text_parts = []
            for doc in notice.documents:
                if doc.file_path and doc.downloaded_at:
                    text = extract_text(doc.file_path, doc.file_type)
                    if text and not text.startswith("["):
                        text_parts.append(f"=== {doc.filename} ===\n{text}")

            extracted_text = "\n\n".join(text_parts)
            if not extracted_text:
                # Use raw_data as fallback
                if notice.raw_data:
                    import json
                    extracted_text = json.dumps(notice.raw_data, ensure_ascii=False, indent=2)
                else:
                    extracted_text = f"Titlu: {notice.title}\nAutoritate: {notice.contracting_authority}\nCPV: {notice.cpv_code}"

            analysis.extracted_text = extracted_text
            analysis.status = "analyzing"
            await db.commit()

        except Exception as e:
            logger.error(f"Text extraction failed for notice {notice_id}: {e}")
            analysis.status = "error"
            analysis.error_message = f"Eroare extragere text: {e}"
            await db.commit()
            return

        # Step 2: AI Analysis
        try:
            result = await analyze_notice(extracted_text, notice.title)

            analysis.ai_analysis = result["analysis"]
            analysis.model_used = result["model"]
            analysis.tokens_used = result["tokens_used"]
            analysis.match_score = result["analysis"].get("match_score", {}).get("score")
            analysis.analyzed_at = datetime.utcnow()
            analysis.status = "completed"

            notice.status = "analyzed"

            await db.commit()
            logger.info(f"Analysis complete for notice {notice_id}, score: {analysis.match_score}")

        except Exception as e:
            logger.error(f"AI analysis failed for notice {notice_id}: {e}")
            analysis.status = "error"
            analysis.error_message = f"Eroare analiză AI: {e}"
            await db.commit()
