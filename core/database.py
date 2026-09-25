from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from domain.models import AuditEvent, Base, Campaign, DEFAULT_ORGANIZATION_ID, Organization
from models.donation import CAMPAIGNS


def _database_url() -> str:
    default_url = (
        "sqlite:////tmp/hoops_forward.db"
        if os.getenv("VERCEL")
        else "sqlite:///./data/hoops_forward.db"
    )
    value = os.getenv("DATABASE_URL", default_url).strip()
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+psycopg://", 1)
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    return value


DATABASE_URL = _database_url()
if DATABASE_URL.startswith("sqlite:///"):
    database_path = DATABASE_URL.removeprefix("sqlite:///")
    if database_path and database_path != ":memory:":
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def persistence_mode() -> str:
    if DATABASE_URL.startswith("postgresql"):
        return "durable_postgres"
    if os.getenv("VERCEL"):
        return "ephemeral_sqlite"
    return "local_sqlite"


def init_database() -> None:
    Base.metadata.create_all(bind=engine)
    with session_scope() as session:
        seed_reference_data(session)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def seed_reference_data(session: Session) -> None:
    organization = session.get(Organization, DEFAULT_ORGANIZATION_ID)
    if organization is None:
        organization = Organization(
            id=DEFAULT_ORGANIZATION_ID,
            legal_name="Hoops Forward (fictional sandbox organization)",
            verification_status="demo_only",
            nonprofit_status="not_represented",
            receipt_eligible=False,
            tax_disclosure=(
                "This sandbox demonstration does not represent that contributions are "
                "tax-deductible and does not issue charitable tax receipts."
            ),
        )
        session.add(organization)
        session.flush()

    _add_audit_once(
        session,
        entity_type="organization",
        entity_id=organization.id,
        action="demo_organization_created",
        safe_metadata={
            "verification_status": organization.verification_status,
            "receipt_eligible": organization.receipt_eligible,
        },
    )

    existing_campaign_ids = set(
        session.scalars(select(Campaign.id).where(Campaign.organization_id == DEFAULT_ORGANIZATION_ID))
    )
    for campaign_id, campaign in CAMPAIGNS.items():
        if campaign_id not in existing_campaign_ids:
            campaign_record = Campaign(
                id=campaign_id,
                organization_id=DEFAULT_ORGANIZATION_ID,
                name=campaign["name"],
                designation=campaign["short_name"],
                description=campaign["description"],
                active=True,
            )
            session.add(campaign_record)
        _add_audit_once(
            session,
            entity_type="campaign",
            entity_id=campaign_id,
            action="demo_campaign_created",
            safe_metadata={
                "organization_id": DEFAULT_ORGANIZATION_ID,
                "active": True,
            },
        )


def _add_audit_once(
    session: Session,
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    safe_metadata: dict[str, object],
) -> None:
    existing_id = session.scalar(
        select(AuditEvent.id).where(
            AuditEvent.entity_type == entity_type,
            AuditEvent.entity_id == entity_id,
            AuditEvent.action == action,
        )
    )
    if existing_id is None:
        session.add(
            AuditEvent(
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
                actor_type="system",
                safe_metadata=safe_metadata,
            )
        )
