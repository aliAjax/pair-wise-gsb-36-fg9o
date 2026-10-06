"""水权质押担保的判定逻辑。

本模块只包含常量和纯函数：不连接数据库、不读写文件。
额度互斥计算、质押/处置状态机、审批结果解析都在这里完成，
存储层（app.py）把数据库行读出来后交给这里判定，方便单独测试。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from errors import DomainError

EPS = 1e-9

# 质押状态机：active(履约中) -> frozen(处置申请已冻结) -> disposed(已转让)/released(已释放)
# active 也可以直接 released（履约解除）。
PLEDGE_ACTIVE = "active"
PLEDGE_FROZEN = "frozen"
PLEDGE_DISPOSED = "disposed"
PLEDGE_RELEASED = "released"
# 处于这两种状态的质押仍然占用额度
PLEDGE_BLOCKING = (PLEDGE_ACTIVE, PLEDGE_FROZEN)

# 处置单状态机：frozen(已冻结质押额，待审批) -> transferred(已生成转让)/released(已释放)
DISPOSAL_FROZEN = "frozen"
DISPOSAL_TRANSFERRED = "transferred"
DISPOSAL_RELEASED = "released"

DECISION_TRANSFER = "transfer"
DECISION_RELEASE = "release"


def compute_available(quota: float, used: float, reserved_outgoing: float, pledged: float) -> float:
    """可用额度 = 许可额度 - 取水占用 - 待审转让预占 - 质押占用（含待处置冻结）。"""
    return max(0.0, float(quota) - float(used) - float(reserved_outgoing) - float(pledged))


def ensure_pledge_input(amount: Any, beneficiary: Any, expires_on: Any) -> tuple[float, str, date]:
    """校验账户持有人提交的质押要素：金额、受益人、到期日。"""
    try:
        value = float(amount)
    except (TypeError, ValueError) as exc:
        raise DomainError("质押金额必须是数值") from exc
    if value <= 0:
        raise DomainError("质押金额必须大于 0")
    name = str(beneficiary or "").strip()
    if not name:
        raise DomainError("受益人不能为空")
    try:
        expiry = date.fromisoformat(str(expires_on))
    except (TypeError, ValueError) as exc:
        raise DomainError("到期日必须是 YYYY-MM-DD") from exc
    return value, name, expiry


def ensure_quota_available(amount: float, available: float) -> None:
    """同一额度不能同时被质押、待审转让和取水占用。"""
    if amount > available + EPS:
        raise DomainError("可用额度不足，质押、待审转让与取水占用互斥", 409)


def ensure_disposable(status: str, expires_on: date, today: date) -> None:
    """只有履约中且已到期的质押才能申请处置。"""
    if status != PLEDGE_ACTIVE:
        raise DomainError("质押已冻结或已了结，不能重复申请处置", 409)
    if expires_on > today:
        raise DomainError("质押尚未到期，不能申请处置", 409)


def ensure_releasable(status: str) -> None:
    """只有履约中的质押可以由持有人直接解除；冻结中的必须走处置审批。"""
    if status != PLEDGE_ACTIVE:
        raise DomainError("只有履约中的质押可以直接解除", 409)


def normalize_decision(raw: Any) -> str:
    """把审批结果规整为 transfer(生成转让) 或 release(释放)。"""
    text = str(raw or "").strip().lower()
    aliases = {
        "transfer": DECISION_TRANSFER,
        "转让": DECISION_TRANSFER,
        "release": DECISION_RELEASE,
        "释放": DECISION_RELEASE,
    }
    if text in aliases:
        return aliases[text]
    raise DomainError("审批结果必须是 transfer(转让) 或 release(释放)")


def final_disposal_status(decision: str) -> str:
    return DISPOSAL_TRANSFERRED if decision == DECISION_TRANSFER else DISPOSAL_RELEASED


def final_pledge_status(decision: str) -> str:
    return PLEDGE_DISPOSED if decision == DECISION_TRANSFER else PLEDGE_RELEASED
