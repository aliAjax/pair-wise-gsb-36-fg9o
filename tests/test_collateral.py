import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class PledgeFlowTest(unittest.TestCase):
    """质押登记与额度互斥：同一额度不能同时被质押、待审转让和取水占用。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.accounts = seed_demo(self.db)
        # 北区水库：quota 1000，已取水 100，可用 900；河口灌区：quota 500
        self.source = self.accounts["北区水库"]
        self.target = self.accounts["河口灌区"]

    def tearDown(self):
        self.tmp.cleanup()

    def pledge(self, amount=200, expires_on="2026-12-31", account=None,
               beneficiary="河口合作社", actor="alice", role="editor"):
        return self.db.create_pledge(actor, {
            "account_id": account or self.source,
            "amount": amount,
            "expires_on": expires_on,
            "beneficiary": beneficiary,
        }, role)

    def transfer(self, amount, actor="alice"):
        return self.db.create_transfer(actor, {
            "from_account_id": self.source, "to_account_id": self.target,
            "amount": amount, "effective_date": "2026-06-01"}, "editor")

    def test_pledge_blocks_transfer_and_usage(self):
        self.pledge(800)
        view = self.db.available(self.source)
        self.assertEqual(view["pledged"], 800)
        self.assertEqual(view["available"], 100)
        with self.assertRaisesRegex(DomainError, "质押"):
            self.transfer(200)
        with self.assertRaisesRegex(DomainError, "超过可用额度"):
            self.db.record_usage("meter-01", {"account_id": self.source, "amount": 200,
                                              "meter_event_id": "M-P1", "occurred_at": "2026-08-01"}, "meter")
        self.db.record_usage("meter-01", {"account_id": self.source, "amount": 100,
                                          "meter_event_id": "M-P2", "occurred_at": "2026-08-01"}, "meter")
        self.assertEqual(self.db.available(self.source)["available"], 0)

    def test_pending_transfer_blocks_pledge(self):
        self.transfer(500)
        with self.assertRaisesRegex(DomainError, "互斥"):
            self.pledge(500)
        self.assertEqual(self.pledge(400)["status"], "active")
        with self.assertRaisesRegex(DomainError, "互斥"):
            self.pledge(1)

    def test_pledge_validation_and_role(self):
        with self.assertRaisesRegex(DomainError, "编辑人员"):
            self.pledge(100, role="viewer")
        with self.assertRaisesRegex(DomainError, "大于 0"):
            self.pledge(0)
        with self.assertRaisesRegex(DomainError, "数值"):
            self.pledge("abc")
        with self.assertRaisesRegex(DomainError, "受益人"):
            self.pledge(100, beneficiary=" ")
        with self.assertRaisesRegex(DomainError, "YYYY-MM-DD"):
            self.pledge(100, expires_on="2026/12/31")
        with self.assertRaisesRegex(DomainError, "不存在"):
            self.pledge(100, account=999)

    def test_release_active_pledge(self):
        pledge = self.pledge(100)
        with self.assertRaisesRegex(DomainError, "编辑人员"):
            self.db.release_pledge(pledge["id"], "alice", "viewer")
        released = self.db.release_pledge(pledge["id"], "alice", "editor")
        self.assertEqual(released["status"], "released")
        self.assertEqual(self.db.available(self.source)["available"], 900)
        with self.assertRaisesRegex(DomainError, "履约中"):
            self.db.release_pledge(pledge["id"], "alice", "editor")

    def _race(self, first, second):
        barrier = threading.Barrier(2)
        results, errors = {}, {}

        def run(name, fn):
            barrier.wait()
            try:
                results[name] = fn()
            except Exception as exc:  # noqa: BLE001 - 收集后统一断言
                errors[name] = exc

        threads = [threading.Thread(target=run, args=("first", first)),
                   threading.Thread(target=run, args=("second", second))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse(any(t.is_alive() for t in threads))
        return results, errors

    def test_concurrent_pledge_and_transfer_only_one_wins(self):
        # 各自单独都能成功（质押 500<=900；转让 500 后留存 400 满足最小留存），一起必然冲突
        results, errors = self._race(lambda: self.pledge(500), lambda: self.transfer(500))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(next(iter(errors.values())), DomainError)
        view = self.db.available(self.source)
        if "first" in results:
            self.assertEqual(view["pledged"], 500)
            self.assertEqual(view["reserved_outgoing"], 0)
        else:
            self.assertEqual(view["pledged"], 0)
            self.assertEqual(view["reserved_outgoing"], 500)
        self.assertEqual(view["available"], 400)

    def test_concurrent_pledges_only_one_wins(self):
        results, errors = self._race(lambda: self.pledge(600), lambda: self.pledge(600, beneficiary="某银行"))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.db.available(self.source)["pledged"], 600)


class DisposalFlowTest(unittest.TestCase):
    """到期未履约的处置流程：申请冻结、审批生成转让或释放、幂等重试。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.accounts = seed_demo(self.db)
        self.source = self.accounts["北区水库"]
        self.target = self.accounts["河口灌区"]

    def tearDown(self):
        self.tmp.cleanup()

    def expired_pledge(self, amount=300):
        return self.db.create_pledge("alice", {
            "account_id": self.source, "amount": amount,
            "expires_on": "2026-01-01", "beneficiary": "河口合作社"}, "editor")

    def apply(self, pledge_id, actor="河口合作社", target=None):
        return self.db.apply_disposal(actor, {"pledge_id": pledge_id,
                                              "target_account_id": target or self.target})

    def test_disposal_transfer_and_idempotent_retry(self):
        pledge = self.expired_pledge(300)
        with self.assertRaisesRegex(DomainError, "受益人"):
            self.apply(pledge["id"], actor="alice")
        disposal = self.apply(pledge["id"])
        self.assertEqual(disposal["status"], "frozen")
        self.assertEqual(disposal["pledge"]["status"], "frozen")
        with self.assertRaisesRegex(DomainError, "已存在处置单"):
            self.apply(pledge["id"])
        # 冻结期间额度仍被占用，不能取水和转让
        view = self.db.available(self.source)
        self.assertEqual(view["frozen"], 300)
        self.assertEqual(view["available"], 600)
        with self.assertRaisesRegex(DomainError, "超过可用额度"):
            self.db.record_usage("meter-01", {"account_id": self.source, "amount": 650,
                                              "meter_event_id": "M-D1", "occurred_at": "2026-08-01"}, "meter")
        with self.assertRaisesRegex(DomainError, "审核人"):
            self.db.decide_disposal(disposal["id"], "bob", {"decision": "transfer"}, "editor")
        # 审批生成转让：额度划转一次
        view = self.db.decide_disposal(disposal["id"], "bob", {"decision": "transfer"}, "reviewer")
        self.assertEqual(view["status"], "transferred")
        self.assertEqual(view["pledge"]["status"], "disposed")
        transfer = view["transfer"]
        self.assertEqual(transfer["status"], "approved")
        self.assertEqual(transfer["disposal_id"], disposal["id"])
        self.assertEqual(transfer["amount"], 300)
        self.assertEqual(self.db.available(self.source)["quota"], 700)
        self.assertEqual(self.db.available(self.target)["quota"], 800)
        # 同一处置单重试：返回原结果，不重复扣减、不重复生成转让
        again = self.db.decide_disposal(disposal["id"], "bob", {"decision": "transfer"}, "reviewer")
        self.assertEqual(again["transfer"]["id"], transfer["id"])
        self.assertEqual(self.db.available(self.source)["quota"], 700)
        linked = [t for t in self.db.list_transfers() if t.get("disposal_id") == disposal["id"]]
        self.assertEqual(len(linked), 1)
        # 审批结果不同才算冲突
        with self.assertRaisesRegex(DomainError, "其他审批结果"):
            self.db.decide_disposal(disposal["id"], "bob", {"decision": "release"}, "reviewer")

    def test_disposal_release_restores_available(self):
        pledge = self.expired_pledge(300)
        disposal = self.apply(pledge["id"])
        view = self.db.decide_disposal(disposal["id"], "bob", {"decision": "释放"}, "reviewer")
        self.assertEqual(view["status"], "released")
        self.assertEqual(view["pledge"]["status"], "released")
        self.assertNotIn("transfer", view)
        source = self.db.available(self.source)
        self.assertEqual(source["quota"], 1000)
        self.assertEqual(source["available"], 900)
        self.assertEqual(source["pledged"], 0)
        self.assertEqual(source["frozen"], 0)
        self.assertEqual(len(self.db.list_transfers()), 0)

    def test_disposal_guards(self):
        pledge = self.db.create_pledge("alice", {
            "account_id": self.source, "amount": 100,
            "expires_on": "2026-12-31", "beneficiary": "河口合作社"}, "editor")
        with self.assertRaisesRegex(DomainError, "尚未到期"):
            self.apply(pledge["id"])
        expired = self.expired_pledge(100)
        with self.assertRaisesRegex(DomainError, "质押账户本身"):
            self.apply(expired["id"], target=self.source)
        with self.assertRaisesRegex(DomainError, "不存在"):
            self.apply(999)
        with self.assertRaisesRegex(DomainError, "不存在"):
            self.db.decide_disposal(999, "bob", {"decision": "transfer"}, "reviewer")
        with self.assertRaisesRegex(DomainError, "审批结果"):
            self.db.decide_disposal(999, "bob", {"decision": "keep"}, "reviewer")

    def test_frozen_pledge_cannot_be_released_directly(self):
        pledge = self.expired_pledge(100)
        self.apply(pledge["id"])
        with self.assertRaisesRegex(DomainError, "履约中"):
            self.db.release_pledge(pledge["id"], "alice", "editor")


class _FlakyConnection:
    """包装连接：第一次写入 transfers 时模拟写失败，验证回滚后可按同一处置单重试。"""

    def __init__(self, real, owner):
        self._real = real
        self._owner = owner

    def execute(self, sql, parameters=()):
        if self._owner.fail_writes and sql.lstrip().upper().startswith("INSERT INTO TRANSFERS"):
            self._owner.fail_writes = False
            raise sqlite3.OperationalError("simulated write failure")
        return self._real.execute(sql, parameters)

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._real, name)


class FlakyDatabase(Database):
    def __init__(self, path):
        self.fail_writes = False
        super().__init__(path)

    def connect(self):
        conn = super().connect()
        if self.fail_writes:
            return _FlakyConnection(conn, self)
        return conn


class DisposalRetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = FlakyDatabase(Path(self.tmp.name) / "test.db")
        self.accounts = seed_demo(self.db)
        self.source = self.accounts["北区水库"]
        self.target = self.accounts["河口灌区"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_failed_write_rolls_back_and_same_disposal_retries(self):
        pledge = self.db.create_pledge("alice", {
            "account_id": self.source, "amount": 300,
            "expires_on": "2026-01-01", "beneficiary": "河口合作社"}, "editor")
        disposal = self.db.apply_disposal("河口合作社", {
            "pledge_id": pledge["id"], "target_account_id": self.target})
        self.db.fail_writes = True
        with self.assertRaises(sqlite3.OperationalError):
            self.db.decide_disposal(disposal["id"], "bob", {"decision": "transfer"}, "reviewer")
        # 失败整体回滚：处置单仍在冻结态，额度未动，没有生成转让
        current = [d for d in self.db.list_disposals() if d["id"] == disposal["id"]][0]
        self.assertEqual(current["status"], "frozen")
        self.assertEqual(self.db.available(self.source)["quota"], 1000)
        self.assertEqual(len(self.db.list_transfers()), 0)
        # 同一处置单重试成功，且只扣减一次
        done = self.db.decide_disposal(disposal["id"], "bob", {"decision": "transfer"}, "reviewer")
        self.assertEqual(done["status"], "transferred")
        self.assertEqual(self.db.available(self.source)["quota"], 700)
        again = self.db.decide_disposal(disposal["id"], "bob", {"decision": "transfer"}, "reviewer")
        self.assertEqual(again["transfer"]["id"], done["transfer"]["id"])
        self.assertEqual(self.db.available(self.source)["quota"], 700)


# 旧版库结构：没有 pledges/disposals 表，transfers 没有 disposal_id 列
OLD_SCHEMA = """
CREATE TABLE accounts (
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
CREATE TABLE transfers (
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
CREATE TABLE usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    meter_event_id TEXT NOT NULL,
    amount REAL NOT NULL CHECK(amount > 0),
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id, meter_event_id)
);
CREATE TABLE season_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    region TEXT NOT NULL,
    month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
    max_fraction REAL NOT NULL CHECK(max_fraction > 0 AND max_fraction <= 1),
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(region, month)
);
CREATE TABLE impact_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_region TEXT NOT NULL,
    target_region TEXT NOT NULL,
    min_source_fraction REAL NOT NULL CHECK(min_source_fraction >= 0 AND min_source_fraction <= 1),
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(source_region, target_region)
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class LegacyMigrationTest(unittest.TestCase):
    """已有账户和转让没有质押关系时按未质押兼容。"""

    def test_legacy_database_migrates_and_behaves_unpledged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.executescript(OLD_SCHEMA)
            conn.execute("INSERT INTO accounts(name,region,holder,priority,valid_from,valid_to,quota,created_at)"
                         " VALUES('老账户','upstream','老用户',2,'2026-01-01','2026-12-31',800,'2026-01-01T00:00:00+00:00')")
            conn.execute("INSERT INTO accounts(name,region,holder,priority,valid_from,valid_to,quota,created_at)"
                         " VALUES('新账户','downstream','新用户',2,'2026-01-01','2026-12-31',300,'2026-01-01T00:00:00+00:00')")
            conn.execute("INSERT INTO transfers(from_account_id,to_account_id,amount,effective_date,status,created_by,created_at)"
                         " VALUES(1,2,100,'2026-06-01','pending','alice','2026-01-02T00:00:00+00:00')")
            conn.commit()
            conn.close()

            db = Database(path)
            # 旧转让没有质押关系：disposal_id 为 NULL，仍按待审预占处理
            transfers = db.list_transfers()
            self.assertEqual(len(transfers), 1)
            self.assertIsNone(transfers[0]["disposal_id"])
            view = db.available(1)
            self.assertEqual(view["pledged"], 0)
            self.assertEqual(view["frozen"], 0)
            self.assertEqual(view["reserved_outgoing"], 100)
            self.assertEqual(view["available"], 700)
            # 旧转让照常审批
            approved = db.approve_transfer(transfers[0]["id"], "bob", "reviewer")
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(db.available(1)["available"], 700)
            # 迁移后新功能直接可用
            pledge = db.create_pledge("alice", {"account_id": 1, "amount": 200,
                                                "expires_on": "2026-12-31", "beneficiary": "某银行"}, "editor")
            self.assertEqual(pledge["status"], "active")
            self.assertEqual(db.available(1)["available"], 500)
            cols = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(transfers)")}
            self.assertIn("disposal_id", cols)


if __name__ == "__main__":
    unittest.main()
