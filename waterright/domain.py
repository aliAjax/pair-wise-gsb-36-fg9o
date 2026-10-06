"""Pure domain rules for water-rights accounts, pledges and disposals.

Everything in this module is free of SQL and HTTP so the judgement logic can be
reused by the storage layer, the API and tests in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value: str, field: str = "日期") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"{field}必须是 YYYY-MM-DD") from exc


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# Pledge lifecycle:
#   pledged  -> frozen     (a disposal order has been filed)
#   pledged  -> released   (reviewer releases an unperformed pledge)
#   frozen   -> transferred (disposal approved, quota moves to beneficiary)
#   frozen   -> released   (reviewer rejects the disposal order)
ACTIVE_PLEDGE_STATES = ("pledged", "frozen")
PENDING_DISPOSAL_STATES = ("applied",)


@dataclass(frozen=True)
class QuotaBreakdown:
    """Quota occupation view.

    ``pledged`` and ``frozen`` cannot overlap: when a disposal order is filed
    the pledge moves from ``pledged`` to ``frozen``. Both are unavailable for
    new transfers or withdrawals.
    """

    quota: float
    used: float
    pending_transfer: float
    pledged: float
    frozen: float

    @property
    def encumbered(self) -> float:
        return self.used + self.pending_transfer + self.pledged + self.frozen

    @property
    def available(self) -> float:
        return round(max(0.0, self.quota - self.encumbered), 6)


def quota_breakdown(
    quota: float,
    used: float,
    pending_transfer: float = 0.0,
    pledged: float = 0.0,
    frozen: float = 0.0,
) -> QuotaBreakdown:
    return QuotaBreakdown(
        quota=float(quota),
        used=float(used),
        pending_transfer=float(pending_transfer),
        pledged=float(pledged),
        frozen=float(frozen),
    )


def quota_available(
    quota: float,
    used: float,
    pending_transfer: float = 0.0,
    pledged: float = 0.0,
    frozen: float = 0.0,
) -> float:
    """Available quota after usage, pending transfers, pledges and frozen
    disposal amounts. The same unit of quota can only occupy one of these
    slots, so every occupation subtracts from availability."""
    return quota_breakdown(quota, used, pending_transfer, pledged, frozen).available


def enough_for(amount: float, breakdown: QuotaBreakdown, *, tolerance: float = 1e-9) -> bool:
    return amount <= breakdown.available + tolerance


def validate_pledge_amount(amount: Any) -> float:
    try:
        value = float(amount)
    except (TypeError, ValueError) as exc:
        raise DomainError("质押金额必须是数值") from exc
    if value <= 0:
        raise DomainError("质押金额必须大于 0")
    return value


def validate_beneficiary(value: Any) -> str:
    beneficiary = str(value or "").strip()
    if not beneficiary:
        raise DomainError("受益人不能为空")
    return beneficiary


def pledge_is_active(pledge: dict[str, Any]) -> bool:
    return pledge["status"] in ACTIVE_PLEDGE_STATES


def can_file_disposal(pledge: dict[str, Any], *, today: date) -> tuple[bool, str]:
    """A disposal order needs an active, unfrozen pledge whose maturity has
    passed without performance."""
    if pledge["status"] == "frozen":
        return False, "该质押已有待处置单，不能重复申请处置"
    if pledge["status"] != "pledged":
        return False, "该质押已处置或已释放，不能再次申请"
    maturity = parse_date(pledge["maturity_date"], "到期日")
    if maturity > today:
        return False, "质押尚未到期，不能申请处置"
    return True, ""


def can_apply_disposal(pledge: dict[str, Any], actor: str, role: str, *, today: date) -> None:
    allowed = {"editor", "reviewer"}
    if role not in allowed and actor != pledge["beneficiary"]:
        raise DomainError("只有受益人或水务管理人员可以申请处置", 403)
    ok, reason = can_file_disposal(pledge, today=today)
    if not ok:
        raise DomainError(reason, 409)


def normalize_disposal_outcome(payload: dict[str, Any]) -> dict[str, Any]:
    """Read the reviewer's decision from a disposal request body.

    ``decision`` may be ``"transfer"``/``"release"``; alternatively an explicit
    ``transfer_to_account_id`` means transfer, an omitted one means release.
    """
    decision = str(payload.get("decision", "")).strip().lower()
    target_raw = payload.get("transfer_to_account_id")
    target_id: int | None = None
    if target_raw not in (None, ""):
        try:
            target_id = int(target_raw)
        except (TypeError, ValueError) as exc:
            raise DomainError("转入账户必须是数值") from exc
    if decision:
        if decision not in {"transfer", "release"}:
            raise DomainError("处置决定只能是 transfer 或 release")
        if decision == "transfer":
            if target_id is None:
                raise DomainError("处置为转让时必须填写受益人转入账户")
        else:
            target_id = None
    elif target_id is not None:
        decision = "transfer"
    else:
        decision = "release"
    return {"decision": decision, "transfer_to_account_id": target_id}


def drought_plan(accounts: Iterable[dict[str, Any]], total_supply: float, reduction: float) -> dict[str, Any]:
    """Allocate scarce supply: higher priority (smaller number) first; within a
    priority tier accounts share in proportion to their remaining quota."""
    rows = list(accounts)
    supply = total_supply * (1 - reduction)
    allocation: dict[int, float] = {}
    deficit: dict[int, float] = {}
    remaining = supply
    for priority in range(1, 6):
        group = [r for r in rows if int(r["priority"]) == priority]
        if not group:
            continue
        requested = sum(max(0.0, float(r["quota"]) - float(r["used"])) for r in group)
        if requested <= 0:
            continue
        take = min(remaining, requested)
        for row in group:
            quota_left = max(0.0, float(row["quota"]) - float(row["used"]))
            share = take * quota_left / requested
            allocation[int(row["id"])] = share
            deficit[int(row["id"])] = quota_left - share
        remaining -= take
        if remaining <= 1e-9:
            for lower in rows:
                if int(lower["priority"]) > priority:
                    left = max(0.0, float(lower["quota"]) - float(lower["used"]))
                    allocation[int(lower["id"])] = 0.0
                    deficit[int(lower["id"])] = left
            break
    return {
        "effective_supply": supply,
        "unallocated": remaining,
        "allocation": allocation,
        "deficit": deficit,
    }
