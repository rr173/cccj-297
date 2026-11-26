"""批次召回与柜台换货的服务层回归测试。"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from print_center.server.db import connect, init_db
from print_center.server import service
from print_center.server.service import DomainError


class RecallTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "recall.db"
        init_db(self.db_path)
        self.conn = connect(self.db_path)
        service.register_agent(self.conn, "ag1")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def assert_error(self, code, fn, *args, **kw):
        with self.assertRaises(DomainError) as ctx:
            fn(*args, **kw)
        self.assertEqual(ctx.exception.code, code,
                         f"expected {code}, got {ctx.exception.code}: "
                         f"{ctx.exception.message}")
        return ctx.exception

    def make_delivered_tracked_copy(
            self, job="J1", pages=2, copy_no=1, batch="B1",
            paper_prefix="ORIG", arrivals=10):
        service.register_arrival(self.conn, batch, arrivals)
        service.create_job(self.conn, job, "召回测试", pages, copy_no,
                           [batch])
        return self.print_and_deliver(job, copy_no, pages, paper_prefix)

    def claim_target(self, job, copy_no, page_no):
        for _ in range(100):
            d = service.claim_dispatch(self.conn, "ag1")
            self.assertIsNotNone(d)
            if (d["job_id"], d["copy_no"], d["page_no"]) == \
                    (job, copy_no, page_no):
                return d
        raise AssertionError("target dispatch not found")

    def print_and_deliver(self, job, copy_no, pages, prefix,
                          deliver=True):
        paper_ids = []
        for page in range(1, pages + 1):
            d = self.claim_target(job, copy_no, page)
            pid = f"P-{prefix}-C{copy_no}-P{page}"
            service.receive_receipt(
                self.conn, d["dispatch_id"], "ag1", pid,
                f"R-{prefix}-C{copy_no}-P{page}")
            service.onsite_retrieve(self.conn, job, copy_no, page, pid)
            paper_ids.append(pid)
        if deliver:
            service.confirm_delivery(self.conn, job, copy_no)
        return paper_ids

    def make_ready_spare(self, job, spare_no, pages, batch="B2",
                         prefix="SP1", arrivals=10):
        service.register_arrival(self.conn, batch, arrivals)
        service.create_spare_copies(
            self.conn, job, 1, [batch])
        return self.print_and_deliver(
            job, spare_no, pages, prefix, deliver=False)


class TestRecallExchangeDemoFlow(RecallTestBase):
    def test_full_two_generation_recall_exchange_and_restart(self):
        pages = 2
        old_papers = self.make_delivered_tracked_copy(pages=pages)

        # 未召回前没有待收回。
        self.assertEqual(service.list_recall_pending(self.conn), [])

        service.recall_batch(self.conn, "B1", "纸边缺陷")
        pending = service.list_recall_pending(self.conn, "B1")
        self.assertEqual(len(pending), 1)
        p = pending[0]
        self.assertEqual((p["job_id"], p["original_copy_no"]), ("J1", 1))
        self.assertEqual(p["current_copy_no"], 1)
        self.assertEqual(p["paper_ids"], old_papers)
        self.assertIn("B1", p["pending_reason"])

        # 从未交付的额外份不列入待收回。
        service.create_job(self.conn, "J2", "未交出", 1, 1, ["B1"])
        self.assertEqual(len(service.list_recall_pending(self.conn, "B1")), 1)

        # 普通入口不得再交付含 B1 的 ready 份；J2 尚未 ready，先构造 J3。
        service.register_arrival(self.conn, "B1", 5)
        service.create_job(self.conn, "J3", "含召回纸", 1, 1, ["B1"])
        d = self.claim_target("J3", 1, 1)
        service.receive_receipt(self.conn, d["dispatch_id"], "ag1",
                                "P-BAD", "R-BAD")
        service.onsite_retrieve(self.conn, "J3", 1, 1, "P-BAD")
        self.assert_error("batch_recalled",
                          service.confirm_delivery, self.conn, "J3", 1)

        # 备品用 B2，照常派工/核对并满足整份条件。
        service.register_arrival(self.conn, "B2", pages)
        spares = service.create_spare_copies(
            self.conn, "J1", copy_count=1, allowed_batches=["B2"])
        spare_no = spares["copy_nos"][0] if "copy_nos" in spares \
            else spares["spares"][0]["copy_no"]
        self.assertEqual(spare_no, 2)
        spare_papers = self.print_and_deliver(
            "J1", spare_no, pages, "SP1", deliver=False)

        result = service.exchange_copy(
            self.conn, "J1", 1, old_papers, spare_no, "EX-1")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["returned_copy_no"], 1)
        self.assertEqual(result["spare_copy_no"], spare_no)
        self.assertEqual([x["paper_id"] for x in result["items"]],
                         spare_papers)

        # 旧批次名单不再有该客户。
        self.assertEqual(service.list_recall_pending(self.conn, "B1"), [])
        holding = service.list_holdings(self.conn, "J1")["holdings"]
        current = [h for h in holding if h["status"] == "with_customer"]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["physical_copy_no"], spare_no)

        # 原交付清单保持原样。
        original_list = service.delivery_list(self.conn, "J1", 1)["items"]
        self.assertEqual([x["paper_id"] for x in original_list], old_papers)
        spare_list = service.delivery_list(
            self.conn, "J1", spare_no)["items"]
        self.assertEqual([x["paper_id"] for x in spare_list], spare_papers)

        # 同号重送返回当次结果，即使后面又发生第二次换货。
        replay = service.exchange_copy(
            self.conn, "EX-1", job_id="J1", original_copy_no=1,
            recovered_paper_ids=old_papers, spare_copy_no=spare_no)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["delivered_at"], result["delivered_at"])
        self.assertEqual(replay["items"], result["items"])

        # 同号改换内容拒绝。
        self.assert_error(
            "request_id_conflict", service.exchange_copy,
            self.conn, "EX-1", job_id="J1", original_copy_no=1,
            recovered_paper_ids=old_papers + ["P-EXTRA"],
            spare_copy_no=spare_no)

        # B2 日后召回：同一原交付份再次待收回，应收上次换出的 B2 纸。
        service.recall_batch(self.conn, "B2")
        pending2 = service.list_recall_pending(self.conn, "B2")
        self.assertEqual(len(pending2), 1)
        self.assertEqual(pending2[0]["original_copy_no"], 1)
        self.assertEqual(pending2[0]["current_copy_no"], spare_no)
        self.assertEqual(pending2[0]["paper_ids"], spare_papers)
        self.assertEqual(service.list_recall_pending(self.conn, "B1"), [])

        service.register_arrival(self.conn, "B3", pages)
        service.create_spare_copies(self.conn, "J1", 1, ["B3"])
        spare2_no = spare_no + 1
        spare2_papers = self.print_and_deliver(
            "J1", spare2_no, pages, "SP2", deliver=False)
        result2 = service.exchange_copy(
            self.conn, job_id="J1", original_copy_no=1,
            recovered_paper_ids=spare_papers,
            spare_copy_no=spare2_no, request_id="EX-2")
        self.assertEqual(result2["returned_copy_no"], spare_no)
        self.assertEqual(result2["spare_copy_no"], spare2_no)
        self.assertEqual(service.list_recall_pending(self.conn), [])

        view = service.get_copy_recall_view(self.conn, "J1", 1)
        self.assertEqual(view["current_holding"]["physical_copy_no"],
                         spare2_no)
        self.assertEqual(len(view["exchanges"]), 2)
        self.assertFalse(view["pending_recall"])

        # 重启/重连后事实仍可查询，且 EX-1 仍是第一次结果快照。
        self.conn.close()
        init_db(self.db_path)
        self.conn = connect(self.db_path)
        restarted = service.get_exchange(self.conn, "EX-1")
        self.assertEqual(restarted["spare_copy_no"], spare_no)
        self.assertEqual(restarted["items"], result["items"])
        self.assertEqual(len(service.list_copy_exchanges(self.conn, "J1")), 2)
        self.assertEqual(len(service.list_recall_pending(self.conn)), 0)
        view2 = service.get_copy_recall_view(self.conn, "J1", 1)
        self.assertEqual(view2["current_holding"]["physical_copy_no"],
                         spare2_no)
        self.assertEqual([x["paper_id"]
                          for x in view2["original_delivery_list"]],
                         old_papers)
        self.assertEqual([x["paper_id"]
                          for x in view2["current_delivery_list"]],
                         spare2_papers)


class TestExchangeGuards(RecallTestBase):
    def test_must_match_full_delivery_and_spare_must_be_ready_good_batch(self):
        old = self.make_delivered_tracked_copy(pages=2)
        service.recall_batch(self.conn, "B1")
        service.register_arrival(self.conn, "B2", 2)
        service.create_spare_copies(self.conn, "J1", 1, ["B2"])
        # 备品尚未印完/verified。
        self.assert_error(
            "not_deliverable", service.exchange_copy,
            self.conn, job_id="J1", original_copy_no=1,
            recovered_paper_ids=old, spare_copy_no=2,
            request_id="EX-BAD")
        # 失败后没有任何换货记录或持有变化。
        self.assertEqual(service.list_copy_exchanges(self.conn), [])
        self.assertEqual(
            service.list_holdings(self.conn)["holdings"][0]["status"],
            "with_customer")

        spare_papers = self.print_and_deliver(
            "J1", 2, 2, "SP", deliver=False)
        # 少交一张/纸号不符均拒绝。
        self.assert_error(
            "recovered_paper_mismatch", service.exchange_copy,
            self.conn, job_id="J1", original_copy_no=1,
            recovered_paper_ids=old[:1], spare_copy_no=2,
            request_id="EX-BAD2")
        self.assert_error(
            "recovered_paper_mismatch", service.exchange_copy,
            self.conn, job_id="J1", original_copy_no=1,
            recovered_paper_ids=["NOPE", old[1]], spare_copy_no=2,
            request_id="EX-BAD3")
        ok = service.exchange_copy(
            self.conn, job_id="J1", original_copy_no=1,
            recovered_paper_ids=old, spare_copy_no=2,
            request_id="EX-OK")
        self.assertEqual([x["paper_id"] for x in ok["items"]], spare_papers)

    def test_cannot_exchange_nonrecalled_or_use_spare_twice(self):
        service.register_arrival(self.conn, "B1", 10)
        service.create_job(self.conn, "J1", "两客户份", 1, 2, ["B1"])
        old1 = self.print_and_deliver("J1", 1, 1, "O1")
        old2 = self.print_and_deliver("J1", 2, 1, "O2")
        service.register_arrival(self.conn, "B2", 1)
        service.create_spare_copies(self.conn, "J1", 1, ["B2"])
        spare = self.print_and_deliver("J1", 3, 1, "SP", deliver=False)

        self.assert_error(
            "copy_not_pending_recall", service.exchange_copy,
            self.conn, "J1", 1, old1, 3, "EX-NO-RECALL")
        service.recall_batch(self.conn, "B1")
        service.exchange_copy(self.conn, "J1", 1, old1, 3, "EX1")

        # 同一备品不能再交给同一原工单的另一个客户份。
        self.assert_error(
            "spare_already_used", service.exchange_copy,
            self.conn, "J1", 2, old2, 3, "EX2")

        # 构造第二备品后，旧物理份1已非在手，不能再次成功换货。
        service.register_arrival(self.conn, "B3", 1)
        service.create_spare_copies(self.conn, "J1", 1, ["B3"])
        self.print_and_deliver("J1", 4, 1, "SP2", deliver=False)
        self.assert_error(
            "returned_copy_mismatch", service.exchange_copy,
            self.conn, job_id="J1", original_copy_no=1,
            returned_copy_no=1, recovered_paper_ids=old1,
            spare_copy_no=4, request_id="EX-DOUBLE-OLD")
        # 当前在手是备品3；B2 尚未召回，不能用 B3 换。
        self.assert_error(
            "copy_not_pending_recall", service.exchange_copy,
            self.conn, job_id="J1", original_copy_no=1,
            recovered_paper_ids=spare, spare_copy_no=4,
            request_id="EX-NOT-B2")

    def test_spare_cannot_use_recalled_batch_and_credit_is_blocked(self):
        self.make_delivered_tracked_copy(pages=1)
        service.recall_batch(self.conn, "B1")
        service.register_arrival(self.conn, "B2", 1)
        service.recall_batch(self.conn, "B2")
        self.assert_error(
            "batch_recalled", service.create_spare_copies,
            self.conn, "J1", 1, ["B2"])

    def test_untracked_job_unchanged(self):
        service.create_job(self.conn, "JL", "旧", 1, 1)
        d = service.claim_dispatch(self.conn, "ag1")
        service.receive_receipt(self.conn, d["dispatch_id"], "ag1",
                                "PL", "RL")
        service.onsite_retrieve(self.conn, "JL", 1, 1, "PL")
        service.confirm_delivery(self.conn, "JL", 1)
        # 即使存在某个召回批，旧工单没有领用批次，不列入、不阻挡。
        service.register_arrival(self.conn, "BX", 1)
        service.recall_batch(self.conn, "BX")
        self.assertEqual(service.list_recall_pending(self.conn), [])


class TestRecallConcurrency(RecallTestBase):
    def test_concurrent_exchanges_same_copy_and_spare(self):
        pages = 1
        old = self.make_delivered_tracked_copy(pages=pages)
        service.recall_batch(self.conn, "B1")
        service.register_arrival(self.conn, "B2", 2)
        service.create_spare_copies(self.conn, "J1", 2, ["B2"])
        papers = {}
        for no, prefix in ((2, "SPA"), (3, "SPB")):
            papers[no] = self.print_and_deliver(
                "J1", no, pages, prefix, deliver=False)

        outcomes = []
        lock = threading.Lock()

        def exchange(req, spare):
            try:
                r = service.exchange_copy(
                    self.conn, job_id="J1", original_copy_no=1,
                    recovered_paper_ids=old, spare_copy_no=spare,
                    request_id=req)
                with lock:
                    outcomes.append(("ok", req, spare))
            except DomainError as exc:
                with lock:
                    outcomes.append((exc.code, req, spare))

        t1 = threading.Thread(target=exchange, args=("EA", 2))
        t2 = threading.Thread(target=exchange, args=("EB", 3))
        t1.start(); t2.start(); t1.join(); t2.join()
        oks = [x for x in outcomes if x[0] == "ok"]
        self.assertEqual(len(oks), 1, outcomes)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM copy_exchanges").fetchone()[0], 1)
        active = self.conn.execute(
            "SELECT physical_copy_no FROM copy_custody "
            "WHERE logical_job_id='J1' AND logical_copy_no=1 "
            "AND status='with_customer'").fetchall()
        self.assertEqual(len(active), 1)


class TestCreditRecallRelease(RecallTestBase):
    def test_held_pickup_releases_after_recall(self):
        # 直接构造授信工单和 ready 份；hold mock 成功，随后召回，held 阶段
        # 应按既有规则 release。
        service.register_arrival(self.conn, "B1", 10)
        service.create_job(
            self.conn, "JC", "授信召回", 1, 1, ["B1"],
            credit_account_id="E1", price_per_copy=100)
        d = service.claim_dispatch(self.conn, "ag1")
        service.receive_receipt(self.conn, d["dispatch_id"], "ag1",
                                "PC", "RC")
        service.onsite_retrieve(self.conn, "JC", 1, 1, "PC")

        released = {}

        def fake_hold(request_id, account_id, amount, reference=None):
            return {"status": "held", "request_id": request_id}

        def fake_release(request_id, reason=None):
            released[request_id] = reason
            return {"status": "voided", "request_id": request_id}

        with mock.patch.object(service.finance_client, "hold",
                               side_effect=fake_hold), \
             mock.patch.object(service.finance_client, "release",
                               side_effect=fake_release), \
             mock.patch.object(service.finance_client, "settle") as settle:
            # 在 hold HTTP 返回后、held 交付检查前插入召回。用包装 hold 模拟
            # 并发召回提交（全局写锁保证召回与交付检查串行）。
            def hold_then_recall(*a, **k):
                service.recall_batch(self.conn, "B1")
                return fake_hold(*a, **k)

            with mock.patch.object(service.finance_client, "hold",
                                   side_effect=hold_then_recall):
                view = service.request_credit_pickup(
                    self.conn, "JC", 1, "REQ-RC")
            self.assertEqual(view["status"], "released")
            self.assertIn("batch_recalled", view["last_error"])
            self.assertIn("REQ-RC", released)
            settle.assert_not_called()

        # 未交付，故没有 custody；召回待收回名单也不包含从未交出的份。
        self.assertEqual(service.list_recall_pending(self.conn), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
