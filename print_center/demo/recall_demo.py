"""批次召回 + 柜台换货的本地可复跑演示（含模拟服务重启）。

运行：
    PYTHONPATH=. python -m print_center.demo.recall_demo

每次运行清空 /tmp/print_center_recall_demo/ 并重建 SQLite。演示：
  1. 一份两页追踪工单正常印完并交付；
  2. 库管永久召回 B1，柜台查到该份待收回及交付纸号；
  3. 用 B2 补制备品，走领纸/回执/核对；
  4. 一个换货请求原子完成“收回整份原件 + 交出整份备品”；
  5. 重连同一数据库模拟服务重启，事实仍可查询；
  6. B2 再被召回，名单要求收回的是上次换出的 B2 纸，而不是最初的 B1 纸；
  7. 第二次换货成功，EX-1 重送仍回放第一次换货结果。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from print_center.server.db import connect, init_db
from print_center.server import service
from print_center.server.service import DomainError

STATE = Path("/tmp/print_center_recall_demo")
DB_PATH = STATE / "print_center.db"


def show(title: str, value) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                     default=str))


def must(cond: bool, text: str) -> None:
    if not cond:
        raise AssertionError(text)
    print(f"  ✅ {text}")


def print_two_page_copy(conn, job: str, copy_no: int, prefix: str):
    paper_ids = []
    for page in (1, 2):
        d = service.claim_dispatch(conn, "ag1")
        if d is None or (d["job_id"], d["copy_no"], d["page_no"]) != \
                (job, copy_no, page):
            raise AssertionError(f"未领到 {job}#{copy_no} p{page}: {d}")
        paper_id = f"{prefix}-P{page}"
        service.receive_receipt(
            conn, d["dispatch_id"], "ag1", paper_id,
            f"RCP-{prefix}-P{page}")
        service.onsite_retrieve(conn, job, copy_no, page, paper_id)
        paper_ids.append(paper_id)
    return paper_ids


def main() -> None:
    shutil.rmtree(STATE, ignore_errors=True)
    STATE.mkdir(parents=True, exist_ok=True)
    init_db(DB_PATH)
    conn = connect(DB_PATH)

    try:
        service.register_agent(conn, "ag1")
        service.register_arrival(conn, "B1", 2, "第一批合格入库后发现缺陷")
        service.create_job(
            conn, "J-RECALL", "召回换货演示稿件",
            page_count=2, copy_count=1, allowed_batches=["B1"])
        original_papers = print_two_page_copy(
            conn, "J-RECALL", 1, "ORIG-B1")
        delivered = service.confirm_delivery(conn, "J-RECALL", 1)
        show("原始交付清单（将永久保持原样）", delivered["items"])
        must(len(delivered["items"]) == 2, "原工单整份两页已交付")

        service.recall_batch(conn, "B1", "纸批永久召回")
        pending = service.list_recall_pending(conn, "B1")
        show("B1 召回后的待收回名单", pending)
        must(len(pending) == 1, "B1 名单中有 1 个原交付份")
        must(pending[0]["paper_ids"] == original_papers,
             "待收回纸号与原始交付清单一致")

        # 错误纸号不能只收回一部分/触发换货。
        try:
            service.exchange_copy(
                conn, job_id="J-RECALL", original_copy_no=1,
                recovered_paper_ids=[original_papers[0]],
                spare_copy_no=2, request_id="EX-BAD")
        except DomainError as exc:
            must(exc.code in ("spare_not_found", "not_deliverable",
                              "recovered_paper_mismatch"),
                 f"不完整换货被拒绝：{exc.code}")
        else:
            raise AssertionError("不完整换货不应成功")

        service.register_arrival(conn, "B2", 2, "合格替换纸")
        spare = service.create_spare_copies(
            conn, "J-RECALL", copy_count=1, allowed_batches=["B2"])
        spare_no = spare["copy_nos"][0]
        must(spare_no == 2, "库管补制了备品份 J-RECALL#2")
        spare_papers = print_two_page_copy(
            conn, "J-RECALL", spare_no, "SPARE-B2")
        readiness = service.delivery_readiness(conn, "J-RECALL")
        show("备品交付前核对状态", readiness)

        ex1 = service.exchange_copy(
            conn,
            request_id="EX-2026-0001",
            job_id="J-RECALL",
            original_copy_no=1,
            recovered_paper_ids=original_papers,
            spare_copy_no=spare_no)
        show("第一次换货结果", ex1)
        must(ex1["status"] == "completed", "第一次原件→B2 备品换货成功")
        must(service.list_recall_pending(conn, "B1") == [],
             "换过的客户不再出现在 B1 旧批次待收回名单")
        must(service.batch_view(conn, "B1")["available"] == 0,
             "旧纸没有被算回库存")

        # 同号重送与改内容冲突。
        replay = service.exchange_copy(
            conn, request_id="EX-2026-0001", job_id="J-RECALL",
            original_copy_no=1, recovered_paper_ids=original_papers,
            spare_copy_no=spare_no)
        must(replay["delivered_at"] == ex1["delivered_at"],
             "同换货请求号重送回放当次结果")
        try:
            service.exchange_copy(
                conn, request_id="EX-2026-0001", job_id="J-RECALL",
                original_copy_no=1,
                recovered_paper_ids=original_papers + ["P-CHANGED"],
                spare_copy_no=spare_no)
        except DomainError as exc:
            must(exc.code == "request_id_conflict",
                 "同号改换内容被拒绝")
        else:
            raise AssertionError("同号改换内容不应成功")

        print("\n--- 模拟服务重启：重新执行启动初始化并重连同一 SQLite 文件 ---")
        conn.close()
        init_db(DB_PATH)
        conn = connect(DB_PATH)

        service.recall_batch(conn, "B2", "替换纸后续也被召回")
        pending_b2 = service.list_recall_pending(conn, "B2")
        show("B2 召回后的待收回名单", pending_b2)
        must(len(pending_b2) == 1, "同一原交付份因 B2 再次待收回")
        must(pending_b2[0]["current_copy_no"] == spare_no,
             "当前在手份是上次换出的备品份")
        must(pending_b2[0]["paper_ids"] == spare_papers,
             "这次要求收回上次换出的 B2 纸，而非最初 B1 纸")

        service.register_arrival(conn, "B3", 2, "第二次合格替换纸")
        spare2 = service.create_spare_copies(
            conn, "J-RECALL", copy_count=1, allowed_batches=["B3"])
        spare2_no = spare2["copy_nos"][0]
        spare2_papers = print_two_page_copy(
            conn, "J-RECALL", spare2_no, "SPARE-B3")
        ex2 = service.exchange_copy(
            conn,
            request_id="EX-2026-0002",
            job_id="J-RECALL",
            original_copy_no=1,
            recovered_paper_ids=spare_papers,
            spare_copy_no=spare2_no)
        show("第二次换货结果", ex2)
        must(ex2["returned_copy_no"] == spare_no,
             "第二次收回的是 B2 备品份")
        must(ex2["spare_copy_no"] == spare2_no,
             "第二次交出的是 B3 备品份")
        must(service.list_recall_pending(conn) == [],
             "第二次换货后无待收回份")

        view = service.get_copy_recall_view(conn, "J-RECALL", 1)
        show("原交付份全生命周期（当前在手/原因/历次换货）", view)
        must(view["current_holding"]["physical_copy_no"] == spare2_no,
             "查询能区分当前在手的是最新 B3 备品")
        must(len(view["exchanges"]) == 2, "可查到历次换货")
        original_list = [
            x["paper_id"]
            for x in service.delivery_list(conn, "J-RECALL", 1)["items"]]
        must(original_list == original_papers,
             "最初真实交付清单仍保持原样")

        old_replay = service.get_exchange(conn, "EX-2026-0001")
        must(old_replay["spare_copy_no"] == spare_no,
             "即使以后又换过，EX-1 快照仍指向第一次 B2 备品")
        print("\n🎉 召回换货演示完成：全部关键断言通过；"
              f"数据库保留在 {DB_PATH}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
