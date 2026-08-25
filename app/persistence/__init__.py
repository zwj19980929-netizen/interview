"""Transactional persistence seam and local adapters."""

from app.persistence.interface import Persistence, PersistenceTransaction, new_work_item
from app.persistence.provider import persistence_for

__all__ = ["Persistence", "PersistenceTransaction", "new_work_item", "persistence_for"]
