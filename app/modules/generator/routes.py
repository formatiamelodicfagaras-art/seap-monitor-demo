from pathlib import Path

from fastapi import APIRouter, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import (
    Notice, NoticeAnalysis, BidTemplate, CompanyProfile,
    GeneratedBid, BidPackage,
)
from app.services.file_service import save_uploaded_file
from app.modules.analyzer.extractor import extract_text

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ─── Templates Management ────────────────────────────────────────────

@router.get("/templates", response_class=HTMLResponse)
async def templates_list(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(BidTemplate).order_by(desc(BidTemplate.uploaded_at))
    )
    bid_templates = result.scalars().all()

    return templates.TemplateResponse("generator/templates.html", {
        "request": request,
        "active_page": "gen_templates",
        "bid_templates": bid_templates,
    })


@router.post("/templates/upload")
async def upload_template(
    request: Request,
    name: str = Form(...),
    category: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    # Validate file type
    allowed = {"docx", "doc", "pdf", "xlsx", "xls"}
    ext = file.filename.rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else ""
    if ext not in allowed:
        return RedirectResponse(url="/generator/templates", status_code=302)

    # Save file
    content = await file.read()
    file_path = save_uploaded_file(content, file.filename or "template", "templates")

    # Extract text (skip for spreadsheets)
    extracted = None
    if ext in ("docx", "doc", "pdf"):
        try:
            extracted = extract_text(file_path, ext)
            if extracted and extracted.startswith("["):
                extracted = None
        except Exception:
            pass

    # Create template record
    bid_template = BidTemplate(
        name=name.strip(),
        category=category,
        original_filename=file.filename,
        file_path=file_path,
        extracted_text=extracted,
    )
    db.add(bid_template)
    await db.commit()

    # Optionally analyze template with AI (skip spreadsheets)
    if ext in ("docx", "doc", "pdf") and extracted:
        from app.modules.generator.template_mgr import analyze_template
        try:
            analysis = await analyze_template(file_path, ext)
            if analysis:
                bid_template.ai_field_analysis = analysis
                await db.commit()
        except Exception:
            pass

    return RedirectResponse(url="/generator/templates", status_code=302)


@router.post("/templates/delete/{template_id}")
async def delete_template(template_id: int, db: AsyncSession = Depends(get_db)):
    tmpl = await db.get(BidTemplate, template_id)
    if tmpl:
        # Delete file
        if tmpl.file_path:
            path = Path(tmpl.file_path)
            if path.exists():
                path.unlink()
        await db.delete(tmpl)
        await db.commit()
    return RedirectResponse(url="/generator/templates", status_code=302)


# ─── Company Profile ─────────────────────────────────────────────────

@router.get("/company", response_class=HTMLResponse)
async def company_profile(request: Request, db: AsyncSession = Depends(get_db)):
    company = await db.scalar(select(CompanyProfile).limit(1))

    return templates.TemplateResponse("generator/company.html", {
        "request": request,
        "active_page": "company",
        "company": company.data if company else {},
    })


@router.post("/company/save")
async def save_company(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    form_data = await request.form()
    data = {
        "name": form_data.get("name", ""),
        "brand": form_data.get("brand", ""),
        "cui": form_data.get("cui", ""),
        "reg_com": form_data.get("reg_com", ""),
        "address": form_data.get("address", ""),
        "bank_name": form_data.get("bank_name", ""),
        "iban": form_data.get("iban", ""),
        "legal_rep": form_data.get("legal_rep", ""),
        "legal_rep_role": form_data.get("legal_rep_role", "Administrator"),
        "phone": form_data.get("phone", ""),
        "email": form_data.get("email", ""),
        "website": form_data.get("website", ""),
    }

    company = await db.scalar(select(CompanyProfile).limit(1))
    if company:
        # Preserve signature/stamp paths from existing data
        old_data = company.data or {}
        if old_data.get("signature_path"):
            data["signature_path"] = old_data["signature_path"]
        if old_data.get("stamp_path"):
            data["stamp_path"] = old_data["stamp_path"]
        company.data = data
    else:
        db.add(CompanyProfile(data=data))

    await db.commit()
    return RedirectResponse(url="/generator/company", status_code=302)


@router.post("/company/upload-signature")
async def upload_signature(
    request: Request,
    signature: UploadFile = File(None),
    stamp: UploadFile = File(None),
    db: AsyncSession = Depends(get_db),
):
    """Upload signature and/or stamp images."""
    from app.services.file_service import get_storage_path

    company = await db.scalar(select(CompanyProfile).limit(1))
    if not company:
        company = CompanyProfile(data={})
        db.add(company)
        await db.commit()

    img_dir = get_storage_path() / "company_images"
    img_dir.mkdir(parents=True, exist_ok=True)

    data = dict(company.data) if company.data else {}

    if signature and signature.filename:
        content = await signature.read()
        if content:
            sig_path = str(img_dir / "signature.png")
            with open(sig_path, "wb") as f:
                f.write(content)
            data["signature_path"] = sig_path

    if stamp and stamp.filename:
        content = await stamp.read()
        if content:
            stamp_path = str(img_dir / "stamp.png")
            with open(stamp_path, "wb") as f:
                f.write(content)
            data["stamp_path"] = stamp_path

    company.data = data
    await db.commit()
    return RedirectResponse(url="/generator/company", status_code=302)


@router.get("/company/image/{image_type}")
async def get_company_image(image_type: str, db: AsyncSession = Depends(get_db)):
    """Serve signature or stamp image."""
    company = await db.scalar(select(CompanyProfile).limit(1))
    if not company or not company.data:
        return RedirectResponse(url="/generator/company", status_code=302)

    key = f"{image_type}_path"
    file_path = company.data.get(key, "")
    if not file_path:
        return RedirectResponse(url="/generator/company", status_code=302)

    p = Path(file_path)
    if not p.exists():
        return RedirectResponse(url="/generator/company", status_code=302)

    return FileResponse(str(p), media_type="image/png")


# ─── Generate Bid ─────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def generator_home(request: Request, db: AsyncSession = Depends(get_db)):
    # Get analyzed notices
    notices_result = await db.execute(
        select(Notice)
        .options(selectinload(Notice.analysis))
        .where(Notice.status.in_(["analyzed", "bid_generated"]))
        .order_by(desc(Notice.created_at))
    )
    notices = notices_result.scalars().all()

    # Get templates
    templates_result = await db.execute(
        select(BidTemplate).order_by(BidTemplate.category, BidTemplate.name)
    )
    bid_templates = templates_result.scalars().all()

    # Get generated bids
    bids_result = await db.execute(
        select(GeneratedBid)
        .options(selectinload(GeneratedBid.notice), selectinload(GeneratedBid.template))
        .order_by(desc(GeneratedBid.generated_at))
        .limit(20)
    )
    generated_bids = bids_result.scalars().all()

    # Build auto-select mapping: notice_id -> list of template_ids to pre-check
    # Based on matching analysis required_documents with template categories
    import json as _json
    auto_select = {}
    cat_keywords = {
        "propunere_financiara": ["financiar", "pret", "oferta financiara", "propunere financiara"],
        "propunere_tehnica": ["tehnic", "propunere tehnica", "oferta tehnica"],
        "formular_oferta": ["formular de oferta", "formular oferta", "scrisoare de inaintare"],
        "declaratie_conflict": ["conflict", "art.60", "art 60"],
        "declaratie_mediu_social": ["mediu", "social", "mediu social"],
        "declaratie_beneficiari": ["beneficiar", "beneficiarii reali"],
        "declaratie_dnsh": ["dnsh", "principiul dnsh"],
        "other": ["acord", "prelucrare date", "gdpr"],
    }
    for notice in notices:
        if notice.analysis and notice.analysis.ai_analysis:
            ai = notice.analysis.ai_analysis
            req_docs = ai.get("required_documents", [])
            doc_text = " ".join(
                (d.get("document", "") + " " + d.get("details", "")).lower()
                for d in req_docs
            )
            matched_ids = []
            for tmpl in bid_templates:
                keywords = cat_keywords.get(tmpl.category, [])
                if any(kw in doc_text for kw in keywords):
                    matched_ids.append(tmpl.id)
            # If no keywords matched but there are required docs, select all single-category templates
            if not matched_ids and req_docs:
                seen_cats = set()
                for tmpl in bid_templates:
                    cat_count = sum(1 for t in bid_templates if t.category == tmpl.category)
                    if cat_count == 1 and tmpl.category not in seen_cats:
                        matched_ids.append(tmpl.id)
                        seen_cats.add(tmpl.category)
            auto_select[notice.id] = matched_ids

    return templates.TemplateResponse("generator/generate.html", {
        "request": request,
        "active_page": "generator",
        "notices": notices,
        "bid_templates": bid_templates,
        "generated_bids": generated_bids,
        "auto_select": auto_select,
    })


@router.post("/generate")
async def generate_bid(
    request: Request,
    notice_id: int = Form(...),
    template_ids: list[int] = Form(...),
    db: AsyncSession = Depends(get_db),
):
    from app.modules.generator.tasks import generate_bid_document
    from loguru import logger

    logger.info(f"Starting generation for notice {notice_id}, templates: {template_ids}")

    for template_id in template_ids:
        try:
            logger.info(f"Generating template {template_id}...")
            await generate_bid_document(notice_id, template_id)
            logger.info(f"Done template {template_id}")
        except Exception as e:
            logger.error(f"Generate failed for template {template_id}: {e}")

    return RedirectResponse(url="/generator/", status_code=302)


@router.post("/delete/{bid_id}")
async def delete_bid(bid_id: int, db: AsyncSession = Depends(get_db)):
    bid = await db.get(GeneratedBid, bid_id)
    if bid:
        if bid.output_file_path:
            p = Path(bid.output_file_path)
            if p.exists():
                p.unlink()
        await db.delete(bid)
        await db.commit()
    return RedirectResponse(url="/generator/", status_code=302)


@router.get("/preview/{bid_id}", response_class=HTMLResponse)
async def preview_bid(
    request: Request,
    bid_id: int,
    db: AsyncSession = Depends(get_db),
):
    bid = await db.get(
        GeneratedBid, bid_id,
        options=[selectinload(GeneratedBid.notice), selectinload(GeneratedBid.template)],
    )
    if not bid:
        return RedirectResponse(url="/generator/", status_code=302)

    return templates.TemplateResponse("generator/preview.html", {
        "request": request,
        "active_page": "generator",
        "bid": bid,
    })


@router.get("/download/{bid_id}")
async def download_bid(bid_id: int, db: AsyncSession = Depends(get_db)):
    bid = await db.get(GeneratedBid, bid_id)
    if not bid or not bid.output_file_path:
        return RedirectResponse(url="/generator/", status_code=302)

    path = Path(bid.output_file_path)
    if not path.exists():
        return RedirectResponse(url="/generator/", status_code=302)

    return FileResponse(
        path=str(path),
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@router.post("/package/{notice_id}")
async def create_package(
    notice_id: int,
    db: AsyncSession = Depends(get_db),
):
    from app.modules.generator.tasks import create_bid_package

    # Get all bids for this notice
    result = await db.execute(
        select(GeneratedBid.id).where(GeneratedBid.notice_id == notice_id)
    )
    bid_ids = [r[0] for r in result.all()]

    if bid_ids:
        await create_bid_package(notice_id, bid_ids)

    return RedirectResponse(url="/generator/", status_code=302)
