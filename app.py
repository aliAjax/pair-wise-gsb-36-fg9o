"""跨区域水权分配、转让与质押担保登记服务（仅使用 Python 标准库）。

分层：errors 定义领域错误；collateral 是纯判定逻辑（额度互斥、质押/处置状态机）；
本模块负责 SQLite 存储与 HTTP 接口；页面在 static/index.html。
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import sqlite3
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import collateral
from errors import DomainError

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "water_rights.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value: str, field: str = "日期") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"{field}必须是 YYYY-MM-DD") from exc


class Database:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    region TEXT NOT NULL,
                    holder TEXT NOT NULL,
                    priority INTEGER NOT NULL CHECK(priority BETWEEN 1 AND 5),
                    valid_from TEXT NOT NULL,
                    valid_to TEXT NOT NULL,
                    quota REAL NOT NULL CHECK(quota >= 0),
                    used REAL NOT NULL DEFAULT 0 CHECK(used >= 0),
                    created_at TEXT NOT NULL,
                    CHECK(valid_from <= valid_to)
                );
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_account_id INTEGER NOT NULL REFERENCES accounts(id),
                    to_account_id INTEGER NOT NULL REFERENCES accounts(id),
                    amount REAL NOT NULL CHECK(amount > 0),
                    effective_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_by TEXT NOT NULL,
                    approved_by TEXT,
                    created_at TEXT NOT NULL,
                    approved_at TEXT,
                    disposal_id INTEGER REFERENCES disposals(id)
                );
                CREATE TABLE IF NOT EXISTS usage_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    meter_event_id TEXT NOT NULL,
                    amount REAL NOT NULL CHECK(amount > 0),
                    occurred_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(account_id, meter_event_id)
                );
                CREATE TABLE IF NOT EXISTS season_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    region TEXT NOT NULL,
                    month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
                    max_fraction REAL NOT NULL CHECK(max_fraction > 0 AND max_fraction <= 1),
                    note TEXT NOT NULL DEFAULT '',
                    UNIQUE(region, month)
                );
                CREATE TABLE IF NOT EXISTS impact_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_region TEXT NOT NULL,
                    target_region TEXT NOT NULL,
                    min_source_fraction REAL NOT NULL CHECK(min_source_fraction >= 0 AND min_source_fraction <= 1),
                    note TEXT NOT NULL DEFAULT '',
                    UNIQUE(source_region, target_region)
                );
                CREATE TABLE IF NOT EXISTS pledges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    beneficiary TEXT NOT NULL,
                    amount REAL NOT NULL CHECK(amount > 0),
                    expires_on TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','frozen','disposed','released')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS disposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pledge_id INTEGER NOT NULL REFERENCES pledges(id),
                    applicant TEXT NOT NULL,
                    target_account_id INTEGER NOT NULL REFERENCES accounts(id),
                    status TEXT NOT NULL DEFAULT 'frozen' CHECK(status IN ('frozen','transferred','released')),
                    decision TEXT,
                    decided_by TEXT,
                    decided_at TEXT,
                    transfer_id INTEGER REFERENCES transfers(id),
                    created_at TEXT NOT NULL,
                    UNIQUE(pledge_id)
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """旧库兼容：transfers 补 disposal_id 列；已有记录的该列为 NULL，按未质押处理。"""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(transfers)")}
        if "disposal_id" not in columns:
            conn.execute("ALTER TABLE transfers ADD COLUMN disposal_id INTEGER REFERENCES disposals(id)")
        # 同一处置单最多生成一笔转让，是幂等重试的兜底约束
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_transfers_disposal "
            "ON transfers(disposal_id) WHERE disposal_id IS NOT NULL"
        )

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
               entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

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
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO accounts(name,region,holder,priority,valid_from,valid_to,quota,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (name, region, holder, priority, valid_from.isoformat(), valid_to.isoformat(), quota, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("账户名称已存在", 409) from exc
            self._audit(conn, actor, "account.created", "account", cur.lastrowid, {"name": name, "quota": quota})
            return dict(conn.execute("SELECT * FROM accounts WHERE id=?", (cur.lastrowid,)).fetchone())

    def set_season_rule(self, actor: str, region: str, month: int, max_fraction: float,
                        note: str = "", role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以设置季节规则", 403)
        if not 1 <= int(month) <= 12 or not 0 < float(max_fraction) <= 1:
            raise DomainError("月份或季节比例不合法")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO season_rules(region,month,max_fraction,note) VALUES(?,?,?,?)
                   ON CONFLICT(region,month) DO UPDATE SET max_fraction=excluded.max_fraction,note=excluded.note""",
                (region.strip(), int(month), float(max_fraction), note),
            )
            self._audit(conn, actor, "season_rule.saved", "region", None, {"region": region, "month": month, "max_fraction": max_fraction})
        return {"region": region, "month": month, "max_fraction": max_fraction, "note": note}

    def set_impact_rule(self, actor: str, source_region: str, target_region: str, min_source_fraction: float,
                        note: str = "", role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以设置第三方影响规则", 403)
        if not 0 <= float(min_source_fraction) <= 1:
            raise DomainError("最小留存比例必须在 0 到 1 之间")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO impact_rules(source_region,target_region,min_source_fraction,note) VALUES(?,?,?,?)
                   ON CONFLICT(source_region,target_region) DO UPDATE SET min_source_fraction=excluded.min_source_fraction,note=excluded.note""",
                (source_region, target_region, float(min_source_fraction), note),
            )
            self._audit(conn, actor, "impact_rule.saved", "region", None, {"source": source_region, "target": target_region, "min_fraction": min_source_fraction})
        return {"source_region": source_region, "target_region": target_region, "min_source_fraction": min_source_fraction, "note": note}

    def _account_row(self, conn: sqlite3.Connection, account_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise DomainError("水权账户不存在", 404)
        return row

    def _reserved_outgoing(self, conn: sqlite3.Connection, account_id: int) -> float:
        row = conn.execute("SELECT COALESCE(SUM(amount),0) total FROM transfers WHERE from_account_id=? AND status='pending'", (account_id,)).fetchone()
        return float(row["total"])

    def _pledged_amounts(self, conn: sqlite3.Connection, account_id: int) -> tuple[float, float]:
        """返回 (质押中, 待处置冻结) 两档占用；两档都从可用额度中扣除。"""
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN status=? THEN amount END),0) active, "
            "COALESCE(SUM(CASE WHEN status=? THEN amount END),0) frozen "
            "FROM pledges WHERE account_id=? AND status IN (?,?)",
            (collateral.PLEDGE_ACTIVE, collateral.PLEDGE_FROZEN, account_id,
             collateral.PLEDGE_ACTIVE, collateral.PLEDGE_FROZEN),
        ).fetchone()
        return float(row["active"]), float(row["frozen"])

    def available(self, account_id: int, as_of: str | None = None) -> dict[str, Any]:
        if as_of:
            parse_date(as_of, "查询日期")
        with self.connect() as conn:
            account = self._account_row(conn, account_id)
            reserved = self._reserved_outgoing(conn, account_id)
            pledged_active, pledged_frozen = self._pledged_amounts(conn, account_id)
            value = collateral.compute_available(account["quota"], account["used"], reserved, pledged_active + pledged_frozen)
        return {"account_id": account_id, "available": value, "reserved_outgoing": reserved,
                "pledged": pledged_active, "frozen": pledged_frozen,
                "quota": account["quota"], "used": account["used"]}

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
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self._account_row(conn, source_id)
            target = self._account_row(conn, target_id)
            if not (source["valid_from"] <= effective.isoformat() <= source["valid_to"]):
                raise DomainError("转出账户在生效日无效", 409)
            if not (target["valid_from"] <= effective.isoformat() <= target["valid_to"]):
                raise DomainError("转入账户在生效日无效", 409)
            reserved = self._reserved_outgoing(conn, source_id)
            pledged_active, pledged_frozen = self._pledged_amounts(conn, source_id)
            available = collateral.compute_available(source["quota"], source["used"], reserved, pledged_active + pledged_frozen)
            if amount > available + collateral.EPS:
                raise DomainError("可用额度不足，待审批转让和质押会预占额度", 409)
            # More critical users (smaller priority number) cannot transfer their
            # protected allocation to a less critical user.
            if int(source["priority"]) > int(target["priority"]):
                raise DomainError("不能把较低优先级水量转给更高优先级账户", 409)
            impact = conn.execute(
                "SELECT * FROM impact_rules WHERE source_region=? AND target_region=?",
                (source["region"], target["region"]),
            ).fetchone()
            if impact:
                remaining = available - amount
                minimum = float(source["quota"]) * float(impact["min_source_fraction"])
                if remaining + 1e-9 < minimum:
                    raise DomainError("转让会违反下游第三方最小留存约束", 409)
            cur = conn.execute(
                "INSERT INTO transfers(from_account_id,to_account_id,amount,effective_date,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (source_id, target_id, amount, effective.isoformat(), actor, utcnow()),
            )
            self._audit(conn, actor, "transfer.created", "transfer", cur.lastrowid,
                        {"source": source_id, "target": target_id, "amount": amount, "effective_date": effective.isoformat()})
            return dict(conn.execute("SELECT * FROM transfers WHERE id=?", (cur.lastrowid,)).fetchone())

    def approve_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        if role != "reviewer":
            raise DomainError("只有审核人可以批准转让", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            transfer = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if not transfer:
                raise DomainError("转让记录不存在", 404)
            if transfer["status"] != "pending":
                raise DomainError("该转让已处理，不能重复批准", 409)
            if actor == transfer["created_by"]:
                raise DomainError("发起人不能批准自己的转让", 403)
            source = self._account_row(conn, transfer["from_account_id"])
            target = self._account_row(conn, transfer["to_account_id"])
            amount = float(transfer["amount"])
            # Compute against pending reservations other than this transfer.
            other_reserved = conn.execute(
                "SELECT COALESCE(SUM(amount),0) total FROM transfers WHERE from_account_id=? AND status='pending' AND id<>?",
                (source["id"], transfer_id),
            ).fetchone()["total"]
            pledged_active, pledged_frozen = self._pledged_amounts(conn, int(source["id"]))
            available = float(source["quota"]) - float(source["used"]) - float(other_reserved) - pledged_active - pledged_frozen
            if amount > available + 1e-9:
                raise DomainError("审批时额度已被其他记录占用，不能批准", 409)
            impact = conn.execute(
                "SELECT * FROM impact_rules WHERE source_region=? AND target_region=?",
                (source["region"], target["region"]),
            ).fetchone()
            if impact:
                minimum = float(source["quota"]) * float(impact["min_source_fraction"])
                if available - amount + 1e-9 < minimum:
                    raise DomainError("审批时下游最小留存约束不再满足", 409)
            # The approved amount moves between quota balances. Keeping the
            # movement in the quota column preserves the original allocation
            # while making every downstream availability calculation consistent.
            conn.execute("UPDATE accounts SET quota=quota-? WHERE id=?", (amount, source["id"]))
            conn.execute("UPDATE accounts SET quota=quota+? WHERE id=?", (amount, target["id"]))
            conn.execute("UPDATE transfers SET status='approved',approved_by=?,approved_at=? WHERE id=?", (actor, utcnow(), transfer_id))
            self._audit(conn, actor, "transfer.approved", "transfer", transfer_id, {"amount": amount})
            row = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        return dict(row)

    def reject_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        if role != "reviewer":
            raise DomainError("只有审核人可以退回转让", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if not row or row["status"] != "pending":
                raise DomainError("转让不存在或已经处理", 409)
            if actor == row["created_by"]:
                raise DomainError("发起人不能自行退回", 403)
            conn.execute("UPDATE transfers SET status='rejected',approved_by=?,approved_at=? WHERE id=?", (actor, utcnow(), transfer_id))
            self._audit(conn, actor, "transfer.rejected", "transfer", transfer_id, {})
        return {"id": transfer_id, "status": "rejected"}

    # ---- 质押担保登记 ----

    def create_pledge(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有水权编辑人员可以登记质押", 403)
        try:
            account_id = int(payload.get("account_id"))
        except (TypeError, ValueError) as exc:
            raise DomainError("质押账户必须是数值") from exc
        amount, beneficiary, expires_on = collateral.ensure_pledge_input(
            payload.get("amount"), payload.get("beneficiary"), payload.get("expires_on"))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = self._account_row(conn, account_id)
            reserved = self._reserved_outgoing(conn, account_id)
            pledged_active, pledged_frozen = self._pledged_amounts(conn, account_id)
            available = collateral.compute_available(account["quota"], account["used"], reserved, pledged_active + pledged_frozen)
            collateral.ensure_quota_available(amount, available)
            cur = conn.execute(
                "INSERT INTO pledges(account_id,beneficiary,amount,expires_on,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (account_id, beneficiary, amount, expires_on.isoformat(), actor, utcnow()),
            )
            self._audit(conn, actor, "pledge.created", "pledge", cur.lastrowid,
                        {"account_id": account_id, "amount": amount, "beneficiary": beneficiary,
                         "expires_on": expires_on.isoformat()})
            return dict(conn.execute("SELECT * FROM pledges WHERE id=?", (cur.lastrowid,)).fetchone())

    def release_pledge(self, pledge_id: int, actor: str, role: str = "editor") -> dict[str, Any]:
        """履约后解除质押；冻结中的质押必须走处置审批，不能在此直接解除。"""
        if role != "editor":
            raise DomainError("只有水权编辑人员可以解除质押", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pledge = conn.execute("SELECT * FROM pledges WHERE id=?", (pledge_id,)).fetchone()
            if not pledge:
                raise DomainError("质押记录不存在", 404)
            collateral.ensure_releasable(pledge["status"])
            conn.execute("UPDATE pledges SET status=? WHERE id=?", (collateral.PLEDGE_RELEASED, pledge_id))
            self._audit(conn, actor, "pledge.released", "pledge", pledge_id, {"amount": float(pledge["amount"])})
            return dict(conn.execute("SELECT * FROM pledges WHERE id=?", (pledge_id,)).fetchone())

    def apply_disposal(self, actor: str, payload: dict[str, Any]) -> dict[str, Any]:
        """受益人对到期未履约的质押申请处置：开立处置单并冻结质押额。"""
        try:
            pledge_id = int(payload.get("pledge_id"))
            target_id = int(payload.get("target_account_id"))
        except (TypeError, ValueError) as exc:
            raise DomainError("处置单需要质押编号和接收账户") from exc
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pledge = conn.execute("SELECT * FROM pledges WHERE id=?", (pledge_id,)).fetchone()
            if not pledge:
                raise DomainError("质押记录不存在", 404)
            if actor != pledge["beneficiary"]:
                raise DomainError("只有受益人可以申请处置", 403)
            if conn.execute("SELECT id FROM disposals WHERE pledge_id=?", (pledge_id,)).fetchone():
                raise DomainError("该质押已存在处置单，可按原处置单查询或重试", 409)
            collateral.ensure_disposable(pledge["status"], date.fromisoformat(pledge["expires_on"]), date.today())
            if target_id == int(pledge["account_id"]):
                raise DomainError("接收账户不能是质押账户本身", 409)
            self._account_row(conn, target_id)
            cur = conn.execute(
                "INSERT INTO disposals(pledge_id,applicant,target_account_id,created_at) VALUES(?,?,?,?)",
                (pledge_id, actor, target_id, utcnow()),
            )
            conn.execute("UPDATE pledges SET status=? WHERE id=?", (collateral.PLEDGE_FROZEN, pledge_id))
            self._audit(conn, actor, "disposal.applied", "disposal", cur.lastrowid,
                        {"pledge_id": pledge_id, "target_account_id": target_id})
            return self._disposal_view(conn, cur.lastrowid)

    def decide_disposal(self, disposal_id: int, actor: str, payload: dict[str, Any], role: str = "reviewer") -> dict[str, Any]:
        """审批处置单：生成等额已批准转让，或释放质押。

        同一处置单重复提交相同审批结果时直接返回原结果，不重复扣减；
        写入失败整体回滚，之后可按同一处置单安全重试。
        """
        if role != "reviewer":
            raise DomainError("只有审核人可以审批处置单", 403)
        decision = collateral.normalize_decision(payload.get("decision"))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            disposal = conn.execute("SELECT * FROM disposals WHERE id=?", (disposal_id,)).fetchone()
            if not disposal:
                raise DomainError("处置单不存在", 404)
            if disposal["status"] != collateral.DISPOSAL_FROZEN:
                if disposal["decision"] == decision:
                    return self._disposal_view(conn, disposal_id)
                raise DomainError("处置单已按其他审批结果处理，不能更改", 409)
            pledge = conn.execute("SELECT * FROM pledges WHERE id=?", (disposal["pledge_id"],)).fetchone()
            amount = float(pledge["amount"])
            now = utcnow()
            transfer_id = None
            if decision == collateral.DECISION_TRANSFER:
                target = self._account_row(conn, int(disposal["target_account_id"]))
                cur = conn.execute(
                    "INSERT INTO transfers(from_account_id,to_account_id,amount,effective_date,status,"
                    "created_by,approved_by,created_at,approved_at,disposal_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (int(pledge["account_id"]), int(target["id"]), amount, date.today().isoformat(),
                     "approved", disposal["applicant"], actor, now, now, disposal_id),
                )
                transfer_id = cur.lastrowid
                conn.execute("UPDATE accounts SET quota=quota-? WHERE id=?", (amount, int(pledge["account_id"])))
                conn.execute("UPDATE accounts SET quota=quota+? WHERE id=?", (amount, int(target["id"])))
            conn.execute("UPDATE pledges SET status=? WHERE id=?",
                         (collateral.final_pledge_status(decision), int(pledge["id"])))
            conn.execute(
                "UPDATE disposals SET status=?,decision=?,decided_by=?,decided_at=?,transfer_id=? WHERE id=?",
                (collateral.final_disposal_status(decision), decision, actor, now, transfer_id, disposal_id),
            )
            self._audit(conn, actor, "disposal.decided", "disposal", disposal_id,
                        {"decision": decision, "pledge_id": int(pledge["id"]), "amount": amount, "transfer_id": transfer_id})
            return self._disposal_view(conn, disposal_id)

    def _disposal_view(self, conn: sqlite3.Connection, disposal_id: int) -> dict[str, Any]:
        disposal = dict(conn.execute("SELECT * FROM disposals WHERE id=?", (disposal_id,)).fetchone())
        disposal["pledge"] = dict(conn.execute("SELECT * FROM pledges WHERE id=?", (disposal["pledge_id"],)).fetchone())
        if disposal.get("transfer_id"):
            disposal["transfer"] = dict(conn.execute("SELECT * FROM transfers WHERE id=?", (disposal["transfer_id"],)).fetchone())
        return disposal

    def list_pledges(self, account_id: int | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM pledges", ()
        if account_id is not None:
            sql, params = "SELECT * FROM pledges WHERE account_id=?", (account_id,)
        with self.connect() as conn:
            rows = conn.execute(sql + " ORDER BY id DESC", params).fetchall()
        return [dict(row) for row in rows]

    def list_disposals(self, account_id: int | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT d.*, p.account_id, p.amount AS pledge_amount, p.beneficiary, p.expires_on "
               "FROM disposals d JOIN pledges p ON p.id=d.pledge_id")
        params: tuple[Any, ...] = ()
        if account_id is not None:
            sql += " WHERE p.account_id=?"
            params = (account_id,)
        with self.connect() as conn:
            rows = conn.execute(sql + " ORDER BY d.id DESC", params).fetchall()
        return [dict(row) for row in rows]

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
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = self._account_row(conn, account_id)
            if not (account["valid_from"] <= occurred.isoformat() <= account["valid_to"]):
                raise DomainError("取水日期不在许可有效期内", 409)
            reserved = self._reserved_outgoing(conn, account_id)
            pledged_active, pledged_frozen = self._pledged_amounts(conn, account_id)
            available = collateral.compute_available(account["quota"], account["used"], reserved, pledged_active + pledged_frozen)
            if amount > available + collateral.EPS:
                raise DomainError("取水超过可用额度", 409)
            season = conn.execute("SELECT max_fraction FROM season_rules WHERE region=? AND month=?", (account["region"], occurred.month)).fetchone()
            month_total = conn.execute(
                "SELECT COALESCE(SUM(amount),0) total FROM usage_records WHERE account_id=? AND substr(occurred_at,1,7)=?",
                (account_id, occurred.strftime("%Y-%m")),
            ).fetchone()["total"]
            if season:
                cap = float(account["quota"]) * float(season["max_fraction"])
                if float(month_total) + amount > cap + 1e-9:
                    raise DomainError("本次取水超过该月份的季节配额", 409)
            try:
                cur = conn.execute(
                    "INSERT INTO usage_records(account_id,meter_event_id,amount,occurred_at,actor,created_at) VALUES(?,?,?,?,?,?)",
                    (account_id, meter_event_id, amount, occurred.isoformat(), actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("计量事件已登记，不能重复计水", 409) from exc
            conn.execute("UPDATE accounts SET used=used+? WHERE id=?", (amount, account_id))
            self._audit(conn, actor, "usage.recorded", "account", account_id,
                        {"amount": amount, "occurred_at": occurred.isoformat(), "meter_event_id": meter_event_id})
            row = conn.execute("SELECT * FROM usage_records WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def simulate_drought(self, total_supply: float, reduction: float = 0.0, role: str = "viewer") -> dict[str, Any]:
        try:
            total_supply, reduction = float(total_supply), float(reduction)
        except (TypeError, ValueError) as exc:
            raise DomainError("供水量和削减比例必须是数值") from exc
        if total_supply < 0 or not 0 <= reduction < 1:
            raise DomainError("供水量不能为负，削减比例应在 0 到 1 之间")
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY priority,name").fetchall()
        supply = total_supply * (1 - reduction)
        allocation: dict[int, float] = {}
        deficit: dict[int, float] = {}
        remaining = supply
        for priority in range(1, 6):
            group = [r for r in rows if int(r["priority"]) == priority]
            if not group:
                continue
            # During shortage, more critical rights receive their remaining
            # allocation first; only then does water flow to lower priorities.
            requested = sum(max(0.0, float(r["quota"]) - float(r["used"])) for r in group)
            take = min(remaining, requested)
            if requested <= 0:
                continue
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
        return {"total_supply": total_supply, "reduction": reduction, "effective_supply": supply,
                "unallocated": remaining, "allocations": [
                    {"account_id": int(r["id"]), "name": r["name"], "priority": r["priority"],
                     "allocation": allocation.get(int(r["id"]), 0.0), "deficit": deficit.get(int(r["id"]), 0.0)}
                    for r in rows
                ]}

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
            result = []
            for row in rows:
                item = dict(row)
                reserved = self._reserved_outgoing(conn, int(row["id"]))
                pledged_active, pledged_frozen = self._pledged_amounts(conn, int(row["id"]))
                item["reserved_outgoing"] = reserved
                item["pledged"] = pledged_active
                item["frozen"] = pledged_frozen
                item["available"] = collateral.compute_available(row["quota"], row["used"], reserved, pledged_active + pledged_frozen)
                result.append(item)
        return result

    def list_transfers(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM transfers ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def audit(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]


def seed_demo(db: Database) -> dict[str, int]:
    if db.list_accounts():
        return {str(a["name"]): int(a["id"]) for a in db.list_accounts()}
    upstream = db.create_account("alice", {"name": "北区水库", "region": "upstream", "holder": "北区水务公司", "priority": 1, "valid_from": "2026-01-01", "valid_to": "2026-12-31", "quota": 1000}, "editor")
    downstream = db.create_account("alice", {"name": "河口灌区", "region": "downstream", "holder": "河口合作社", "priority": 2, "valid_from": "2026-01-01", "valid_to": "2026-12-31", "quota": 500}, "editor")
    db.set_season_rule("alice", "upstream", 7, 0.35, "夏季上限", "editor")
    db.set_impact_rule("alice", "upstream", "downstream", 0.4, "保障河口最小生态流量", "editor")
    db.record_usage("meter-01", {"account_id": upstream["id"], "amount": 100, "meter_event_id": "UP-2026-0001", "occurred_at": "2026-03-01"}, "meter")
    return {"北区水库": int(upstream["id"]), "河口灌区": int(downstream["id"])}


class Handler(BaseHTTPRequestHandler):
    db: Database
    server_version = "WaterRights/1.0"

    def _send(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self) -> None:
        data = (ROOT / "static" / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DomainError("请求体不是合法 JSON") from exc

    def _auth(self) -> tuple[str, str]:
        # HTTP 头按 ISO-8859-1 解码，中文用户名会被误读；尝试按 UTF-8 还原，
        # 使身份栏的中文名能与 JSON 正文存储的受益人匹配。
        def header_text(name: str, default: str) -> str:
            value = self.headers.get(name, default)
            try:
                return value.encode("iso-8859-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                return value
        return header_text("X-User", "anonymous"), header_text("X-Role", "viewer")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/index.html"}:
                return self._html()
            if parsed.path == "/api/health":
                return self._send({"ok": True})
            if parsed.path == "/api/accounts":
                return self._send({"accounts": self.db.list_accounts()})
            if parsed.path == "/api/transfers":
                return self._send({"transfers": self.db.list_transfers()})
            if parsed.path == "/api/pledges":
                q = parse_qs(parsed.query)
                account_id = q.get("account_id", [None])[0]
                return self._send({"pledges": self.db.list_pledges(int(account_id) if account_id else None)})
            if parsed.path == "/api/disposals":
                q = parse_qs(parsed.query)
                account_id = q.get("account_id", [None])[0]
                return self._send({"disposals": self.db.list_disposals(int(account_id) if account_id else None)})
            if parsed.path == "/api/audit":
                return self._send({"audit": self.db.audit()})
            if parsed.path.startswith("/api/accounts/") and parsed.path.endswith("/available"):
                account_id = int(parsed.path.split("/")[3])
                return self._send(self.db.available(account_id))
            if parsed.path == "/api/drought/simulate":
                q = parse_qs(parsed.query)
                return self._send(self.db.simulate_drought(float(q.get("supply", ["0"])[0]), float(q.get("reduction", ["0"])[0])))
            raise DomainError("接口不存在", 404)
        except (ValueError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            actor, role = self._auth()
            body = self._body()
            parts = [p for p in parsed.path.split("/") if p]
            if parts == ["api", "accounts"]:
                return self._send(self.db.create_account(actor, body, role), 201)
            if parts == ["api", "rules", "season"]:
                return self._send(self.db.set_season_rule(actor, str(body.get("region", "")), int(body.get("month", 0)), body.get("max_fraction"), str(body.get("note", "")), role), 201)
            if parts == ["api", "rules", "impact"]:
                return self._send(self.db.set_impact_rule(actor, str(body.get("source_region", "")), str(body.get("target_region", "")), body.get("min_source_fraction"), str(body.get("note", "")), role), 201)
            if parts == ["api", "transfers"]:
                return self._send(self.db.create_transfer(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "approve":
                return self._send(self.db.approve_transfer(int(parts[2]), actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "reject":
                return self._send(self.db.reject_transfer(int(parts[2]), actor, role))
            if parts == ["api", "pledges"]:
                return self._send(self.db.create_pledge(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "pledges"] and parts[3] == "release":
                return self._send(self.db.release_pledge(int(parts[2]), actor, role))
            if parts == ["api", "disposals"]:
                return self._send(self.db.apply_disposal(actor, body), 201)
            if len(parts) == 4 and parts[:2] == ["api", "disposals"] and parts[3] == "decide":
                return self._send(self.db.decide_disposal(int(parts[2]), actor, body, role))
            if parts == ["api", "usage"]:
                return self._send(self.db.record_usage(actor, body, role), 201)
            raise DomainError("接口不存在", 404)
        except (ValueError, TypeError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[water] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="跨区域水资源使用权分配与转让服务")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8007")))
    parser.add_argument("--db", default=os.getenv("WATER_DB", str(DEFAULT_DB)))
    parser.add_argument("--init", action="store_true", help="创建数据库并写入示例账户")
    args = parser.parse_args()
    db = Database(args.db)
    if args.init:
        seed_demo(db)
        print(f"initialized database at {args.db}")
        return
    Handler.db = db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"water-rights listening on http://127.0.0.1:{args.port} (db={args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()
