"""Water-rights service layers.

- :mod:`waterright.domain`: pure domain rules (dates, quota judgement, disposal
  state machine, drought allocation) with no SQL.
- :mod:`waterright.store`: SQLite schema and row-level storage gateway.
- :mod:`waterright.service`: use-case orchestration combining domain and store.
"""
from __future__ import annotations

from .domain import DomainError, drought_plan, parse_date, quota_available, utcnow
from .service import WaterRightsService
from .store import Store

__all__ = [
    "DomainError",
    "Store",
    "WaterRightsService",
    "drought_plan",
    "parse_date",
    "quota_available",
    "utcnow",
]
