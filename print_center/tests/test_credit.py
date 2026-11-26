"""企业授信取件的服务层回归测试。

分两部分：
* finance 服务（独立库）：额度锁定/扣费/释放的幂等与台账正确性；
* 印制服务：授信取件状态机，HTTP 财务调用以 mock 注入，覆盖
  额度不足、请求号冲突/复用、原入口守卫、财务无响应待处理与
  恢复收敛、条件失效释放、重送幂等、共享额度、不自动退款等。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from print_center.finance.db import connect as fin_connect
from print_center.finance.db import init_db as fin_init_db
from print_center.finance import service as fin_service
from print_center.finance.service import FinanceError
from print_center.server.db import connect, init_db
from print_center.server import service
from print_center.server.service import DomainError
from print_center.server import finance_client


# ---------------------------------------------------------------------------
# 财务服务（独立 SQLite）
# ---------------------------------------------------------------------------

class FinanceServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "finance.db"
        fin_init_db(self.db_path)
        self.conn = fin_connect(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def assert_fin_error(self, code, fn, *args, **kw):
        with self.assertRaises(FinanceError) as ctx:
            fn(*args, **kw)
        self.assertEqual(ctx.exception.code, code)


class TestFinanceHoldSettleRelease(FinanceServiceTestBase):
    def test_hold_reduces_available_and_settle_charges_once(self):
        fin_service.create_account(self.conn, "E1", 1000, "企业一")
        r = fin_service.hold(self.conn, "REQ-1", "E1", 300)
        self.assertEqual(r["status"], "held")
        acc = fin_service.get_account(self.conn, "E1")
        self.assertEqual((acc["held_pending"], acc["available"]), (300, 700))
        # 同号重送幂等：不重复锁额
        again = fin_service.hold(self.conn, "REQ-1", "E1", 300)
        self.assertEqual(again["status"], "held")
        acc = fin_service.get_account(self.conn, "E1")
        self.assertEqual((acc["held_pending"], acc["available"]), (300, 700))
        # 清算：held 转 settled，净扣费恰好一次
        s = fin_service.settle(self.conn, "REQ-1")
        self.assertEqual(s["status"], "settled")
        self.assertEqual(s["net_charge"], 300)
        fin_service.settle(self.conn, "REQ-1")          # 重放不重复扣费
        acc = fin_service.get_account(self.conn, "E1")
        self.assertEqual((acc["held_pending"], acc["settled_charges"],
                          acc["available"]), (0, 300, 700))
        ledger = fin_service.ledger(self.conn, "E1")
        self.assertEqual([x["kind"] for x in ledger], ["hold", "settle"])
        self.assertEqual(sum(x["amount"] for x in ledger
                             if x["kind"] == "settle"), 300)

    def test_insufficient_credit_blocks_hold_and_writes_nothing(self):
        fin_service.create_account(self.conn, "E1", 100)
        self.assert_fin_error(
            "insufficient_credit", fin_service.hold,
            self.conn, "REQ-X", "E1", 250)
        acc = fin_service.get_account(self.conn, "E1")
        self.assertEqual((acc["held_pending"], acc["available"]), (0, 100))
        self.assertEqual(fin_service.ledger(self.conn, "E1"), [])
        self.assert_fin_error(
            "request_not_found", fin_service.get_request,
            self.conn, "REQ-X")

    def test_held_money_is_unavailable_to_other_requests(self):
        fin_service.create_account(self.conn, "E1", 500)
        fin_service.hold(self.conn, "REQ-1", "E1", 400)
        # 剩余 100：另一取件要 300 必须被挡住（锁定的钱他人动不了）
        self.assert_fin_error(
            "insufficient_credit", fin_service.hold,
            self.conn, "REQ-2", "E1", 300)
        # 释放后额度恢复，REQ-2 即可锁定
        fin_service.release(self.conn, "REQ-1", "条件失效")
        acc = fin_service.get_account(self.conn, "E1")
        self.assertEqual(acc["available"], 500)
        r2 = fin_service.hold(self.conn, "REQ-2", "E1", 300)
        self.assertEqual(r2["status"], "held")
        view = fin_service.get_request(self.conn, "REQ-1")
        self.assertEqual(view["status"], "voided")
        self.assertEqual(view["net_charge"], 0)        # 未产生净费用

    def test_settled_money_cannot_be_released(self):
        fin_service.create_account(self.conn, "E1", 500)
        fin_service.hold(self.conn, "REQ-1", "E1", 200)
        fin_service.settle(self.conn, "REQ-1")
        self.assert_fin_error(
            "already_settled", fin_service.release, self.conn, "REQ-1")
        # 已扣费不自动退：可用额度不恢复
        self.assertEqual(
            fin_service.get_account(self.conn, "E1")["available"], 300)

    def test_request_id_conflict_rejected(self):
        fin_service.create_account(self.conn, "E1", 1000)
        fin_service.create_account(self.conn, "E2", 1000)
        fin_service.hold(self.conn, "REQ-1", "E1", 100)
        # 同号换账户/金额：拒绝而非静默复用
        self.assert_fin_error(
            "request_id_conflict", fin_service.hold,
            self.conn, "REQ-1", "E2", 100)
        self.assert_fin_error(
            "request_id_conflict", fin_service.hold,
            self.conn, "REQ-1", "E1", 200)

    def test_settle_unknown_and_release_unknown_are_errors(self):
        self.assert_fin_error(
            "request_not_found", fin_service.settle, self.conn, "NOPE")
        self.assert_fin_error(
            "request_not_found", fin_service.release, self.conn, "NOPE")

    def test_shared_account_spans_jobs(self):
        # 同一企业账户被多笔取件共用，可用额度随锁定/扣费联动
        fin_service.create_account(self.conn, "ESH", 600)
        fin_service.hold(self.conn, "J1-C1", "ESH", 200,
                         reference="J1#copy1")
        fin_service.hold(self.conn, "J2-C1", "ESH", 200,
                         reference="J2#copy1")
        self.assertEqual(
            fin_service.get_account(self.conn, "ESH")["available"], 200)
        fin_service.settle(self.conn, "J1-C1")
        fin_service.settle(self.conn, "J2-C1")
        acc = fin_service.get_account(self.conn, "ESH")
        self.assertEqual((acc["settled_charges"], acc["available"]),
                         (400, 200))


# ---------------------------------------------------------------------------
# 印制服务授信取件（mock 财务 HTTP 客户端）
# ---------------------------------------------------------------------------

class CreditPickupTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "srv.db"
        init_db(self.db_path)
        self.conn = connect(self.db_path)
        service.register_agent(self.conn, "ag1")
        self.holds: dict[str, dict] = {}
        self._patchers = [
            mock.patch.object(service.finance_client, "hold",
                              side_effect=self._fake_hold),
            mock.patch.object(service.finance_client, "settle",
                              side_effect=self._fake_settle),
            mock.patch.object(service.finance_client, "release",
                              side_effect=self._fake_release),
            mock.patch.object(service.finance_client, "get_request",
                              side_effect=self._fake_get),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self) -> None:
        for p in self._patchers:
            p.stop()
        self.conn.close()
        self.tmp.cleanup()

    # -- 内存版财务服务（语义与 finance/service.py 对齐）------------------
    def _fake_hold(self, request_id, account_id, amount,
                   reference=None, note=None):
        rec = self.holds.get(request_id)
        if rec is not None:
            if rec["account_id"] != account_id or rec["amount"] != amount:
                raise finance_client.FinanceRejected(
                    "request_id_conflict", "请求号已用于其他取件")
            return {"request_id": request_id, "status": rec["status"],
                    "amount": amount}
        used = sum(r["amount"] for r in self.holds.values()
                   if r["status"] in ("held", "settled"))
        if used + amount > self.credit_limit:
            raise finance_client.FinanceRejected(
                "insufficient_credit",
                f"需要 {amount}，可用 {self.credit_limit - used}")
        self.holds[request_id] = {
            "request_id": request_id, "account_id": account_id,
            "amount": amount, "status": "held"}
        return {"request_id": request_id, "status": "held",
                "amount": amount}

    def _fake_settle(self, request_id, amount=None):
        rec = self.holds.get(request_id)
        if rec is None:
            raise finance_client.FinanceRejected(
                "request_not_found", "无记录")
        if amount is not None and amount != rec["amount"]:
            raise finance_client.FinanceRejected("amount_mismatch", "金额不符")
        if rec["status"] == "settled":
            return {"request_id": request_id, "status": "settled"}
        if rec["status"] != "held":
            raise finance_client.FinanceRejected(
                "request_not_held", f"状态 {rec['status']}")
        rec["status"] = "settled"
        return {"request_id": request_id, "status": "settled"}

    def _fake_release(self, request_id, reason=None):
        rec = self.holds.get(request_id)
        if rec is None:
            raise finance_client.FinanceRejected(
                "request_not_found", "无记录")
        if rec["status"] == "voided":
            return {"request_id": request_id, "status": "voided"}
        if rec["status"] == "settled":
            raise finance_client.FinanceRejected("already_settled", "已扣费")
        rec["status"] = "voided"
        return {"request_id": request_id, "status": "voided"}

    def _fake_get(self, request_id):
        rec = self.holds.get(request_id)
        if rec is None:
            raise finance_client.FinanceRejected(
                "request_not_found", "无记录")
        return dict(rec)

    # -- 夹具 -------------------------------------------------------------
    credit_limit = 1000

    def make_credit_job(self, job_id="JC", page_count=1, copy_count=1,
                        price=300, limit=None):
        if limit is not None:
            self.credit_limit = limit
        return service.create_job(
            self.conn, job_id, "授信工单", page_count, copy_count,
            credit_account_id="ENT-1", price_per_copy=price)

    def make_ready_copy(self, job_id="JC", copy_no=1):
        """领取 -> 回执 -> 现场核对，使该份满足原有交付条件。"""
        dispatches = [d for d in self.conn.execute(
            "SELECT * FROM dispatches WHERE job_id=? AND copy_no=? "
            "ORDER BY page_no", (job_id, copy_no)).fetchall()]
        for i, d in enumerate(dispatches):
            d = dict(d)
            service.claim_dispatch(self.conn, "ag1")
            paper_id = f"PAP-{job_id}-{copy_no}-{d['page_no']}"
            service.receive_receipt(
                self.conn, d["dispatch_id"], "ag1", paper_id,
                f"RCP-{job_id}-{copy_no}-{d['page_no']}")
            service.onsite_retrieve(
                self.conn, job_id, copy_no, d["page_no"], paper_id)

    def pickup(self, job_id, copy_no, request_id):
        return service.request_credit_pickup(
            self.conn, job_id, copy_no, request_id)

    def assert_error(self, code, fn, *args, **kw):
        with self.assertRaises(DomainError) as ctx:
            fn(*args, **kw)
        self.assertEqual(ctx.exception.code, code)


class TestCreditPickupHappyPath(CreditPickupTestBase):
    def test_full_delivery_charges_exactly_once(self):
        self.make_credit_job(price=300)
        self.make_ready_copy()
        out = self.pickup("JC", 1, "REQ-1")
        self.assertEqual(out["status"], "delivered")
        self.assertEqual(out["fund_status"], "charged")
        self.assertEqual(len(out["delivery_list"]), 1)
        self.assertEqual(self.holds["REQ-1"]["status"], "settled")
        self.assertEqual(self.holds["REQ-1"]["amount"], 300)
        # 财务侧只有一次 hold + 一次 settle（净扣费一次）
        self.assertEqual(len(self.holds), 1)
        # 份与交付清单已固化
        cp = dict(self.conn.execute(
            "SELECT * FROM job_copies WHERE job_id='JC' AND copy_no=1"
        ).fetchone())
        self.assertEqual(cp["status"], "delivered")

    def test_resend_same_request_id_is_idempotent(self):
        self.make_credit_job(price=300)
        self.make_ready_copy()
        first = self.pickup("JC", 1, "REQ-1")
        second = self.pickup("JC", 1, "REQ-1")
        third = service.reconcile_pickup(self.conn, "REQ-1")
        self.assertEqual(first["status"], "delivered")
        self.assertEqual(second["status"], "delivered")
        self.assertEqual(third["status"], "delivered")
        # 多次重送：财务侧仍只有同一请求的一次扣费
        self.assertEqual(list(self.holds), ["REQ-1"])
        self.assertEqual(self.holds["REQ-1"]["status"], "settled")

    def test_legacy_job_still_uses_original_entry(self):
        service.create_job(self.conn, "JL", "普通工单", 1, 1)
        self.make_ready_copy("JL")
        # 不使用授信：原入口照常交付
        out = service.confirm_delivery(self.conn, "JL", 1)
        self.assertEqual(len(out["items"]), 1)
        # 授信入口对普通工单拒绝
        self.assert_error(
            "not_credit_job", self.pickup, "JL", 1, "REQ-L")

    def test_original_entry_cannot_bypass_credit(self):
        self.make_credit_job()
        self.make_ready_copy()
        self.assert_error(
            "credit_delivery_required",
            service.confirm_delivery, self.conn, "JC", 1)
        # 没有任何资金动作发生
        self.assertEqual(self.holds, {})


class TestCreditGuards(CreditPickupTestBase):
    def test_insufficient_credit_blocks_delivery(self):
        self.make_credit_job(price=800, limit=500)
        self.make_ready_copy()
        out = self.pickup("JC", 1, "REQ-1")
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["fund_status"], "no_net_charge")
        # 份未交付，财务侧无残留锁定
        cp = dict(self.conn.execute(
            "SELECT * FROM job_copies WHERE job_id='JC' AND copy_no=1"
        ).fetchone())
        self.assertEqual(cp["status"], "ready")
        self.assertNotIn("REQ-1", self.holds)

    def test_request_id_reused_for_other_pickup_rejected(self):
        self.make_credit_job(job_id="J1", copy_count=1)
        service.create_job(self.conn, "J2", "授信工单2", 1, 1,
                           credit_account_id="ENT-1", price_per_copy=100)
        self.make_ready_copy("J1", 1)
        self.pickup("J1", 1, "REQ-SHARED")
        self.assert_error(
            "request_id_conflict", self.pickup, "J2", 1, "REQ-SHARED")

    def test_same_copy_new_id_before_terminal_rejected(self):
        self.make_credit_job(price=100)
        self.make_ready_copy()
        self.pickup("JC", 1, "REQ-1")          # delivered
        # 已交付份：换号再请求 -> 已交付守卫
        self.assert_error(
            "already_delivered", self.pickup, "JC", 1, "REQ-2")

    def test_same_copy_cannot_double_deliver_via_two_requests(self):
        # 额度只够一份，第一份交付后第二请求绝不能再交付/扣费
        self.make_credit_job(price=400, limit=400)
        self.make_ready_copy()
        self.pickup("JC", 1, "REQ-1")
        view = service.get_pickup(self.conn, "REQ-1")
        self.assertEqual(view["status"], "delivered")
        self.assert_error(
            "already_delivered", self.pickup, "JC", 1, "REQ-9")
        self.assertEqual(set(self.holds), {"REQ-1"})

    def test_not_deliverable_conditions_block_pickup(self):
        self.make_credit_job(price=100)
        # 不做现场核对：份不满足原有交付条件。
        # 额度可锁定，但确认交付前条件检查失败 -> 释放锁定，交付不成立
        out = self.pickup("JC", 1, "REQ-1")
        self.assertEqual(out["status"], "released")
        self.assertEqual(out["fund_status"], "no_net_charge")
        self.assertIn("not_deliverable", out["last_error"])
        self.assertEqual(self.holds["REQ-1"]["status"], "voided")
        cp = dict(self.conn.execute(
            "SELECT * FROM job_copies WHERE job_id='JC' AND copy_no=1"
        ).fetchone())
        self.assertNotEqual(cp["status"], "delivered")

    def test_invalid_credit_job_params(self):
        self.assert_error(
            "invalid_credit_job", service.create_job,
            self.conn, "JBAD", "t", 1, 1, credit_account_id="ENT-1")
        self.assert_error(
            "invalid_credit_job", service.create_job,
            self.conn, "JBAD", "t", 1, 1, price_per_copy=10)


class TestFinanceOutageAndRecovery(CreditPickupTestBase):
    def test_hold_timeout_stays_pending_and_blocks_nothing_else(self):
        self.make_credit_job(job_id="J1", price=100)
        self.make_ready_copy("J1")
        # 财务在 hold 阶段无响应
        with mock.patch.object(
                service.finance_client, "hold",
                side_effect=finance_client.FinanceUnavailable("timeout")):
            out = self.pickup("J1", 1, "REQ-1")
        self.assertEqual(out["status"], "pending")
        self.assertEqual(out["fund_status"], "pending")
        self.assertIn("待处理", out["fund_status_text"])
        # 待处理期间不臆断成功：份未交付
        cp = dict(self.conn.execute(
            "SELECT * FROM job_copies WHERE job_id='J1' AND copy_no=1"
        ).fetchone())
        self.assertEqual(cp["status"], "ready")
        # 不阻塞非授信交付：另一普通工单照常走原入口
        service.create_job(self.conn, "JL", "普通", 1, 1)
        self.make_ready_copy("JL")
        legacy = service.confirm_delivery(self.conn, "JL", 1)
        self.assertEqual(len(legacy["items"]), 1)

    def test_recovery_after_lost_hold_reply_converges(self):
        self.make_credit_job(price=250)
        self.make_ready_copy()
        # 第一次：财务其实已锁额，但应答丢失
        real_hold = service.finance_client.hold

        def flaky_hold(request_id, account_id, amount, **kw):
            if request_id == "REQ-1" and not getattr(flaky_hold, "fired", False):
                flaky_hold.fired = True
                real_hold(request_id, account_id, amount, **kw)
                raise finance_client.FinanceUnavailable("connection reset")
            return real_hold(request_id, account_id, amount, **kw)

        with mock.patch.object(service.finance_client, "hold",
                               side_effect=flaky_hold):
            out1 = self.pickup("JC", 1, "REQ-1")
        self.assertEqual(out1["status"], "pending")
        self.assertEqual(self.holds["REQ-1"]["status"], "held")
        # 重送同号：财务 hold 幂等返回 held，随后交付+清算一次完成
        out2 = self.pickup("JC", 1, "REQ-1")
        self.assertEqual(out2["status"], "delivered")
        self.assertEqual(out2["fund_status"], "charged")
        self.assertEqual(self.holds["REQ-1"]["status"], "settled")
        self.assertEqual(len(self.holds), 1)               # 无重复扣费
        self.assertEqual(len(out2["delivery_list"]), 1)

    def test_settle_outage_keeps_held_then_converges(self):
        self.make_credit_job(price=250)
        self.make_ready_copy()
        with mock.patch.object(
                service.finance_client, "settle",
                side_effect=finance_client.FinanceUnavailable("timeout")):
            out = self.pickup("JC", 1, "REQ-1")
        # 交付已固化、钱仍在 hold：显示待清算而非成功臆断
        self.assertEqual(out["status"], "held")
        self.assertEqual(out["fund_status"], "held")
        self.assertEqual(self.holds["REQ-1"]["status"], "held")
        cp = dict(self.conn.execute(
            "SELECT * FROM job_copies WHERE job_id='JC' AND copy_no=1"
        ).fetchone())
        self.assertEqual(cp["status"], "delivered")
        # 恢复后重送：幂等清算，恰好一次净扣费
        again = self.pickup("JC", 1, "REQ-1")
        self.assertEqual(again["status"], "delivered")
        self.assertEqual(self.holds["REQ-1"]["status"], "settled")

    def _drive_to_held_without_local_delivery(self, request_id="REQ-1"):
        """让请求停在 held：财务已锁额，但印制侧尚未固化交付。

        做法：hold 正常；held 阶段先把保留页判废（条件失效），再让
        settle 分支不可达——实际由 _pickup_held_stage 的条件检查直接
        转入 release。这里用 settle 抛错来证明流程不会走到清算。
        """
        paper = dict(self.conn.execute(
            "SELECT * FROM papers WHERE job_id='JC' LIMIT 1").fetchone())
        service.onsite_waste(self.conn, "JC", 1, paper["page_no"],
                             paper["paper_id"], "确认前复检墨脏")
        return paper

    def test_conditions_fail_after_hold_releases_funds(self):
        self.make_credit_job(price=200, limit=1000)
        self.make_ready_copy()
        # hold 成功、确认交付前原条件失效（保留页被判废）
        self._drive_to_held_without_local_delivery()
        with mock.patch.object(
                service.finance_client, "settle",
                side_effect=AssertionError("条件失效时绝不能调用 settle")):
            again = self.pickup("JC", 1, "REQ-1")
        self.assertEqual(again["status"], "released")
        self.assertEqual(again["fund_status"], "no_net_charge")
        self.assertEqual(self.holds["REQ-1"]["status"], "voided")
        cp = dict(self.conn.execute(
            "SELECT * FROM job_copies WHERE job_id='JC' AND copy_no=1"
        ).fetchone())
        self.assertNotEqual(cp["status"], "delivered")
        # 原尝试已确定未交付且资金处理完毕：可用新请求号重新发起取件
        service.onsite_retrieve(
            self.conn, "JC", 1, 1,
            dict(self.conn.execute(
                "SELECT paper_id FROM papers WHERE state='verified' "
                "LIMIT 1").fetchone())["paper_id"]) \
            if self.conn.execute(
                "SELECT 1 FROM papers WHERE state='verified' LIMIT 1"
            ).fetchone() else None
        # 若没有现成 verified 纸，则补打一张再核对
        if self.conn.execute(
                "SELECT 1 FROM page_slots WHERE status='verified' LIMIT 1"
        ).fetchone() is None:
            service.reprint(self.conn, "JC", 1, 1)
            d2 = dict(self.conn.execute(
                "SELECT * FROM dispatches WHERE job_id='JC' "
                "ORDER BY attempt_no DESC LIMIT 1").fetchone())
            service.receive_receipt(
                self.conn, d2["dispatch_id"], "ag1", "PAP-NEW", "RCP-NEW")
            service.onsite_retrieve(self.conn, "JC", 1, 1, "PAP-NEW")
        retry = self.pickup("JC", 1, "REQ-2")
        self.assertEqual(retry["status"], "delivered")
        # 第一次无净费用，第二次原价一次净扣费
        self.assertEqual(self.holds["REQ-1"]["status"], "voided")
        self.assertEqual(self.holds["REQ-2"]["status"], "settled")
        self.assertEqual(self.holds["REQ-2"]["amount"], 200)

    def test_release_outage_then_converges(self):
        self.make_credit_job(price=200)
        self.make_ready_copy()
        self._drive_to_held_without_local_delivery()
        # release 应答丢失：请求保持 held + release_pending（待处理）
        with mock.patch.object(
                service.finance_client, "release",
                side_effect=finance_client.FinanceUnavailable("reset")), \
             mock.patch.object(
                service.finance_client, "settle",
                side_effect=AssertionError("条件失效时绝不能调用 settle")):
            out = self.pickup("JC", 1, "REQ-1")
        self.assertEqual(out["status"], "held")
        self.assertEqual(out["release_pending"], 1)
        self.assertEqual(self.holds["REQ-1"]["status"], "held")
        # 财务侧锁定仍在（钱没被错误释放或扣走），但结果明确显示待处理
        self.assertEqual(out["fund_status"], "held")
        # 恢复后重送同号：幂等 release 完成，请求终结为未产生净费用
        again = self.pickup("JC", 1, "REQ-1")
        self.assertEqual(again["status"], "released")
        self.assertEqual(self.holds["REQ-1"]["status"], "voided")
        self.assertEqual(again["net_charge"] if "net_charge" in again else
                         again["fund_status"], "no_net_charge")


class TestSharedCreditAndNoRefund(CreditPickupTestBase):
    def test_two_jobs_share_one_account_limit(self):
        self.credit_limit = 500
        service.create_job(self.conn, "K1", "t", 1, 1,
                           credit_account_id="ENT-1", price_per_copy=300)
        service.create_job(self.conn, "K2", "t", 1, 1,
                           credit_account_id="ENT-1", price_per_copy=300)
        self.make_ready_copy("K1")
        self.make_ready_copy("K2")
        out1 = self.pickup("K1", 1, "R1")
        self.assertEqual(out1["status"], "delivered")
        # 共享额度 500：已扣 300，第二份 300 不足
        out2 = self.pickup("K2", 1, "R2")
        self.assertEqual(out2["status"], "rejected")
        self.assertIn("insufficient_credit", out2["last_error"])
        self.assertEqual(set(self.holds), {"R1"})

    def test_post_delivery_conflict_does_not_refund(self):
        self.make_credit_job(price=150)
        self.make_ready_copy()
        out = self.pickup("JC", 1, "REQ-1")
        self.assertEqual(out["status"], "delivered")
        # 交付后晚回执制造纸张矛盾：只立追查，财务不自动退钱
        d1 = dict(self.conn.execute(
            "SELECT * FROM dispatches WHERE job_id='JC' LIMIT 1").fetchone())
        late = service.receive_receipt(
            self.conn, d1["dispatch_id"], "ag1", "PAP-LATE", "RCP-LATE")
        self.assertEqual(late["outcome"], "investigated_after_delivery")
        self.assertEqual(self.holds["REQ-1"]["status"], "settled")
        inv = service.list_investigations(self.conn, "JC")
        self.assertTrue(any(
            i["reason"] == "late_receipt_after_delivery" for i in inv))
        # 交付清单照旧冻结
        dl = service.delivery_list(self.conn, "JC")["items"]
        self.assertEqual(len(dl), 1)
        self.assertNotEqual(dl[0]["paper_id"], "PAP-LATE")


class TestPersistenceAcrossRestart(CreditPickupTestBase):
    def test_pending_and_held_survive_restart(self):
        self.make_credit_job(price=100)
        self.make_ready_copy()
        with mock.patch.object(
                service.finance_client, "hold",
                side_effect=finance_client.FinanceUnavailable("down")):
            out = self.pickup("JC", 1, "REQ-1")
        self.assertEqual(out["status"], "pending")
        # 模拟印制服务重启：重连同一数据库
        self.conn.close()
        self.conn = connect(self.db_path)
        view = service.get_pickup(self.conn, "REQ-1")
        self.assertEqual(view["status"], "pending")
        # 财务恢复后重送收敛
        again = service.request_credit_pickup(self.conn, "JC", 1, "REQ-1")
        self.assertEqual(again["status"], "delivered")
        self.assertEqual(self.holds["REQ-1"]["status"], "settled")


if __name__ == "__main__":
    unittest.main()
