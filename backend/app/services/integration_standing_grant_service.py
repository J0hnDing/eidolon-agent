from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import IntegrationStandingGrant
from app.models.entities import utc_now


class IntegrationStandingGrantError(ValueError):
    error_type = "standing_grant_missing_or_stale"


class IntegrationStandingGrantService:
    """Backend settings and dispatch access to exact, revocable grant records."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def grant(
        self,
        *,
        operation_id: str,
        contract_fingerprint: str,
        provider: str,
        connection_id: int,
        account_id: str,
        principal_kind: str,
        principal_id: str,
    ) -> IntegrationStandingGrant:
        bounds = self._bounds(
            operation_id=operation_id,
            contract_fingerprint=contract_fingerprint,
            provider=provider,
            connection_id=connection_id,
            account_id=account_id,
            principal_kind=principal_kind,
            principal_id=principal_id,
        )
        existing = self.active(**bounds)
        if existing is not None:
            return existing
        grant = IntegrationStandingGrant(**bounds)
        try:
            # The partial unique index also makes concurrent enable requests
            # idempotent without rolling back unrelated settings mutations.
            with self.db.begin_nested():
                self.db.add(grant)
                self.db.flush()
        except IntegrityError:
            existing = self.active(**bounds)
            if existing is None:
                raise
            return existing
        self.db.commit()
        return grant

    def active(
        self,
        *,
        operation_id: str,
        contract_fingerprint: str,
        provider: str,
        connection_id: int,
        account_id: str,
        principal_kind: str,
        principal_id: str,
    ) -> IntegrationStandingGrant | None:
        bounds = self._bounds(
            operation_id=operation_id,
            contract_fingerprint=contract_fingerprint,
            provider=provider,
            connection_id=connection_id,
            account_id=account_id,
            principal_kind=principal_kind,
            principal_id=principal_id,
        )
        return self.db.scalar(
            select(IntegrationStandingGrant)
            .filter_by(**bounds)
            .where(IntegrationStandingGrant.revoked_at.is_(None))
            .execution_options(populate_existing=True)
        )

    def require_bound(self, grant_id: int, **bounds) -> IntegrationStandingGrant:
        grant = self.active(**bounds)
        if grant is None or grant.id != grant_id:
            raise IntegrationStandingGrantError("Integration standing grant is missing, revoked, or stale")
        return grant

    def revoke(self, grant_id: int) -> bool:
        result = self.db.execute(
            update(IntegrationStandingGrant)
            .where(IntegrationStandingGrant.id == grant_id)
            .where(IntegrationStandingGrant.revoked_at.is_(None))
            .values(revoked_at=utc_now())
        )
        self.db.commit()
        return result.rowcount > 0

    def revoke_for_principal(
        self, *, operation_id: str, principal_kind: str, principal_id: str,
    ) -> int:
        """Disable an operation even if its connection or contract has changed."""
        result = self.db.execute(
            update(IntegrationStandingGrant)
            .where(IntegrationStandingGrant.operation_id == operation_id)
            .where(IntegrationStandingGrant.principal_kind == principal_kind)
            .where(IntegrationStandingGrant.principal_id == principal_id)
            .where(IntegrationStandingGrant.revoked_at.is_(None))
            .values(revoked_at=utc_now())
        )
        self.db.commit()
        return result.rowcount

    @staticmethod
    def _bounds(**bounds) -> dict:
        if (
            not isinstance(bounds.get("connection_id"), int)
            or isinstance(bounds.get("connection_id"), bool)
            or bounds["connection_id"] <= 0
            or any(
                not isinstance(value, str) or not value.strip()
                for key, value in bounds.items() if key != "connection_id"
            )
        ):
            raise IntegrationStandingGrantError("Integration standing grant requires complete identity bounds")
        return bounds
