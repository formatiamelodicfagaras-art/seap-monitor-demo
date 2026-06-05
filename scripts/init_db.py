"""
Initialize the database with tables and seed data.
Run: py scripts/init_db.py
"""
import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine, Base, async_session
from app.models import *  # noqa: F401, F403
from app.models import MonitorSettings, CompanyProfile


async def init():
    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Tables created successfully.")

    # Seed initial data
    async with async_session() as db:
        # Create default monitor settings if not exists
        from sqlalchemy import select
        existing = await db.scalar(select(MonitorSettings).limit(1))
        if not existing:
            db.add(MonitorSettings(
                scan_interval_hours=6,
                notification_emails=[],
                is_active=False,
            ))
            print("Default monitor settings created.")

        # Create default company profile if not exists
        existing_company = await db.scalar(select(CompanyProfile).limit(1))
        if not existing_company:
            db.add(CompanyProfile(data={
                "name": "",
                "brand": "",
                "cui": "",
                "reg_com": "",
                "address": "",
                "bank_name": "",
                "iban": "",
                "legal_rep": "",
                "legal_rep_role": "Administrator",
                "phone": "",
                "email": "",
                "website": "",
            }))
            print("Default company profile created.")

        await db.commit()

    print("Database initialization complete!")


if __name__ == "__main__":
    asyncio.run(init())
