import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from app.config import get_settings

settings = get_settings()


def get_storage_path() -> Path:
    return Path(settings.storage_path)


def get_downloads_path(notice_id: int) -> Path:
    path = get_storage_path() / "downloads" / str(notice_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_templates_path() -> Path:
    path = get_storage_path() / "templates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_generated_path(notice_id: int) -> Path:
    path = get_storage_path() / "generated" / str(notice_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_old_files(max_age_days: int = 90):
    """Remove downloaded and generated files older than max_age_days."""
    cutoff = datetime.now() - timedelta(days=max_age_days)

    for folder in ["downloads", "generated"]:
        base_path = get_storage_path() / folder
        if not base_path.exists():
            continue

        for item in base_path.iterdir():
            if item.is_dir():
                mod_time = datetime.fromtimestamp(item.stat().st_mtime)
                if mod_time < cutoff:
                    shutil.rmtree(item, ignore_errors=True)
                    logger.info(f"Cleaned up old directory: {item}")


def save_uploaded_file(file_content: bytes, filename: str, subfolder: str = "templates") -> str:
    """Save an uploaded file and return the relative path."""
    safe_name = filename.replace("..", "").replace("/", "_").replace("\\", "_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_name = f"{timestamp}_{safe_name}"

    if subfolder == "templates":
        dest_dir = get_templates_path()
    else:
        dest_dir = get_storage_path() / subfolder
        dest_dir.mkdir(parents=True, exist_ok=True)

    dest_path = dest_dir / final_name
    dest_path.write_bytes(file_content)

    return str(dest_path)


def get_file_size_mb(file_path: str) -> float:
    try:
        return os.path.getsize(file_path) / (1024 * 1024)
    except OSError:
        return 0.0
