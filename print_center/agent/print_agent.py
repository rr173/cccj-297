"""独立本地打印代理。

与服务端的契约
==============
* 代理通过 HTTP 与服务端交互，本地介质（local_store）对服务端不可见。
* 领活后先在本地标记 seen；实际"出纸"时生成唯一纸张编号并落盘
  （executed + paper_id + receipt_id），**然后**才发回执。
  因此"纸已经产出但还没回执时退出"后重启，代理能凭本地记录把同一
  张纸的回执重发，不会重复出纸。
* 重启后先拉取自己名下全部派工，与本地记录比对，明确区分：
  - 第一次见到的派工（无本地 seen/executed 记录）；
  - 已经执行过的派工（有 executed 记录）。
* 停用确认前先查本地：若本地已 executed，必须如实带 executed_local
  与 evidence_paper_id；它只表示"不会再执行"，不代表服务端可据此
  判定出纸——仍由现场核对。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .local_store import open_local


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PrintAgent:
    def __init__(self, server_url: str, agent_id: str, local_db: str,
                 quiet: bool = False):
        self.server = server_url.rstrip("/")
        self.agent_id = agent_id
        self.db = open_local(local_db)
        self.client = httpx.Client(base_url=self.server, timeout=10)
        self.quiet = quiet

    def log(self, kind: str, payload: dict) -> None:
        self.db.execute(
            "INSERT INTO local_events(ts,kind,payload) VALUES (?,?,?)",
            (utcnow(), kind,
             json.dumps(payload, ensure_ascii=False, sort_keys=True)))
        self.db.commit()
        if not self.quiet:
            print(f"[agent {self.agent_id}] {kind}: "
                  f"{json.dumps(payload, ensure_ascii=False)}")

    # ------------------------------------------------------------------
    def register(self) -> None:
        r = self.client.post("/agents/register",
                             json={"agent_id": self.agent_id})
        r.raise_for_status()
        self.log("registered", {"agent_id": self.agent_id})

    def _local(self, dispatch_id: str):
        row = self.db.execute(
            "SELECT * FROM local_dispatches WHERE dispatch_id=?",
            (dispatch_id,)).fetchone()
        return dict(row) if row else None

    def _persist_seen(self, d: dict) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO local_dispatches(dispatch_id,job_id,"
            "copy_no,page_no,attempt_no,first_seen_at,status) "
            "VALUES (?,?,?,?,?,?, 'seen')",
            (d["dispatch_id"], d["job_id"], d["copy_no"], d["page_no"],
             d["attempt_no"], utcnow()))
        self.db.commit()

    def _produce_paper(self, d: dict) -> dict:
        """模拟物理出纸：先持久化纸张身份，再继续后续动作。"""
        local = self._local(d["dispatch_id"])
        if local and local["status"] == "executed":
            # 崩溃恢复：绝不重复出纸，复用原纸张号与回执键
            return {"paper_id": local["paper_id"],
                    "receipt_id": local["receipt_id"], "reused": True}
        paper_id = f"PAP-{uuid.uuid4().hex[:10].upper()}"
        receipt_id = f"RCP-{uuid.uuid4().hex[:10].upper()}"
        cur = self.db.execute(
            "UPDATE local_dispatches SET status='executed', paper_id=?, "
            "receipt_id=?, produced_at=? WHERE dispatch_id=? AND status='seen'",
            (paper_id, receipt_id, utcnow(), d["dispatch_id"]))
        self.db.commit()
        if cur.rowcount != 1:
            # 并发/状态异常时拒绝在非 seen 记录上"再次出纸"
            raise RuntimeError(
                f"派工 {d['dispatch_id']} 本地状态非 seen，拒绝重复出纸")
        self.log("paper_produced", {
            "dispatch_id": d["dispatch_id"], "paper_id": paper_id,
            "copy_no": d["copy_no"], "page_no": d["page_no"]})
        return {"paper_id": paper_id, "receipt_id": receipt_id,
                "reused": False}

    def _send_receipt(self, d: dict, paper_id: str, receipt_id: str) -> dict:
        r = self.client.post("/receipts", json={
            "dispatch_id": d["dispatch_id"], "agent_id": self.agent_id,
            "paper_id": paper_id, "receipt_id": receipt_id})
        r.raise_for_status()
        out = r.json()
        self.db.execute(
            "UPDATE local_dispatches SET receipt_acked=1 WHERE dispatch_id=?",
            (d["dispatch_id"],))
        self.db.commit()
        self.log("receipt_sent", {"dispatch_id": d["dispatch_id"],
                                  "paper_id": paper_id,
                                  "outcome": out.get("outcome")})
        return out

    # ------------------------------------------------------------------
    def claim(self, crash: str | None = None) -> dict | None:
        """领一个活并执行。

        crash 注入点（演示用）：
        * ``after_produce``：纸张已落盘、回执未发即退出（核心场景）；
        * ``after_claim``：领取后、出纸前退出（纯失联，无纸）；
        * ``none``：正常执行到回执确认。
        """
        r = self.client.post(f"/agents/{self.agent_id}/claim")
        r.raise_for_status()
        d = r.json()["dispatch"]
        if d is None:
            return None

        local = self._local(d["dispatch_id"])
        if local is None:
            self._persist_seen(d)
            self.log("first_seen_dispatch", {"dispatch_id": d["dispatch_id"]})
        else:
            self.log("known_dispatch", {
                "dispatch_id": d["dispatch_id"],
                "local_status": local["status"]})

        if crash == "after_claim":
            self.log("crash_after_claim",
                     {"dispatch_id": d["dispatch_id"]})
            sys.exit(3)

        produced = self._produce_paper(d)
        self.log(("paper_reused" if produced["reused"]
                  else "paper_produced_continue"),
                 {"dispatch_id": d["dispatch_id"],
                  "paper_id": produced["paper_id"]})

        if crash == "after_produce":
            # 关键演示点：纸已产出、本地已落盘，但回执尚未发出
            self.log("crash_after_produce_before_receipt",
                     {"dispatch_id": d["dispatch_id"],
                      "paper_id": produced["paper_id"]})
            sys.exit(4)

        out = self._send_receipt(d, produced["paper_id"],
                                 produced["receipt_id"])
        return out

    def work_loop(self, max_jobs: int = 100, idle_seconds: float = 0.3,
                  crash: str | None = None) -> int:
        self.register()
        done = 0
        while done < max_jobs:
            out = self.claim(crash=crash if done == 0 else None)
            if out is None:
                time.sleep(idle_seconds)
                break
            done += 1
        self.log("work_loop_done", {"jobs": done})
        return done

    # ------------------------------------------------------------------
    def reconcile(self) -> dict:
        """重启后回连：区分首次见到与已执行过的派工。"""
        r = self.client.get(f"/agents/{self.agent_id}/dispatches")
        r.raise_for_status()
        server_dispatches = r.json()["dispatches"]
        first_seen, executed, seen = [], [], []
        for d in server_dispatches:
            local = self._local(d["dispatch_id"])
            if local is None:
                first_seen.append(d["dispatch_id"])
            elif local["status"] == "executed":
                executed.append(d["dispatch_id"])
            else:
                seen.append(d["dispatch_id"])
        self.log("reconcile", {
            "first_seen": first_seen, "executed": executed,
            "seen_but_not_executed": seen})
        return {"first_seen": first_seen, "executed": executed,
                "seen_but_not_executed": seen,
                "server_dispatches": server_dispatches}

    def flush_pending_receipts(self) -> list[dict]:
        """把"已出纸但回执未确认"的派工用同一 receipt_id 重发。

        服务端按 receipt_id 幂等去重，重复/延迟/乱序到达都安全。
        """
        rows = self.db.execute(
            "SELECT * FROM local_dispatches WHERE status='executed' "
            "AND receipt_acked=0").fetchall()
        results = []
        for row in rows:
            row = dict(row)
            # 重新取服务端派工详情做载体
            r = self.client.get(f"/agents/{self.agent_id}/dispatches")
            r.raise_for_status()
            d = next((x for x in r.json()["dispatches"]
                      if x["dispatch_id"] == row["dispatch_id"]), None)
            if d is None:
                self.log("flush_skip_missing",
                         {"dispatch_id": row["dispatch_id"]})
                continue
            out = self._send_receipt(d, row["paper_id"], row["receipt_id"])
            results.append(out)
        self.log("flush_done", {"count": len(results)})
        return results

    # ------------------------------------------------------------------
    def deactivate(self, dispatch_id: str) -> dict:
        """停用确认：先查本地介质，再如实声明。"""
        local = self._local(dispatch_id)
        if local is None:
            raise SystemExit(
                f"拒绝停用：本地无派工 {dispatch_id} 记录，"
                "不能由未知代理声明停用")
        executed = local["status"] == "executed"
        evidence = local["paper_id"] if executed else None
        r = self.client.post("/deactivations", json={
            "dispatch_id": dispatch_id, "agent_id": self.agent_id,
            "executed_local": executed,
            "evidence_paper_id": evidence})
        r.raise_for_status()
        self.log("deactivated", {
            "dispatch_id": dispatch_id, "executed_local": executed,
            "evidence_paper_id": evidence})
        return r.json()

    def local_state(self) -> dict:
        dispatches = [dict(r) for r in self.db.execute(
            "SELECT dispatch_id,job_id,copy_no,page_no,attempt_no,status,"
            "paper_id,receipt_id,receipt_acked FROM local_dispatches "
            "ORDER BY copy_no,page_no,attempt_no").fetchall()]
        return {"agent_id": self.agent_id, "dispatches": dispatches}


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="本地打印代理")
    p.add_argument("--server", default="http://127.0.0.1:8077")
    p.add_argument("--agent", required=True)
    p.add_argument("--db", required=True, help="代理本地介质路径")
    p.add_argument("--json", dest="quiet", action="store_true",
                   help="静默日志，命令结果只输出 JSON")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("register")
    w = sub.add_parser("work")
    w.add_argument("--max-jobs", type=int, default=100)
    w.add_argument("--crash", choices=["after_claim", "after_produce"],
                   default=None)
    sub.add_parser("reconcile")
    sub.add_parser("flush-receipts")
    sub.add_parser("local-state")
    dv = sub.add_parser("deactivate")
    dv.add_argument("dispatch_id")
    args = p.parse_args(argv)

    agent = PrintAgent(args.server, args.agent, args.db, quiet=args.quiet)
    if args.cmd == "register":
        agent.register()
    elif args.cmd == "work":
        agent.work_loop(max_jobs=args.max_jobs, crash=args.crash)
    elif args.cmd == "reconcile":
        print(json.dumps(agent.reconcile(), ensure_ascii=False, indent=2))
    elif args.cmd == "flush-receipts":
        print(json.dumps(agent.flush_pending_receipts(),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "deactivate":
        print(json.dumps(agent.deactivate(args.dispatch_id),
                         ensure_ascii=False, indent=2))
    elif args.cmd == "local-state":
        print(json.dumps(agent.local_state(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
