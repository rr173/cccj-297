"""领域状态机回归测试（不启动 HTTP，直接打 service 层）。

覆盖题目硬性规则：
* 失联未知态、停用前置、补打双前置；
* 回执重复/延迟/乱序的幂等与隔离；
* 废页不可复活、每槽位唯一保留、交付清单不回改；
* 无候选页仍需补打、矛盾暂停与追查。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from print_center.server.db import connect, init_db
from print_center.server import service
from print_center.server.service import DomainError


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "srv.db"
        init_db(self.db_path)
        self.conn = connect(self.db_path)
        service.register_agent(self.conn, "ag1")
        service.create_job(self.conn, "J1", "测试工单",
                           page_count=2, copy_count=2)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def first_dispatch(self, copy_no: int = 1, page_no: int = 1):
        return dict(self.conn.execute(
            "SELECT * FROM dispatches WHERE job_id='J1' AND copy_no=? "
            "AND page_no=? AND attempt_no=1", (copy_no, page_no)
        ).fetchone())

    def claim(self):
        return service.claim_dispatch(self.conn, "ag1")

    def claim_specific(self, dispatch_id: str):
        """FIFO 队列下排空前面的 queued 派工，直到领到指定派工。"""
        for _ in range(100):
            d = self.claim()
            self.assertIsNotNone(d, "目标派工未在队列中找到")
            if d["dispatch_id"] == dispatch_id:
                return d
        raise AssertionError(f"未能领到 {dispatch_id}")

    def slot(self, copy_no=1, page_no=1):
        return dict(self.conn.execute(
            "SELECT * FROM page_slots WHERE job_id='J1' AND copy_no=? "
            "AND page_no=?", (copy_no, page_no)).fetchone())

    def assert_error(self, code, fn, *args, **kw):
        with self.assertRaises(DomainError) as ctx:
            fn(*args, **kw)
        self.assertEqual(ctx.exception.code, code)


class TestUnknownStateAndGuards(ServiceTestBase):
    def test_claimed_is_unknown_no_reprint_no_nopaper(self):
        d = self.claim()
        # 无回执：既非失败也非成功，且禁止未停用就下结论/补打
        self.assertEqual(d["status"], "claimed")
        self.assert_error(
            "deactivation_required",
            service.onsite_no_paper, self.conn, "J1", 1, 1)
        self.assert_error(
            "reprint_not_allowed",
            service.reprint, self.conn, "J1", 1, 1)
        # 队列里该派工不会因超时重新可领（仍只有 queued 的其他派工）
        again = self.claim()
        self.assertNotEqual(again["dispatch_id"], d["dispatch_id"])

    def test_deactivation_does_not_prove_paper(self):
        d = self.claim()
        out = service.deactivate_dispatch(
            self.conn, d["dispatch_id"], "ag1",
            executed_local=False)
        self.assertTrue(out["idempotent"] is False)
        # 停用后没有任何纸张进入系统
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM papers").fetchone()[0], 0)
        # 重复停用是幂等的
        again = service.deactivate_dispatch(
            self.conn, d["dispatch_id"], "ag1", False)
        self.assertTrue(again["idempotent"])

    def test_only_original_agent_can_deactivate(self):
        d = self.claim()
        service.register_agent(self.conn, "ag2")
        self.assert_error(
            "agent_mismatch",
            service.deactivate_dispatch,
            self.conn, d["dispatch_id"], "ag2", False)

    def test_reprint_requires_both_preconditions(self):
        d = self.claim()
        # 只停用、没现场结论 -> 不能补打
        service.deactivate_dispatch(self.conn, d["dispatch_id"],
                                    "ag1", False)
        self.assert_error("reprint_not_allowed",
                          service.reprint, self.conn, "J1", 1, 1)
        # 现场未出纸后可以补打，attempt 递增
        service.onsite_no_paper(self.conn, "J1", 1, 1)
        rp = service.reprint(self.conn, "J1", 1, 1)
        self.assertEqual(rp["attempt_no"], 2)
        self.assertEqual(rp["status"], "queued")


class TestReceiptSemantics(ServiceTestBase):
    def test_duplicate_receipt_is_idempotent(self):
        d = self.claim()
        r1 = service.receive_receipt(
            self.conn, d["dispatch_id"], "ag1", "PAP-1", "RCP-1")
        r2 = service.receive_receipt(
            self.conn, d["dispatch_id"], "ag1", "PAP-1", "RCP-1")
        self.assertFalse(r1["duplicate"])
        self.assertTrue(r2["duplicate"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM receipts WHERE receipt_id='RCP-1'"
            ).fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM papers").fetchone()[0], 1)

    def test_same_dispatch_same_paper_dedup(self):
        d = self.claim()
        service.receive_receipt(self.conn, d["dispatch_id"], "ag1",
                                "PAP-1", "RCP-1")
        # 换 receipt_id 但同派工同纸张：仍然只算一次
        out = service.receive_receipt(
            self.conn, d["dispatch_id"], "ag1", "PAP-1", "RCP-2")
        self.assertTrue(out["duplicate"])

    def test_paper_id_globally_unique(self):
        d1 = self.claim()
        service.receive_receipt(self.conn, d1["dispatch_id"], "ag1",
                                "PAP-X", "RCP-a")
        d2 = self.claim()
        self.assert_error(
            "paper_id_conflict",
            service.receive_receipt, self.conn, d2["dispatch_id"],
            "ag1", "PAP-X", "RCP-b")

    def test_late_receipt_after_delivery_keeps_list(self):
        # 原派工出纸、判废、补打、交付
        d1 = self.claim()
        service.receive_receipt(self.conn, d1["dispatch_id"], "ag1",
                                "PAP-old", "RCP-old")
        service.onsite_waste(self.conn, "J1", 1, 1, "PAP-old")
        rp = service.reprint(self.conn, "J1", 1, 1)
        d2 = self.claim_specific(rp["dispatch_id"])
        service.receive_receipt(self.conn, d2["dispatch_id"], "ag1",
                                "PAP-new", "RCP-new")
        service.onsite_retrieve(self.conn, "J1", 1, 1, "PAP-new")
        # 把第二页也交付条件凑齐
        self._deliver_remaining_pages()
        dl = service.confirm_delivery(self.conn, "J1", 1)
        self.assertEqual(dl["items"][0]["paper_id"], "PAP-new")

        # 交付后旧派工晚回执：不回改清单，只追查
        late = service.receive_receipt(
            self.conn, d1["dispatch_id"], "ag1",
            "PAP-ghost", "RCP-ghost")
        self.assertEqual(late["outcome"],
                         "investigated_after_delivery")
        items = service.delivery_list(self.conn, "J1", 1)["items"]
        self.assertEqual(items[0]["paper_id"], "PAP-new")
        inv = service.list_investigations(self.conn, "J1")
        self.assertTrue(any(
            i["reason"] == "late_receipt_after_delivery" for i in inv))
        copy = dict(self.conn.execute(
            "SELECT * FROM job_copies WHERE job_id='J1' AND copy_no=1"
        ).fetchone())
        self.assertEqual(copy["status"], "investigating")
        # 废页依旧是废页，未被复活
        state = dict(self.conn.execute(
            "SELECT state FROM papers WHERE paper_id='PAP-old'"
        ).fetchone())
        self.assertEqual(state["state"], "waste")

    def test_waste_never_revived_by_late_receipt_before_delivery(self):
        d1 = self.claim()
        service.receive_receipt(self.conn, d1["dispatch_id"], "ag1",
                                "PAP-w", "RCP-w")
        service.onsite_waste(self.conn, "J1", 1, 1, "PAP-w")
        # 另一张声称来自同派工的晚回执（不同纸）= 疑似重复出纸
        late = service.receive_receipt(
            self.conn, d1["dispatch_id"], "ag1", "PAP-w2", "RCP-w2")
        self.assertIn(late["outcome"],
                      ("conflict_duplicate_paper",
                       "conflict_stale_against_onsite",
                       "stale_receipt_quarantined"))
        states = {r["paper_id"]: r["state"] for r in
                  self.conn.execute(
                      "SELECT paper_id,state FROM papers").fetchall()}
        self.assertEqual(states["PAP-w"], "waste")
        self.assertEqual(states["PAP-w2"], "isolated")
        # 废页结论没有被任何回执覆盖
        self.assertEqual(self.slot()["status"], "conflict")

    def test_out_of_order_receipts_reprint_first_then_stale(self):
        # 崩溃恢复时序：停用 -> 未出纸 -> 补派 -> 补派回执先到 ->
        # 旧派工回执后到。槽位必须保持可用，旧纸隔离。
        d1 = self.claim()
        service.deactivate_dispatch(self.conn, d1["dispatch_id"],
                                    "ag1", False)
        service.onsite_no_paper(self.conn, "J1", 1, 1)
        rp = service.reprint(self.conn, "J1", 1, 1)
        d2 = self.claim_specific(rp["dispatch_id"])
        service.receive_receipt(self.conn, d2["dispatch_id"], "ag1",
                                "PAP-new", "RCP-new")
        # 旧 attempt 回执晚到
        late = service.receive_receipt(
            self.conn, d1["dispatch_id"], "ag1", "PAP-old", "RCP-old")
        self.assertEqual(late["outcome"], "stale_receipt_quarantined")
        self.assertEqual(self.slot()["status"], "candidate")
        states = {r["paper_id"]: r["state"] for r in
                  self.conn.execute(
                      "SELECT paper_id,state FROM papers").fetchall()}
        self.assertEqual(states["PAP-new"], "candidate")
        self.assertEqual(states["PAP-old"], "isolated")

    def _deliver_remaining_pages(self):
        # 本份第 2 页可能已被领取（claimed）也可能仍在队列，直接回执即可
        target = dict(self.conn.execute(
            "SELECT * FROM dispatches WHERE job_id='J1' "
            "AND copy_no=1 AND page_no=2 AND attempt_no=1").fetchone())
        service.receive_receipt(
            self.conn, target["dispatch_id"], "ag1",
            "PAP-1-2-1", "RCP-121")
        # 逐页现场确认未验证页（取该页最新的非废页纸）
        for page_no in (1, 2):
            if self.slot(1, page_no)["status"] == "verified":
                continue
            paper = self.conn.execute(
                "SELECT paper_id FROM papers WHERE job_id='J1' AND copy_no=1 "
                "AND page_no=? AND state<>'waste' "
                "ORDER BY rowid DESC LIMIT 1", (page_no,)).fetchone()
            self.assertIsNotNone(paper, f"第 {page_no} 页缺少候选纸")
            service.onsite_retrieve(
                self.conn, "J1", 1, page_no, paper["paper_id"])


class TestSlotAndDeliveryRules(ServiceTestBase):
    def test_only_one_verified_paper_per_slot(self):
        d = self.claim()
        service.receive_receipt(self.conn, d["dispatch_id"], "ag1",
                                "PAP-a", "RCP-a")
        # 现场实物首次出现的另一张同槽位纸（onsite 来源）
        service.onsite_retrieve(self.conn, "J1", 1, 1, "PAP-a")
        slot = self.slot()
        self.assertEqual(slot["status"], "verified")
        self.assertEqual(slot["retained_paper_id"], "PAP-a")
        # 已保留后不允许直接换纸
        self.assert_error(
            "slot_has_retained_paper",
            service.onsite_retrieve, self.conn, "J1", 1, 1, "PAP-b")

    def test_delivery_requires_every_page_verified(self):
        # 一份两页，只验证一页
        d = self.claim()
        service.receive_receipt(self.conn, d["dispatch_id"], "ag1",
                                "PAP-1", "RCP-1")
        service.onsite_retrieve(self.conn, "J1", 1, 1, "PAP-1")
        self.assert_error(
            "not_deliverable",
            service.confirm_delivery, self.conn, "J1", 1)

    def test_conflict_without_candidate_still_needs_reprint(self):
        d = self.claim()
        service.deactivate_dispatch(
            self.conn, d["dispatch_id"], "ag1",
            executed_local=True, evidence_paper_id="PAP-e")
        # 现场未出纸与见证冲突
        service.onsite_no_paper(self.conn, "J1", 1, 1)
        self.assertEqual(self.slot()["status"], "conflict")
        # 现场不保留任何候选
        out = service.resolve_conflict(
            self.conn, "J1", 1, 1, keep_paper_id=None)
        self.assertEqual(out["outcome"],
                         "no_candidate_needs_reprint")
        self.assertEqual(self.slot()["status"], "no_paper")
        rp = service.reprint(self.conn, "J1", 1, 1)
        self.assertEqual(rp["attempt_no"], 2)

    def test_conflict_keep_unique_and_isolate_rest(self):
        d1 = self.claim()
        service.deactivate_dispatch(
            self.conn, d1["dispatch_id"], "ag1",
            executed_local=True, evidence_paper_id="PAP-e")
        service.onsite_no_paper(self.conn, "J1", 1, 1)  # conflict
        # conflict 未解决前不允许补打
        self.assert_error(
            "reprint_not_allowed",
            service.reprint, self.conn, "J1", 1, 1)
        out = service.resolve_conflict(
            self.conn, "J1", 1, 1, keep_paper_id="PAP-e",
            note="现场找回见证纸实物")
        self.assertEqual(out["outcome"], "kept_unique_paper")
        self.assertEqual(self.slot()["retained_paper_id"], "PAP-e")

    def test_attest_paper_must_pass_onsite_check(self):
        d = self.claim()
        service.deactivate_dispatch(
            self.conn, d["dispatch_id"], "ag1",
            executed_local=True, evidence_paper_id="PAP-e")
        paper = dict(self.conn.execute(
            "SELECT * FROM papers WHERE paper_id='PAP-e'").fetchone())
        # 见证纸初始不是 verified，不能直接交付
        self.assertEqual(paper["state"], "attest")
        self.assert_error(
            "not_deliverable",
            service.confirm_delivery, self.conn, "J1", 1)
        # 现场找回后才 verified
        service.onsite_retrieve(self.conn, "J1", 1, 1, "PAP-e")
        self.assertEqual(self.slot()["status"], "verified")


if __name__ == "__main__":
    unittest.main(verbosity=2)
