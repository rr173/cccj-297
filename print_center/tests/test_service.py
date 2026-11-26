"""领域状态机回归测试（不启动 HTTP，直接打 service 层）。

覆盖题目硬性规则：
* 失联未知态、停用前置、补打双前置；
* 回执重复/延迟/乱序的幂等与隔离；
* 废页不可复活、每槽位唯一保留、交付清单不回改；
* 无候选页仍需补打、矛盾暂停与追查；
* 纸料承诺：同总量不同接单结果、锁定/换批/撤销、领用原子性、
  补打同批且不吃承诺原印、缺料等待不挡别人、库存不恢复、
  并发不超承、重启后承诺与领用仍有效。
"""
from __future__ import annotations

import threading
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


class PaperCommitmentBase(unittest.TestCase):
    """纸料承诺测试基类：B1/B2 各到货 3 张。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "srv.db"
        init_db(self.db_path)
        self.conn = connect(self.db_path)
        service.register_agent(self.conn, "ag1")
        service.register_arrival(self.conn, "B1", 3)
        service.register_arrival(self.conn, "B2", 3)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def assert_error(self, code, fn, *args, **kw):
        with self.assertRaises(DomainError) as ctx:
            fn(*args, **kw)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def batch(self, batch_id):
        return service.batch_view(self.conn, batch_id)

    def commitment(self, job_id, copy_no):
        cm = service.job_commitments(self.conn, job_id)
        return next(c for c in cm["copies"]
                    if c["copy_no"] == copy_no)["commitment"]

    def dispatch_of(self, job_id, copy_no, page_no, attempt_no=1):
        return dict(self.conn.execute(
            "SELECT * FROM dispatches WHERE job_id=? AND copy_no=? "
            "AND page_no=? AND attempt_no=?",
            (job_id, copy_no, page_no, attempt_no)).fetchone())


class TestJobAcceptance(PaperCommitmentBase):
    def test_same_total_stock_different_acceptance(self):
        # 总库存 6（B1+B2 各 3）。3 份×2 页（总需求也是 6）：
        # 每份 2 张不可拆分，两批各装一份后各剩 1 张 -> 整单不建
        exc = self.assert_error(
            "insufficient_stock", service.create_job,
            self.conn, "J-REJ", "三折页", 2, 3, ["B1", "B2"])
        self.assertIn("整单未建", exc.message)
        self.assertIn("批次 B1", exc.message)
        # 拒单不建单、不记账
        self.assert_error("job_not_found", service.get_job, self.conn,
                          "J-REJ")
        self.assertEqual(self.batch("B1")["committed_original"], 0)
        # 同总量（2 份×3 页 = 6）则可接：不同份用不同批
        service.create_job(self.conn, "J-OK", "手册", 3, 2, ["B1", "B2"])
        used = {self.commitment("J-OK", 1)["batch_id"],
                self.commitment("J-OK", 2)["batch_id"]}
        self.assertEqual(used, {"B1", "B2"})
        self.assertEqual(self.batch("B1")["committed_original"], 3)
        self.assertEqual(self.batch("B2")["committed_original"], 3)

    def test_unarrived_batch_is_zero_stock(self):
        exc = self.assert_error(
            "insufficient_stock", service.create_job,
            self.conn, "J-Z", "单页", 1, 1, ["B9"])
        self.assertIn("B9", exc.message)
        # 同批补入到货后即可接单
        service.register_arrival(self.conn, "B9", 1)
        service.create_job(self.conn, "J-Z", "单页", 1, 1, ["B9"])
        self.assertEqual(self.commitment("J-Z", 1)["batch_id"], "B9")

    def test_restock_same_batch_accumulates(self):
        service.register_arrival(self.conn, "B1", 2, note="同批再入库")
        view = self.batch("B1")
        self.assertEqual((view["arrived"], view["available"]), (5, 5))

    def test_acceptance_replans_unstarted_copies(self):
        service.register_arrival(self.conn, "B1", 1)   # B1=4
        service.register_arrival(self.conn, "B2", 3)   # B2=6
        service.create_job(self.conn, "J1", "t", 3, 1, ["B1", "B2"])
        self.assertEqual(self.commitment("J1", 1)["batch_id"], "B1")
        # J2 只能用 B1 且要 4 张：只有把 J1 的未开印份重排到 B2 才可行，
        # 不能因此前对未开印份的分配而拒单
        service.create_job(self.conn, "J2", "t", 4, 1, ["B1"])
        self.assertEqual(self.commitment("J1", 1)["batch_id"], "B2")
        self.assertEqual(self.commitment("J2", 1)["batch_id"], "B1")

    def test_legacy_job_not_tracked(self):
        service.create_job(self.conn, "JL", "旧工单", 1, 1)
        d = service.claim_dispatch(self.conn, "ag1")
        self.assertNotIn("batch_id", d)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM withdrawals").fetchone()[0], 0)
        cm = service.job_commitments(self.conn, "JL")
        self.assertIsNone(cm["allowed_batches"])
        self.assertIsNone(cm["copies"][0]["commitment"])


class TestCopyCommitmentLifecycle(PaperCommitmentBase):
    def test_lock_on_first_claim_and_trace(self):
        service.create_job(self.conn, "J1", "t", 2, 1, ["B1", "B2"])
        self.assertEqual(self.commitment("J1", 1)["status"], "planned")
        before = self.batch("B1")["available"]
        d = service.claim_dispatch(self.conn, "ag1")
        # 首次领到该份页任务 -> 锁定批次，且领用与派工一起生效
        self.assertEqual(d["batch_id"], "B1")
        self.assertEqual(d["status"], "claimed")
        cm = self.commitment("J1", 1)
        self.assertEqual((cm["status"], cm["batch_id"]), ("locked", "B1"))
        w = service.dispatch_withdrawal(self.conn, d["dispatch_id"])
        self.assertEqual(w["withdrawal"]["batch_id"], "B1")
        # 发出的纸不再算库内可用
        self.assertEqual(self.batch("B1")["available"], before - 1)
        self.assertEqual(self.batch("B1")["withdrawn"], 1)
        # 该份剩余原印仍同批
        d2 = service.claim_dispatch(self.conn, "ag1")
        self.assertEqual(d2["batch_id"], "B1")

    def test_reassign_guard_and_success(self):
        service.create_job(self.conn, "J1", "t", 3, 2, ["B1", "B2"])
        # B2 已被 copy2 占满，换入必拒
        self.assert_error("insufficient_stock",
                          service.reassign_copy_batch,
                          self.conn, "J1", 1, "B2")
        # 同批补货后可换
        service.register_arrival(self.conn, "B2", 3)
        service.reassign_copy_batch(self.conn, "J1", 1, "B2")
        self.assertEqual(self.commitment("J1", 1)["batch_id"], "B2")
        # 不在允许范围内的批次
        self.assert_error("batch_not_allowed",
                          service.reassign_copy_batch,
                          self.conn, "J1", 2, "B9")

    def test_cancel_copy(self):
        service.create_job(self.conn, "J1", "t", 3, 2, ["B1", "B2"])
        service.cancel_copy(self.conn, "J1", 2)
        cm = self.commitment("J1", 2)
        self.assertEqual(cm["status"], "cancelled")
        # 撤销后不再派工：其派工全部 cancelled，领活只拿到 copy1 的
        rows = self.conn.execute(
            "SELECT status FROM dispatches WHERE job_id='J1' AND copy_no=2"
        ).fetchall()
        self.assertTrue(rows and all(r["status"] == "cancelled"
                                     for r in rows))
        d = service.claim_dispatch(self.conn, "ag1")
        self.assertEqual(d["copy_no"], 1)
        # 撤销后不再交付
        self.assert_error("copy_cancelled", service.confirm_delivery,
                          self.conn, "J1", 2)
        # 撤销释放承诺用纸
        self.assertEqual(self.batch("B2")["committed_original"], 0)
        # 已开印（锁定）的份不能撤销也不能换批
        self.assert_error("copy_already_locked", service.cancel_copy,
                          self.conn, "J1", 1)
        self.assert_error("copy_already_locked",
                          service.reassign_copy_batch,
                          self.conn, "J1", 1, "B1")
        # 撤销幂等
        again = service.cancel_copy(self.conn, "J1", 2)
        self.assertEqual(again["copies"][1]["commitment"]["status"],
                         "cancelled")


class TestReprintStockRules(PaperCommitmentBase):
    def test_reprint_same_batch_waits_and_recovers(self):
        # B1=3、B2=3；工单 1 份 2 页 -> 锁到 B1（先 fit）
        service.create_job(self.conn, "J1", "t", 2, 1, ["B1", "B2"])
        d1 = service.claim_dispatch(self.conn, "ag1")     # p1 原印
        service.receive_receipt(self.conn, d1["dispatch_id"], "ag1",
                                "PAP-1", "RCP-1")
        service.onsite_waste(self.conn, "J1", 1, 1, "PAP-1")
        rp = service.reprint(self.conn, "J1", 1, 1)
        d2 = service.claim_dispatch(self.conn, "ag1")     # p2 原印
        self.assertEqual(d2["page_no"], 2)
        # B1: 到货3 已发2 可用1 承诺0 -> 补打可领且只能同批
        d3 = service.claim_dispatch(self.conn, "ag1")
        self.assertEqual(d3["dispatch_id"], rp["dispatch_id"])
        self.assertEqual(d3["batch_id"], "B1")
        service.receive_receipt(self.conn, d3["dispatch_id"], "ag1",
                                "PAP-2", "RCP-2")
        service.onsite_waste(self.conn, "J1", 1, 1, "PAP-2")
        rp2 = service.reprint(self.conn, "J1", 1, 1)
        # B1 可用 0：补打缺料等待（哪怕 B2 还有 3 张，锁定的份只能用 B1）
        self.assertIsNone(service.claim_dispatch(self.conn, "ag1"))
        self.assertEqual(self.batch("B2")["available"], 3)
        # 同批补入足够纸后可以继续
        service.register_arrival(self.conn, "B1", 1)
        d4 = service.claim_dispatch(self.conn, "ag1")
        self.assertEqual(d4["dispatch_id"], rp2["dispatch_id"])
        self.assertEqual(d4["batch_id"], "B1")

    def test_reprint_never_eats_committed_original(self):
        # B1=3：J1 一份一页 -> 承诺 1
        service.create_job(self.conn, "J1", "t", 1, 1, ["B1"])
        d1 = service.claim_dispatch(self.conn, "ag1")   # J1 原印
        service.receive_receipt(self.conn, d1["dispatch_id"], "ag1",
                                "PAP-1", "RCP-1")
        service.onsite_waste(self.conn, "J1", 1, 1, "PAP-1")
        rp = service.reprint(self.conn, "J1", 1, 1)     # 补打排队
        # 此时 B1 可用 2。J2 两份一页 -> 承诺 2，恰好占满余量
        service.create_job(self.conn, "J2", "t", 1, 2, ["B1"])
        self.assertEqual(self.batch("B1")["available"], 2)
        self.assertEqual(self.batch("B1")["committed_original"], 2)
        # FIFO 中补打 rp 排在 J2 原印之前，但余量 0：
        # 补打不能吃掉任何已承诺的原印用纸 -> 被跳过，J2 原印照常领取
        d2 = service.claim_dispatch(self.conn, "ag1")
        self.assertEqual((d2["job_id"], d2["copy_no"]), ("J2", 1))
        d3 = service.claim_dispatch(self.conn, "ag1")
        self.assertEqual((d3["job_id"], d3["copy_no"]), ("J2", 2))
        # 已接工单的原印全部兑现后，补打仍因缺料等待
        self.assertIsNone(service.claim_dispatch(self.conn, "ag1"))
        queued = dict(self.conn.execute(
            "SELECT * FROM dispatches WHERE dispatch_id=?",
            (rp["dispatch_id"],)).fetchone())
        self.assertEqual(queued["status"], "queued")
        # 同批补入后补打继续
        service.register_arrival(self.conn, "B1", 1)
        d4 = service.claim_dispatch(self.conn, "ag1")
        self.assertEqual(d4["dispatch_id"], rp["dispatch_id"])

    def test_starving_reprint_does_not_block_others(self):
        service.create_job(self.conn, "J1", "t", 2, 1, ["B1"])
        d1 = service.claim_dispatch(self.conn, "ag1")
        service.receive_receipt(self.conn, d1["dispatch_id"], "ag1",
                                "PAP-1", "RCP-1")
        service.onsite_waste(self.conn, "J1", 1, 1, "PAP-1")
        service.reprint(self.conn, "J1", 1, 1)
        service.claim_dispatch(self.conn, "ag1")              # p2 原印
        d3 = service.claim_dispatch(self.conn, "ag1")         # 补打 p1a2
        service.receive_receipt(self.conn, d3["dispatch_id"], "ag1",
                                "PAP-2", "RCP-2")
        service.onsite_waste(self.conn, "J1", 1, 1, "PAP-2")
        rp2 = service.reprint(self.conn, "J1", 1, 1)          # B1 可用 0
        # 新工单排在缺料补打之后，仍照常可领
        service.create_job(self.conn, "J2", "t", 1, 1, ["B2"])
        d4 = service.claim_dispatch(self.conn, "ag1")
        self.assertEqual((d4["job_id"], d4["batch_id"]), ("J2", "B2"))
        self.assertEqual(rp2["status"], "queued")


class TestStockAccountingIntegrity(PaperCommitmentBase):
    def test_no_restore_no_double_deduct(self):
        service.create_job(self.conn, "J1", "t", 1, 1, ["B1"])
        d = service.claim_dispatch(self.conn, "ag1")
        avail = self.batch("B1")["available"]
        self.assertEqual(avail, 2)
        # 停用不恢复
        service.deactivate_dispatch(self.conn, d["dispatch_id"],
                                    "ag1", False)
        self.assertEqual(self.batch("B1")["available"], avail)
        # 核对未出纸不恢复
        service.onsite_no_paper(self.conn, "J1", 1, 1)
        self.assertEqual(self.batch("B1")["available"], avail)
        # 补打领取才再次扣减（一次一张）
        service.reprint(self.conn, "J1", 1, 1)
        d2 = service.claim_dispatch(self.conn, "ag1")
        self.assertEqual(self.batch("B1")["available"], avail - 1)
        # 回执与重复回执都不再扣减
        service.receive_receipt(self.conn, d2["dispatch_id"], "ag1",
                                "PAP-1", "RCP-1")
        service.receive_receipt(self.conn, d2["dispatch_id"], "ag1",
                                "PAP-1", "RCP-1")
        self.assertEqual(self.batch("B1")["available"], avail - 1)
        # 判废不恢复
        service.onsite_waste(self.conn, "J1", 1, 1, "PAP-1")
        self.assertEqual(self.batch("B1")["available"], avail - 1)
        self.assertEqual(self.batch("B1")["withdrawn"], 2)

    def test_receipt_and_retrieve_require_withdrawal(self):
        service.create_job(self.conn, "J1", "t", 1, 1, ["B1"])
        d = self.dispatch_of("J1", 1, 1)
        # 未领取（无领用记录）的派工不能凭回执/找回认定成品
        self.assert_error("no_withdrawal_record", service.receive_receipt,
                          self.conn, d["dispatch_id"], "ag1", "PAP-X",
                          "RCP-X")
        self.assert_error("no_withdrawal_record", service.onsite_retrieve,
                          self.conn, "J1", 1, 1, "PAP-Y")
        # 旧流程工单不受此限
        service.create_job(self.conn, "J2", "旧工单", 1, 1)
        d2 = self.dispatch_of("J2", 1, 1)
        out = service.receive_receipt(self.conn, d2["dispatch_id"], "ag1",
                                      "PAP-L", "RCP-L")
        self.assertFalse(out["duplicate"])

    def test_concurrent_claims_no_duplicate_withdrawal(self):
        service.register_arrival(self.conn, "B1", 5)      # B1=8
        service.create_job(self.conn, "J1", "t", 2, 4, ["B1"])   # 8 页
        claimed, errors = [], []
        lock = threading.Lock()

        def worker():
            try:
                while True:
                    d = service.claim_dispatch(self.conn, "ag1")
                    if d is None:
                        return
                    with lock:
                        claimed.append(d["dispatch_id"])
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(claimed), 8)
        self.assertEqual(len(set(claimed)), 8)          # 无重复领用
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM withdrawals").fetchone()[0], 8)
        self.assertEqual(self.batch("B1")["available"], 0)

    def test_concurrent_acceptance_never_overcommits(self):
        results, lock = {}, threading.Lock()

        def worker(i):
            try:
                service.create_job(self.conn, f"JC{i}", "t", 2, 1,
                                   ["B1", "B2"])
                with lock:
                    results[i] = "ok"
            except DomainError as exc:
                with lock:
                    results[i] = exc.code

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        oks = [i for i, r in results.items() if r == "ok"]
        # 每个已接工单承诺 2 张，总库存 6：至多 3 单，且账实相符
        self.assertLessEqual(len(oks) * 2, 6)
        for b in service.list_batches(self.conn):
            self.assertLessEqual(b["committed_original"], b["available"])
        self.assertTrue(all(r == "insufficient_stock"
                            for r in results.values() if r != "ok"))

    def test_persistence_across_reconnect(self):
        service.create_job(self.conn, "J1", "t", 2, 1, ["B1"])
        d = service.claim_dispatch(self.conn, "ag1")
        self.conn.close()
        self.conn = connect(self.db_path)   # 模拟服务重启
        cm = self.commitment("J1", 1)
        self.assertEqual((cm["status"], cm["batch_id"]), ("locked", "B1"))
        w = service.dispatch_withdrawal(self.conn, d["dispatch_id"])
        self.assertEqual(w["withdrawal"]["batch_id"], "B1")
        self.assertEqual(self.batch("B1")["withdrawn"], 1)
        # 重启后继续领活仍同批
        d2 = service.claim_dispatch(self.conn, "ag1")
        self.assertEqual(d2["batch_id"], "B1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
