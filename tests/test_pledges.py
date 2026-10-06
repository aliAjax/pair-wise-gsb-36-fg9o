import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import WaterRightsService
from waterright.domain import DomainError, quota_available


class PledgeFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = WaterRightsService(Path(self.tmp.name) / "pledge.db")
        self.source = self.db.create_account("alice", {
            "name": "北区水库", "region": "upstream", "holder": "北区水务公司",
            "priority": 1, "valid_from": "2026-01-01", "valid_to": "2026-12-31", "quota": 1000,
        }, "editor")["id"]
        self.target = self.db.create_account("alice", {
            "name": "河口灌区", "region": "downstream", "holder": "河口合作社",
            "priority": 2, "valid_from": "2026-01-01", "valid_to": "2026-12-31", "quota": 500,
        }, "editor")["id"]
        self.past = (date.today() - timedelta(days=1)).isoformat()
        self.future = (date.today() + timedelta(days=30)).isoformat()

    def tearDown(self):
        self.tmp.cleanup()

    def _pledge(self, amount=200, maturity=None, beneficiary="河口合作社"):
        return self.db.create_pledge("alice", {
            "account_id": self.source, "amount": amount,
            "maturity_date": maturity or self.future, "beneficiary": beneficiary,
        }, "editor")

    def test_pledge_reduces_available_and_blocks_transfer_usage(self):
        self._pledge(200)
        view = self.db.available(self.source)
        self.assertEqual(view["available"], 800)
        self.assertEqual(view["pledged"], 200)
        # quota pledged -> cannot be reserved by a pending transfer
        with self.assertRaisesRegex(DomainError, "可用额度不足"):
            self.db.create_transfer("alice", {
                "from_account_id": self.source, "to_account_id": self.target,
                "amount": 850, "effective_date": "2026-06-01",
            }, "editor")
        # and cannot be occupied by a withdrawal
        with self.assertRaisesRegex(DomainError, "质押"):
            self.db.record_usage("m1", {
                "account_id": self.source, "amount": 850,
                "meter_event_id": "E-1", "occurred_at": "2026-08-01",
            }, "meter")
        # free remainder is still usable
        self.db.create_transfer("alice", {
            "from_account_id": self.source, "to_account_id": self.target,
            "amount": 800, "effective_date": "2026-06-01",
        }, "editor")
        with self.assertRaisesRegex(DomainError, "可质押额度不足"):
            self._pledge(1)

    def test_pending_transfer_blocks_pledge(self):
        self.db.create_transfer("alice", {
            "from_account_id": self.source, "to_account_id": self.target,
            "amount": 900, "effective_date": "2026-06-01",
        }, "editor")
        with self.assertRaisesRegex(DomainError, "可质押额度不足"):
            self._pledge(200)

    def test_two_concurrent_pledges_only_one_wins(self):
        results: list[object] = []

        def submit(amount):
            try:
                results.append(self._pledge(amount))
            except DomainError as exc:
                results.append(exc)

        t1 = threading.Thread(target=submit, args=(900,))
        t2 = threading.Thread(target=submit, args=(900,))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(isinstance(r, DomainError) for r in results), 1)
        self.assertEqual(self.db.available(self.source)["pledged"], 900)

    def test_concurrent_pledge_and_transfer_only_one_wins(self):
        outcomes: list[object] = []

        def pledge():
            try:
                outcomes.append(self._pledge(900))
            except DomainError as exc:
                outcomes.append(exc)

        def transfer():
            try:
                outcomes.append(self.db.create_transfer("alice", {
                    "from_account_id": self.source, "to_account_id": self.target,
                    "amount": 900, "effective_date": "2026-06-01",
                }, "editor"))
            except DomainError as exc:
                outcomes.append(exc)

        t1 = threading.Thread(target=pledge)
        t2 = threading.Thread(target=transfer)
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sum(isinstance(r, dict) for r in outcomes), 1)
        view = self.db.available(self.source)
        self.assertEqual(view["available"], 100)
        self.assertEqual(view["pledged"] + view["pending_transfer"], 900)

    def test_disposal_freees_then_transfers(self):
        pledge = self._pledge(200, maturity=self.past)
        disposal = self.db.file_disposal("河口合作社", {"pledge_id": pledge["id"]}, "viewer", today=date.today())
        self.assertEqual(disposal["status"], "applied")
        view = self.db.available(self.source)
        self.assertEqual(view["pledged"], 0)
        self.assertEqual(view["frozen"], 200)
        # frozen pledge cannot be filed twice
        with self.assertRaisesRegex(DomainError, "不能重复申请处置"):
            self.db.file_disposal("河口合作社", {"pledge_id": pledge["id"]}, "editor", today=date.today())
        result = self.db.decide_disposal(disposal["id"], "bob",
                                         {"decision": "transfer", "transfer_to_account_id": self.target}, "reviewer")
        self.assertEqual(result["status"], "transferred")
        self.assertEqual(self.db.available(self.source)["quota"], 800)
        self.assertEqual(self.db.available(self.target)["quota"], 700)
        finished = self.db.account_detail(self.source)["pledges"][0]
        self.assertEqual(finished["status"], "transferred")
        self.assertEqual(finished["disposal"]["status"], "transferred")

    def test_disposal_can_release(self):
        pledge = self._pledge(200, maturity=self.past)
        disposal = self.db.file_disposal("河口合作社", {"pledge_id": pledge["id"]}, "editor", today=date.today())
        self.db.decide_disposal(disposal["id"], "bob", {"decision": "release"}, "reviewer")
        view = self.db.available(self.source)
        self.assertEqual(view["pledged"], 0)
        self.assertEqual(view["frozen"], 0)
        self.assertEqual(view["available"], 1000)

    def test_undue_pledge_and_stranger_cannot_file(self):
        pledge = self._pledge(200, maturity=self.future)
        with self.assertRaisesRegex(DomainError, "尚未到期"):
            self.db.file_disposal("河口合作社", {"pledge_id": pledge["id"]}, "editor", today=date.today())
        matured = self._pledge(100, maturity=self.past, beneficiary="河口合作社")
        with self.assertRaisesRegex(DomainError, "受益人"):
            self.db.file_disposal("路人甲", {"pledge_id": matured["id"]}, "viewer", today=date.today())

    def test_write_failure_retry_does_not_double_deduct(self):
        pledge = self._pledge(200, maturity=self.past)
        disposal = self.db.file_disposal("河口合作社", {"pledge_id": pledge["id"]}, "editor", today=date.today())
        real_shift = self.db.store.shift_quota
        calls = {"n": 0}

        def flaky(conn, account_id, delta):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated write failure after transfer row")
            return real_shift(conn, account_id, delta)

        self.db.store.shift_quota = flaky
        with self.assertRaises(RuntimeError):
            self.db.decide_disposal(disposal["id"], "bob",
                                    {"decision": "transfer", "transfer_to_account_id": self.target}, "reviewer")
        self.db.store.shift_quota = real_shift
        # First failed attempt rolled back: still applied/frozen, quota intact.
        self.assertEqual(self.db.available(self.source)["quota"], 1000)
        result = self.db.decide_disposal(disposal["id"], "bob",
                                         {"decision": "transfer", "transfer_to_account_id": self.target}, "reviewer")
        self.assertEqual(result["status"], "transferred")
        # Retry with same disposal order is idempotent.
        again = self.db.decide_disposal(disposal["id"], "carol",
                                        {"decision": "transfer", "transfer_to_account_id": self.target}, "reviewer")
        self.assertEqual(again["status"], "transferred")
        self.assertEqual(self.db.available(self.source)["quota"], 800)
        self.assertEqual(self.db.available(self.target)["quota"], 700)
        transfers = [t for t in self.db.list_transfers() if t.get("disposal_id") == disposal["id"]]
        self.assertEqual(len(transfers), 1)
        # Contradictory decisions on a decided order are rejected.
        with self.assertRaisesRegex(DomainError, "相反"):
            self.db.decide_disposal(disposal["id"], "carol", {"decision": "release"}, "reviewer")

    def test_applicant_cannot_review_own_disposal(self):
        pledge = self._pledge(200, maturity=self.past)
        disposal = self.db.file_disposal("bob-reviewer", {"pledge_id": pledge["id"]}, "editor", today=date.today())
        with self.assertRaisesRegex(DomainError, "不能审批自己"):
            self.db.decide_disposal(disposal["id"], "bob-reviewer", {"decision": "release"}, "reviewer")

    def test_legacy_account_without_pledge_is_treated_as_unpledged(self):
        # No pledge rows at all: existing accounts/transfers stay compatible.
        self.assertEqual(self.db.available(self.source)["pledged"], 0)
        detail = self.db.account_detail(self.source)
        self.assertEqual(detail["pledges"], [])
        self.assertEqual(detail["available"], 1000)
        self.assertEqual(quota_available(1000, 100, 400), 500)


if __name__ == "__main__":
    unittest.main()
