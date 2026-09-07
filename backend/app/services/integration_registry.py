"""Compatibility imports for the provider-neutral integration registry.

New code should import from :mod:`app.integrations`. This module deliberately
contains no operation definitions and therefore cannot become an independent
execution or security authority.
"""

from app.integrations.registry import DEFAULT_INTEGRATION_REGISTRY, registry_contract_identity
from app.integrations.types import IntegrationOperation

OPERATIONS = DEFAULT_INTEGRATION_REGISTRY.operation_mapping

__all__ = ["IntegrationOperation", "OPERATIONS", "registry_contract_identity"]
