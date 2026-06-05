from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, Date,
    Numeric, JSON, ForeignKey, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


# ─── Module 1: Monitor ───────────────────────────────────────────────

class CPVCode(Base):
    __tablename__ = "cpv_codes"

    id = Column(Integer, primary_key=True)
    code = Column(String(20), unique=True, nullable=False, index=True)
    description = Column(String(500), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class MonitoredAuthority(Base):
    __tablename__ = "monitored_authorities"

    id = Column(Integer, primary_key=True)
    seap_id = Column(Integer, unique=True, nullable=False, index=True)
    name = Column(String(500), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class Notice(Base):
    __tablename__ = "notices"

    id = Column(Integer, primary_key=True)
    seap_id = Column(String(50), unique=True, nullable=False, index=True)
    notice_type = Column(String(30), nullable=False)  # "licitatie" / "achizitie_directa"
    title = Column(Text, nullable=False)
    contracting_authority = Column(String(500))
    authority_seap_id = Column(Integer, nullable=True)
    cpv_code = Column(String(20))
    estimated_value = Column(Numeric(15, 2), nullable=True)
    currency = Column(String(10), default="RON")
    publication_date = Column(Date, nullable=True)
    deadline = Column(DateTime, nullable=True)
    seap_url = Column(String(500))
    raw_data = Column(JSON, nullable=True)
    status = Column(String(20), default="new", index=True)
    # status: "new" / "analyzed" / "bid_generated" / "bid_submitted" / "archived"
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    documents = relationship("NoticeDocument", back_populates="notice", cascade="all, delete-orphan")
    analysis = relationship("NoticeAnalysis", back_populates="notice", uselist=False)


class NoticeDocument(Base):
    __tablename__ = "notice_documents"

    id = Column(Integer, primary_key=True)
    notice_id = Column(Integer, ForeignKey("notices.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(500), nullable=False)
    file_path = Column(String(1000))
    file_type = Column(String(20))  # "pdf" / "docx" / "zip" etc.
    file_size = Column(Integer, nullable=True)
    download_url = Column(String(1000))
    downloaded_at = Column(DateTime, nullable=True)

    notice = relationship("Notice", back_populates="documents")


class ScanLog(Base):
    __tablename__ = "scan_logs"

    id = Column(Integer, primary_key=True)
    scan_type = Column(String(20), nullable=False)  # "cpv" / "authority"
    filter_value = Column(String(100))
    notices_found = Column(Integer, default=0)
    new_notices = Column(Integer, default=0)
    started_at = Column(DateTime, server_default=func.now())
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(20), default="running")  # "running" / "success" / "error"
    error_message = Column(Text, nullable=True)


class MonitorSettings(Base):
    __tablename__ = "monitor_settings"

    id = Column(Integer, primary_key=True)
    scan_interval_hours = Column(Integer, default=6)
    notification_emails = Column(JSON, default=list)  # JSON array of emails
    is_active = Column(Boolean, default=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


# ─── Module 2: Analyzer ──────────────────────────────────────────────

class NoticeAnalysis(Base):
    __tablename__ = "notice_analyses"

    id = Column(Integer, primary_key=True)
    notice_id = Column(Integer, ForeignKey("notices.id", ondelete="CASCADE"), unique=True, nullable=False)
    extracted_text = Column(Text, nullable=True)
    ai_analysis = Column(JSON, nullable=True)
    match_score = Column(Integer, nullable=True)
    model_used = Column(String(100))
    tokens_used = Column(Integer, nullable=True)
    analyzed_at = Column(DateTime, nullable=True)
    status = Column(String(20), default="pending")  # "pending" / "extracting" / "analyzing" / "completed" / "error"
    error_message = Column(Text, nullable=True)

    notice = relationship("Notice", back_populates="analysis")


# ─── Module 3: Generator ─────────────────────────────────────────────

class BidTemplate(Base):
    __tablename__ = "bid_templates"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    category = Column(String(30), nullable=False)  # "administrative" / "technical" / "financial" / "other"
    original_filename = Column(String(500))
    file_path = Column(String(1000))
    extracted_text = Column(Text, nullable=True)
    ai_field_analysis = Column(JSON, nullable=True)
    uploaded_at = Column(DateTime, server_default=func.now())


class CompanyProfile(Base):
    __tablename__ = "company_profiles"

    id = Column(Integer, primary_key=True)
    data = Column(JSON, nullable=False, default=dict)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class GeneratedBid(Base):
    __tablename__ = "generated_bids"

    id = Column(Integer, primary_key=True)
    notice_id = Column(Integer, ForeignKey("notices.id", ondelete="CASCADE"), nullable=False)
    analysis_id = Column(Integer, ForeignKey("notice_analyses.id"), nullable=True)
    template_id = Column(Integer, ForeignKey("bid_templates.id"), nullable=True)
    generated_content = Column(Text, nullable=True)
    output_file_path = Column(String(1000), nullable=True)
    output_format = Column(String(10), default="docx")
    status = Column(String(20), default="draft")  # "draft" / "reviewed" / "final"
    generated_at = Column(DateTime, server_default=func.now())
    reviewed_at = Column(DateTime, nullable=True)

    notice = relationship("Notice")
    analysis = relationship("NoticeAnalysis")
    template = relationship("BidTemplate")


class BidPackage(Base):
    __tablename__ = "bid_packages"

    id = Column(Integer, primary_key=True)
    notice_id = Column(Integer, ForeignKey("notices.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(300), nullable=False)
    documents = Column(JSON, default=list)  # List of GeneratedBid IDs
    zip_file_path = Column(String(1000), nullable=True)
    status = Column(String(20), default="in_progress")  # "in_progress" / "complete" / "submitted"
    created_at = Column(DateTime, server_default=func.now())
    submitted_at = Column(DateTime, nullable=True)

    notice = relationship("Notice")
