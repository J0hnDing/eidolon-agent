from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.entities import utc_now


class IntegrationStandingGrant(Base):
    """One revocable, exact-principal integration authority, without payloads."""

    __tablename__ = "integration_standing_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(160), nullable=False)
    contract_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    connection_id: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[str] = mapped_column(String(320), nullable=False)
    principal_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "uq_integration_standing_grant_active",
            "operation_id", "contract_fingerprint", "provider", "connection_id",
            "account_id", "principal_kind", "principal_id",
            unique=True,
            sqlite_where=sql_text("revoked_at IS NULL"),
        ),
        {"sqlite_autoincrement": True},
    )
