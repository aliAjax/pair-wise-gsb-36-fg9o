"""SQLite storage gateway for the water-rights service.

Only schema, queries and row mapping live here; business judgement belongs in
:mod:`waterright.domain` and orchestration in :mod:`waterright.service`.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .domain import utcnow

DEFAULT_DB = Path(__file__).resolve().parent.parent / "water_rights.db"


class Store:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self) -> None:
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
                    approved_at TEXT
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
                    amount REAL NOT NULL CHECK(amount > 0),
                    maturity_date TEXT NOT NULL,
                    beneficiary TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pledged',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS disposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pledge_id INTEGER NOT NULL UNIQUE REFERENCES pledges(id),
                    amount REAL NOT NULL CHECK(amount > 0),
                    status TEXT NOT NULL DEFAULT 'applied',
                    requested_by TEXT NOT NULL,
                    decided_by TEXT,
                    decision TEXT,
                    transfer_to_account_id INTEGER REFERENCES accounts(id),
                    created_at TEXT NOT NULL,
                    decided_at TEXT
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
            # Migrate databases created before disposal-linked transfers existed.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(transfers)").fetchall()}
            if "disposal_id" not in cols:
                conn.execute("ALTER TABLE transfers ADD COLUMN disposal_id INTEGER REFERENCES disposals(id)")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_transfers_disposal ON transfers(disposal_id) "
                "WHERE disposal_id IS NOT NULL"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pledges_account ON pledges(account_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_disposals_status ON disposals(status)")

    # ---- common -------------------------------------------------------
    def audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
              entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    def list_audit(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()]

    # ---- accounts -----------------------------------------------------
    def insert_account(self, conn: sqlite3.Connection, data: dict[str, Any]) -> int:
        cur = conn.execute(
            "INSERT INTO accounts(name,region,holder,priority,valid_from,valid_to,quota,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (data["name"], data["region"], data["holder"], data["priority"],
             data["valid_from"], data["valid_to"], data["quota"], utcnow()),
        )
        return int(cur.lastrowid)

    def get_account(self, conn: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()

    def list_accounts(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()

    def shift_quota(self, conn: sqlite3.Connection, account_id: int, delta: float) -> None:
        conn.execute("UPDATE accounts SET quota=quota+? WHERE id=?", (delta, account_id))

    def add_used(self, conn: sqlite3.Connection, account_id: int, amount: float) -> None:
        conn.execute("UPDATE accounts SET used=used+? WHERE id=?", (amount, account_id))

    # ---- rules --------------------------------------------------------
    def upsert_season_rule(self, conn: sqlite3.Connection, region: str, month: int,
                           max_fraction: float, note: str) -> None:
        conn.execute(
            "INSERT INTO season_rules(region,month,max_fraction,note) VALUES(?,?,?,?) "
            "ON CONFLICT(region,month) DO UPDATE SET max_fraction=excluded.max_fraction,note=excluded.note",
            (region, month, max_fraction, note),
        )

    def upsert_impact_rule(self, conn: sqlite3.Connection, source: str, target: str,
                           fraction: float, note: str) -> None:
        conn.execute(
            "INSERT INTO impact_rules(source_region,target_region,min_source_fraction,note) VALUES(?,?,?,?) "
            "ON CONFLICT(source_region,target_region) DO UPDATE SET "
            "min_source_fraction=excluded.min_source_fraction,note=excluded.note",
            (source, target, fraction, note),
        )

    def get_season_rule(self, conn: sqlite3.Connection, region: str, month: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT max_fraction FROM season_rules WHERE region=? AND month=?", (region, month)
        ).fetchone()

    def get_impact_rule(self, conn: sqlite3.Connection, source: str, target: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM impact_rules WHERE source_region=? AND target_region=?", (source, target)
        ).fetchone()

    # ---- transfers ----------------------------------------------------
    def sum_pending_transfers(self, conn: sqlite3.Connection, account_id: int,
                              exclude_id: int | None = None) -> float:
        if exclude_id is None:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount),0) total FROM transfers "
                "WHERE from_account_id=? AND status='pending'",
                (account_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount),0) total FROM transfers "
                "WHERE from_account_id=? AND status='pending' AND id<>?",
                (account_id, exclude_id),
            ).fetchone()
        return float(row["total"])

    def insert_transfer(self, conn: sqlite3.Connection, data: dict[str, Any]) -> int:
        cur = conn.execute(
            "INSERT INTO transfers(from_account_id,to_account_id,amount,effective_date,"
            "status,created_by,approved_by,created_at,approved_at,disposal_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (data["from_account_id"], data["to_account_id"], data["amount"], data["effective_date"],
             data.get("status", "pending"), data["created_by"], data.get("approved_by"),
             data.get("created_at", utcnow()), data.get("approved_at"), data.get("disposal_id")),
        )
        return int(cur.lastrowid)

    def get_transfer(self, conn: sqlite3.Connection, transfer_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()

    def list_transfers(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM transfers ORDER BY id DESC").fetchall()

    def get_disposal_transfer(self, conn: sqlite3.Connection, disposal_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM transfers WHERE disposal_id=?", (disposal_id,)
        ).fetchone()

    def mark_transfer(self, conn: sqlite3.Connection, transfer_id: int, status: str,
                      actor: str, at: str) -> None:
        conn.execute(
            "UPDATE transfers SET status=?,approved_by=?,approved_at=? WHERE id=?",
            (status, actor, at, transfer_id),
        )

    # ---- usage --------------------------------------------------------
    def month_usage(self, conn: sqlite3.Connection, account_id: int, month: str) -> float:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) total FROM usage_records "
            "WHERE account_id=? AND substr(occurred_at,1,7)=?",
            (account_id, month),
        ).fetchone()
        return float(row["total"])

    def insert_usage(self, conn: sqlite3.Connection, data: dict[str, Any]) -> int:
        cur = conn.execute(
            "INSERT INTO usage_records(account_id,meter_event_id,amount,occurred_at,actor,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (data["account_id"], data["meter_event_id"], data["amount"],
             data["occurred_at"], data["actor"], utcnow()),
        )
        return int(cur.lastrowid)

    # ---- pledges ------------------------------------------------------
    def insert_pledge(self, conn: sqlite3.Connection, data: dict[str, Any]) -> int:
        cur = conn.execute(
            "INSERT INTO pledges(account_id,amount,maturity_date,beneficiary,created_by,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (data["account_id"], data["amount"], data["maturity_date"],
             data["beneficiary"], data["created_by"], utcnow()),
        )
        return int(cur.lastrowid)

    def get_pledge(self, conn: sqlite3.Connection, pledge_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM pledges WHERE id=?", (pledge_id,)).fetchone()

    def list_pledges(self, conn: sqlite3.Connection, account_id: int | None = None) -> list[sqlite3.Row]:
        if account_id is None:
            return conn.execute("SELECT * FROM pledges ORDER BY id DESC").fetchall()
        return conn.execute(
            "SELECT * FROM pledges WHERE account_id=? ORDER BY id DESC", (account_id,)
        ).fetchall()

    def pledge_totals(self, conn: sqlite3.Connection, account_id: int) -> dict[str, float]:
        rows = conn.execute(
            "SELECT status, COALESCE(SUM(amount),0) total FROM pledges "
            "WHERE account_id=? AND status IN ('pledged','frozen') GROUP BY status",
            (account_id,),
        ).fetchall()
        totals = {"pledged": 0.0, "frozen": 0.0}
        for row in rows:
            totals[row["status"]] = float(row["total"])
        return totals

    def mark_pledge(self, conn: sqlite3.Connection, pledge_id: int, status: str, at: str) -> None:
        conn.execute(
            "UPDATE pledges SET status=?, finished_at=? WHERE id=?", (status, at, pledge_id)
        )

    # ---- disposals ----------------------------------------------------
    def insert_disposal(self, conn: sqlite3.Connection, data: dict[str, Any]) -> int:
        cur = conn.execute(
            "INSERT INTO disposals(pledge_id,amount,requested_by,created_at) VALUES(?,?,?,?)",
            (data["pledge_id"], data["amount"], data["requested_by"], utcnow()),
        )
        return int(cur.lastrowid)

    def get_disposal(self, conn: sqlite3.Connection, disposal_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM disposals WHERE id=?", (disposal_id,)).fetchone()

    def list_disposals(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM disposals ORDER BY id DESC").fetchall()

    def mark_disposal(self, conn: sqlite3.Connection, disposal_id: int, status: str,
                      decision: str, actor: str, target_id: int | None, at: str) -> None:
        conn.execute(
            "UPDATE disposals SET status=?,decision=?,decided_by=?,transfer_to_account_id=?,"
            "decided_at=? WHERE id=?",
            (status, decision, actor, target_id, at, disposal_id),
        )
