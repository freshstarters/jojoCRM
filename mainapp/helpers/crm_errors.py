from dataclasses import dataclass


@dataclass(frozen=True)
class CrmDomainError(Exception):
    """Domain validation, permission, or conflict error for CRM operations."""

    message: str
    status_code: int = 400

    def __str__(self) -> str:
        return self.message
