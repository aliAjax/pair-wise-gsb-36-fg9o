"""Use-case orchestration: domain judgement + SQLite transactions."""
from __future__ import annotations

from datetime import date
from typing import Any

from . import domain
from .domain import (
    DomainError,
    drought_plan,
    enough_for,
    normalize_disposal_outcome,
    parse_date,
    quota_breakdown,
    utcnow,
    validate_beneficiary,
    validate_pledge_amount,
)
from .store import DEFAULT_DB, Store

EPS = 1e-9


class WaterRightsService:
    def __init__(self, path: str | None = None):
        self.store = Store(path or DEFAULT_DB)

    def _account(self, conn, account_id: int) -> Any:
        row = self.store.get_account(conn, account_id)
        if not row:
            raise DomainError("水权账户不存在", 404)
        return row

    def _breakdown(self, conn, account_id: int, exclude_transfer_id: int | None = None):
        account = self._account(conn, account_id)
        pending = self.store.sum_pending_transfers(conn, account_id, exclude_transfer_id)
        totals = self.store.pledge_totals(conn, account_id)
        return account, quota_breakdown(
            float(account["quota"]),
            float(account["used"]),
            pending,
            totals["pledged"],
            totals["frozen"],
        )

    def _audit(self, conn, actor: str, action: str, entity_type: str,
               entity_id: int | None, details: dict[str, Any]) -> None:
        self.store.audit(conn, actor, action, entity_type, entity_id, details)

    # ---- accounts & rules --------------------------------------------
    def create_account(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以创建账户", 403)
        name = str(payload.get("name", "")).strip()
        region = str(payload.get("region", "")).strip()
        holder = str(payload.get("holder", "")).strip()
        if not name or not region or not holder:
            raise DomainError("账户名称、地区和持有人不能为空")
        try:
            priority = int(payload.get("priority"))
            quota = float(payload.get("quota"))
        except (TypeError, ValueError) as exc:
            raise DomainError("优先级和额度必须是数值") from exc
        if not 1 <= priority <= 5 or quota < 0:
            raise DomainError("优先级应在 1 到 5 之间，额度不能为负")
        valid_from = parse_date(str(payload.get("valid_from", "")), "生效日期")
        valid_to = parse_date(str(payload.get("valid_to", "")), "失效日期")
        if valid_from > valid_to:
            raise DomainError("生效日期不能晚于失效日期")
        with self.store.connect() as conn:
            try:
                account_id = self.store.insert_account(conn, {
                    "name": name, "region": region, "holder": holder, "priority": priority,
                    "valid_from": valid_from.isoformat(), "valid_to": valid_to.isoformat(), "quota": quota,
                })
            except Exception as exc:  # sqlite3.IntegrityError on unique name
                raise DomainError("账户名称已存在", 409) from exc
            self._audit(conn, actor, "account.created", "account", account_id, {"name": name, "quota": quota})
            return dict(self.store.get_account(conn, account_id))

    def set_season_rule(self, actor: str, region: str, month: int, max_fraction: float,
                        note: str = "", role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以设置季节规则", 403)
        if not 1 <= int(month) <= 12 or not 0 < float(max_fraction) <= 1:
            raise DomainError("月份或季节比例不合法")
        with self.store.connect() as conn:
            self.store.upsert_season_rule(conn, region.strip(), int(month), float(max_fraction), note)
            self._audit(conn, actor, "season_rule.saved", "region", None,
                        {"region": region, "month": month, "max_fraction": max_fraction})
        return {"region": region, "month": month, "max_fraction": float(max_fraction), "note": note}

    def set_impact_rule(self, actor: str, source_region: str, target_region: str, min_source_fraction: float,
                        note: str = "", role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以设置第三方影响规则", 403)
        if not 0 <= float(min_source_fraction) <= 1:
            raise DomainError("最小留存比例必须在 0 到 1 之间")
        with self.store.connect() as conn:
            self.store.upsert_impact_rule(conn, source_region, target_region, float(min_source_fraction), note)
            self._audit(conn, actor, "impact_rule.saved", "region", None,
                        {"source": source_region, "target": target_region, "min_fraction": min_source_fraction})
        return {"source_region": source_region, "target_region": target_region,
                "min_source_fraction": float(min_source_fraction), "note": note}

    def available(self, account_id: int, as_of: str | None = None) -> dict[str, Any]:
        if as_of:
            parse_date(as_of, "查询日期")
        with self.store.connect() as conn:
            account, breakdown = self._breakdown(conn, account_id)
        return {
            "account_id": account_id,
            "available": breakdown.available,
            "quota": account["quota"],
            "used": account["used"],
            "pending_transfer": breakdown.pending_transfer,
            "reserved_outgoing": breakdown.pending_transfer,
            "pledged": breakdown.pledged,
            "frozen": breakdown.frozen,
            "pending_disposal": breakdown.frozen,
        }

    def account_detail(self, account_id: int) -> dict[str, Any]:
        with self.store.connect() as conn:
            account, breakdown = self._breakdown(conn, account_id)
            pledges = [dict(r) for r in self.store.list_pledges(conn, account_id)]
        disposal_ids = {p["id"] for p in pledges}
        detail: dict[str, Any] = dict(account)
        detail.update({
            "available": breakdown.available,
            "used": breakdown.used,
            "pending_transfer": breakdown.pending_transfer,
            "pledged_total": breakdown.pledged,
            "frozen_total": breakdown.frozen,
            "pending_disposal_total": breakdown.frozen,
        })
        if disposal_ids:
            with self.store.connect() as conn:
                disposal_map = {d["pledge_id"]: dict(d) for d in self.store.list_disposals(conn)
                                if d["pledge_id"] in disposal_ids}
        else:
            disposal_map = {}
        detail["pledges"] = [
            {**p, "disposal": disposal_map.get(p["id"])} for p in pledges
        ]
        return detail

    def list_accounts(self) -> list[dict[str, Any]]:
        result = []
        with self.store.connect() as conn:
            for row in self.store.list_accounts(conn):
                _, breakdown = self._breakdown(conn, int(row["id"]))
                item = dict(row)
                item.update({
                    "available": breakdown.available,
                    "pending_transfer": breakdown.pending_transfer,
                    "pledged": breakdown.pledged,
                    "frozen": breakdown.frozen,
                })
                result.append(item)
        return result

    def list_transfers(self) -> list[dict[str, Any]]:
        with self.store.connect() as conn:
            return [dict(r) for r in self.store.list_transfers(conn)]

    def list_pledges(self) -> list[dict[str, Any]]:
        with self.store.connect() as conn:
            return [dict(r) for r in self.store.list_pledges(conn)]

    def list_disposals(self) -> list[dict[str, Any]]:
        with self.store.connect() as conn:
            return [dict(r) for r in self.store.list_disposals(conn)]

    def audit_log(self) -> list[dict[str, Any]]:
        with self.store.connect() as conn:
            return self.store.list_audit(conn)

    # ---- transfers ----------------------------------------------------
    def create_transfer(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有水权编辑人员可以发起转让", 403)
        try:
            source_id = int(payload.get("from_account_id"))
            target_id = int(payload.get("to_account_id"))
            amount = float(payload.get("amount"))
        except (TypeError, ValueError) as exc:
            raise DomainError("账户和转让量必须是数值") from exc
        if source_id == target_id or amount <= 0:
            raise DomainError("转让账户不能相同，转让量必须大于 0")
        effective = parse_date(str(payload.get("effective_date", "")), "生效日期")
        # BEGIN IMMEDIATE serialises concurrent pledges/transfers: whichever
        # writer commits first occupies the quota, the other sees it gone.
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self._account(conn, source_id)
            target = self._account(conn, target_id)
            if not (source["valid_from"] <= effective.isoformat() <= source["valid_to"]):
                raise DomainError("转出账户在生效日无效", 409)
            if not (target["valid_from"] <= effective.isoformat() <= target["valid_to"]):
                raise DomainError("转入账户在生效日无效", 409)
            _, breakdown = self._breakdown(conn, source_id)
            if not enough_for(amount, breakdown):
                raise DomainError("可用额度不足：同一额度不能同时被质押、待审转让和取水占用", 409)
            if int(source["priority"]) > int(target["priority"]):
                raise DomainError("不能把较低优先级水量转给更高优先级账户", 409)
            impact = self.store.get_impact_rule(conn, source["region"], target["region"])
            if impact:
                minimum = float(source["quota"]) * float(impact["min_source_fraction"])
                if breakdown.available - amount + EPS < minimum:
                    raise DomainError("转让会违反下游第三方最小留存约束", 409)
            transfer_id = self.store.insert_transfer(conn, {
                "from_account_id": source_id, "to_account_id": target_id, "amount": amount,
                "effective_date": effective.isoformat(), "created_by": actor,
            })
            self._audit(conn, actor, "transfer.created", "transfer", transfer_id,
                        {"source": source_id, "target": target_id, "amount": amount,
                         "effective_date": effective.isoformat()})
            return dict(self.store.get_transfer(conn, transfer_id))

    def approve_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        if role != "reviewer":
            raise DomainError("只有审核人可以批准转让", 403)
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            transfer = self.store.get_transfer(conn, transfer_id)
            if not transfer:
                raise DomainError("转让记录不存在", 404)
            if transfer["status"] != "pending":
                raise DomainError("该转让已处理，不能重复批准", 409)
            if actor == transfer["created_by"]:
                raise DomainError("发起人不能批准自己的转让", 403)
            source = self._account(conn, int(transfer["from_account_id"]))
            target = self._account(conn, int(transfer["to_account_id"]))
            _, breakdown = self._breakdown(conn, source["id"], exclude_transfer_id=transfer_id)
            amount = float(transfer["amount"])
            if not enough_for(amount, breakdown):
                raise DomainError("审批时额度已被质押或其他记录占用，不能批准", 409)
            impact = self.store.get_impact_rule(conn, source["region"], target["region"])
            if impact:
                minimum = float(self._account(conn, int(transfer["from_account_id"]))["quota"]) * float(impact["min_source_fraction"])
                if breakdown.available - amount + EPS < minimum:
                    raise DomainError("审批时下游最小留存约束不再满足", 409)
            self.store.shift_quota(conn, int(transfer["from_account_id"]), -amount)
            self.store.shift_quota(conn, int(transfer["to_account_id"]), amount)
            now = utcnow()
            self.store.mark_transfer(conn, transfer_id, "approved", actor, now)
            self._audit(conn, actor, "transfer.approved", "transfer", transfer_id, {"amount": amount})
            return dict(self.store.get_transfer(conn, transfer_id))

    def reject_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        if role != "reviewer":
            raise DomainError("只有审核人可以退回转让", 403)
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self.store.get_transfer(conn, transfer_id)
            if not row or row["status"] != "pending":
                raise DomainError("转让不存在或已经处理", 409)
            if actor == row["created_by"]:
                raise DomainError("发起人不能自行退回", 403)
            self.store.mark_transfer(conn, transfer_id, "rejected", actor, utcnow())
            self._audit(conn, actor, "transfer.rejected", "transfer", transfer_id, {})
        return {"id": transfer_id, "status": "rejected"}

    def record_usage(self, actor: str, payload: dict[str, Any], role: str = "meter") -> dict[str, Any]:
        if role not in {"meter", "editor"}:
            raise DomainError("只有计量员可以登记取水", 403)
        try:
            account_id = int(payload.get("account_id"))
            amount = float(payload.get("amount"))
        except (TypeError, ValueError) as exc:
            raise DomainError("账户和取水量必须是数值") from exc
        meter_event_id = str(payload.get("meter_event_id", "")).strip()
        occurred = parse_date(str(payload.get("occurred_at", "")), "计量日期")
        if amount <= 0 or not meter_event_id:
            raise DomainError("取水量必须大于 0，计量事件编号不能为空")
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = self._account(conn, account_id)
            if not (account["valid_from"] <= occurred.isoformat() <= account["valid_to"]):
                raise DomainError("取水日期不在许可有效期内", 409)
            _, breakdown = self._breakdown(conn, account_id)
            if not enough_for(amount, breakdown):
                raise DomainError("取水超过可用额度：质押或待审转让中的额度不能取水占用", 409)
            season = self.store.get_season_rule(conn, account["region"], occurred.month)
            if season:
                cap = float(account["quota"]) * float(season["max_fraction"])
                month_total = self.store.month_usage(conn, account_id, occurred.strftime("%Y-%m"))
                if float(month_total) + amount > cap + EPS:
                    raise DomainError("本次取水超过该月份的季节配额", 409)
            data = {"account_id": account_id, "meter_event_id": meter_event_id,
                    "amount": amount, "occurred_at": occurred.isoformat(), "actor": actor}
            try:
                usage_id = self.store.insert_usage(conn, data)
            except Exception as exc:
                raise DomainError("计量事件已登记，不能重复计水", 409) from exc
            self.store.add_used(conn, account_id, amount)
            self._audit(conn, actor, "usage.recorded", "account", account_id,
                        {"amount": amount, "occurred_at": occurred.isoformat(),
                         "meter_event_id": meter_event_id})
            return dict(conn.execute("SELECT * FROM usage_records WHERE id=?", (usage_id,)).fetchone())

    def simulate_drought(self, total_supply: float, reduction: float = 0.0) -> dict[str, Any]:
        try:
            total_supply, reduction = float(total_supply), float(reduction)
        except (TypeError, ValueError) as exc:
            raise DomainError("供水量和削减比例必须是数值") from exc
        if total_supply < 0 or not 0 <= reduction < 1:
            raise DomainError("供水量不能为负，削减比例应在 0 到 1 之间")
        with self.store.connect() as conn:
            rows = [dict(r) for r in self.store.list_accounts(conn)]
        plan = drought_plan(rows, total_supply, reduction)
        return {
            "total_supply": total_supply,
            "reduction": reduction,
            "effective_supply": plan["effective_supply"],
            "unallocated": plan["unallocated"],
            "allocations": [
                {"account_id": int(r["id"]), "name": r["name"], "priority": r["priority"],
                 "allocation": plan["allocation"].get(int(r["id"]), 0.0),
                 "deficit": plan["deficit"].get(int(r["id"]), 0.0)}
                for r in rows
            ],
        }

    # ---- pledges ------------------------------------------------------
    def create_pledge(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        """Register a quota pledge. Amount, maturity date and beneficiary are
        captured from the holder; quota must not already be pledged, pending
        transfer or consumed by usage."""
        if role != "editor":
            raise DomainError("只有账户持有人或水权编辑人员可以提交质押", 403)
        try:
            account_id = int(payload.get("account_id"))
        except (TypeError, ValueError) as exc:
            raise DomainError("账户必须是数值") from exc
        amount = validate_pledge_amount(payload.get("amount"))
        maturity = parse_date(str(payload.get("maturity_date", "")), "到期日")
        beneficiary = validate_beneficiary(payload.get("beneficiary"))
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._account(conn, account_id)
            _, breakdown = self._breakdown(conn, account_id)
            if not enough_for(amount, breakdown):
                raise DomainError("可质押额度不足：同一额度不能同时被质押、待审转让和取水占用", 409)
            pledge_id = self.store.insert_pledge(conn, {
                "account_id": account_id, "amount": amount,
                "maturity_date": maturity.isoformat(),
                "beneficiary": beneficiary, "created_by": actor,
            })
            self._audit(conn, actor, "pledge.created", "pledge", pledge_id,
                        {"account_id": account_id, "amount": amount,
                         "maturity_date": maturity.isoformat(), "beneficiary": beneficiary})
            return dict(self.store.get_pledge(conn, pledge_id))

    def file_disposal(self, actor: str, payload: dict[str, Any], role: str = "editor",
                      *, today: date | None = None) -> dict[str, Any]:
        """Beneficiary files a disposal order after maturity. Filing freezes the
        pledged quota first; the later review turns it into a transfer or a
        release."""
        try:
            pledge_id = int(payload.get("pledge_id"))
        except (TypeError, ValueError) as exc:
            raise DomainError("质押 ID 必须是数值") from exc
        today = today or date.today()
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pledge = self.store.get_pledge(conn, pledge_id)
            if not pledge:
                raise DomainError("质押记录不存在", 404)
            domain.can_apply_disposal(dict(pledge), actor, role, today=today)
            disposal_id = self.store.insert_disposal(conn, {
                "pledge_id": pledge_id, "amount": float(pledge["amount"]),
                "requested_by": actor,
            })
            self.store.mark_pledge(conn, pledge_id, "frozen", None)
            self._audit(conn, actor, "disposal.filed", "disposal", disposal_id,
                        {"pledge_id": pledge_id, "amount": pledge["amount"]})
            return dict(self.store.get_disposal(conn, disposal_id))

    def decide_disposal(self, disposal_id: int, actor: str, payload: dict[str, Any] | None = None,
                        role: str = "reviewer") -> dict[str, Any]:
        """Review a disposal order: approve (transfer frozen quota to the
        beneficiary's account) or reject (release it back to the holder).

        The decision and its quota movement are one transaction keyed by the
        disposal id. Retrying the same disposal after a failed write returns the
        existing outcome without deducting quota twice."""
        if role != "reviewer":
            raise DomainError("只有审核人可以作出处置审批", 403)
        payload = payload or {}
        outcome = normalize_disposal_outcome(payload)
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            disposal = self.store.get_disposal(conn, disposal_id)
            if not disposal:
                raise DomainError("处置单不存在", 404)
            pledge = self.store.get_pledge(conn, int(disposal["pledge_id"]))
            if disposal["status"] != "applied":
                # Idempotent retry: the unique transfers.deposal_id index and
                # the disposal state together make a second run a no-op.
                existing = self.store.get_disposal_transfer(conn, disposal_id)
                result = dict(disposal)
                if existing:
                    result["transfer"] = dict(existing)
                if disposal["decision"] and disposal["decision"] != outcome["decision"]:
                    raise DomainError("该处置单已有相反的审批结果，不能更改", 409)
                return result
            if actor == disposal["requested_by"]:
                raise DomainError("处置申请人不能审批自己的处置单", 403)
            amount = float(disposal["amount"])
            now = utcnow()
            if outcome["decision"] == "release":
                self.store.mark_pledge(conn, int(pledge["id"]), "released", now)
                self.store.mark_disposal(conn, disposal_id, "released", "release", actor, None, now)
                self._audit(conn, actor, "disposal.released", "disposal", disposal_id,
                            {"pledge_id": pledge["id"], "amount": amount})
                return dict(self.store.get_disposal(conn, disposal_id))

            target_id = int(outcome["transfer_to_account_id"])
            if target_id == int(pledge["account_id"]):
                raise DomainError("处置转让账户不能是出质账户本身")
            target = self.store.get_account(conn, target_id)
            if not target:
                raise DomainError("受益人转入账户不存在", 404)
            existing = self.store.get_disposal_transfer(conn, disposal_id)
            if existing is None:
                transfer_id = self.store.insert_transfer(conn, {
                    "from_account_id": int(pledge["account_id"]),
                    "to_account_id": target_id,
                    "amount": amount,
                    "effective_date": now[:10],
                    "status": "approved",
                    "created_by": actor,
                    "approved_by": actor,
                    "created_at": now,
                    "approved_at": now,
                    "disposal_id": disposal_id,
                })
            else:
                transfer_id = int(existing["id"])
            # Frozen quota is not part of the holder's free quota, but the
            # quota column still holds it; moving it realises the disposal.
            self.store.shift_quota(conn, int(pledge["account_id"]), -amount)
            self.store.shift_quota(conn, target_id, amount)
            self.store.mark_pledge(conn, int(pledge["id"]), "transferred", now)
            self.store.mark_disposal(conn, disposal_id, "transferred", "transfer", actor, target_id, now)
            self._audit(conn, actor, "disposal.transferred", "disposal", disposal_id,
                        {"pledge_id": pledge["id"], "amount": amount,
                         "transfer_to_account_id": target_id, "transfer_id": transfer_id})
            result = dict(self.store.get_disposal(conn, disposal_id))
            result["transfer"] = dict(self.store.get_disposal_transfer(conn, disposal_id))
            return result

    # Backwards-compatible alias used by the HTTP layer/tests.
    def audit(self) -> list[dict[str, Any]]:
        return self.audit_log()
