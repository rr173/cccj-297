"""企业授信取件的可复跑本地演示（真实进程、独立持久化）。

运行：
    python -m print_center.demo.credit_demo

拓扑（三个独立进程，两个业务数据库物理隔离、不共用）
====================================================
    柜员(HTTP) -> 印制服务(print_center.db)
                      |  HTTP（经故障代理，可注入断连）
                      v
                 故障代理(8079) -> 财务服务(finance.db)

每次运行清空 /tmp/print_center_credit_demo/ 重建。场景：
  G1 正常授信交付：锁额 -> 交付 -> 一次净扣费，交付清单冻结；
  G2 额度不足不得交付（无净费用）；
  G3 请求号守卫：同号复用他单拒绝、同份换号拒绝、原入口不能绕过授信、
     不使用授信的工单仍走原流程；
  G4 财务无响应：待处理明确展示，不臆断成功/未扣，且不阻塞非授信交付；
  G5 核心场景——财务已处理(hold 已落账)但应答丢失：重送同号得到一致的
     交付与扣费（仅一次净扣费），财务台账可逐笔复核；
  G6 确认前交付条件失效：释放锁定、交付不成立、额度恢复，之后用新请求号
     重新取件成功；
  G7 同企业多工单共享额度 + 交付后纸张矛盾只追查不自动退钱；
  G8 双服务重启后持久化仍然有效，待处理请求重送收敛。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
STATE = Path(os.environ.get("CREDIT_DEMO_STATE",
                            "/tmp/print_center_credit_demo"))

PASS, FAIL, INFO = "PASS", "FAIL", "INFO"
results: list[tuple[str, str, str]] = []


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def report(level: str, title: str, detail: str = "") -> None:
    results.append((level, title, detail))
    mark = {"PASS": "✅", "FAIL": "❌", "INFO": "ℹ️ "}[level]
    line = f"{mark} {title}"
    if detail:
        line += f"\n      {detail}"
    print(line, flush=True)


def check(title: str, cond: bool, detail: str = "") -> None:
    report(PASS if cond else FAIL, title, detail)


class Process:
    def __init__(self, name: str, cmd: list[str], env: dict,
                 log_path: Path):
        self.name = name
        self.log = open(log_path, "w")
        self.proc = subprocess.Popen(
            cmd, cwd=str(ROOT), env=env,
            stdout=self.log, stderr=subprocess.STDOUT)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.log.close()


class World:
    def __init__(self):
        self.pc_port = free_port()
        self.fin_port = free_port()
        self.proxy_port = free_port()
        self.PC = f"http://127.0.0.1:{self.pc_port}"
        self.FIN = f"http://127.0.0.1:{self.fin_port}"
        self.PROXY = f"http://127.0.0.1:{self.proxy_port}"
        self.procs: list[Process] = []

    def start(self) -> None:
        (STATE / "logs").mkdir(parents=True, exist_ok=True)
        base_env = os.environ.copy()
        base_env["PYTHONPATH"] = str(ROOT)

        fin_env = dict(base_env)
        fin_env["FINANCE_DB"] = str(STATE / "finance" / "finance.db")
        fin_env["FINANCE_PORT"] = str(self.fin_port)
        self.procs.append(Process(
            "finance",
            [sys.executable, "-m", "print_center.finance.run_server"],
            fin_env, STATE / "logs" / "finance.log"))

        proxy_env = dict(base_env)
        proxy_env["FINPROXY_UPSTREAM"] = self.FIN
        proxy_env["FINPROXY_PORT"] = str(self.proxy_port)
        self.procs.append(Process(
            "proxy",
            [sys.executable, "-m", "print_center.demo.fault_proxy"],
            proxy_env, STATE / "logs" / "proxy.log"))

        pc_env = dict(base_env)
        pc_env["PRINTCENTER_DB"] = str(STATE / "print" / "print_center.db")
        pc_env["PRINTCENTER_PORT"] = str(self.pc_port)
        # 印制服务只连代理地址，绝不直接打开财务库
        pc_env["FINANCE_URL"] = self.PROXY
        pc_env["FINANCE_TIMEOUT"] = "4"
        self.procs.append(Process(
            "print",
            [sys.executable, "-m", "print_center.demo.run_server"],
            pc_env, STATE / "logs" / "print.log"))

        self._wait(self.FIN + "/health", "财务服务")
        self._wait(self.PROXY + "/__control/stats", "故障代理")
        self._wait(self.PC + "/health", "印制服务")

    def _wait(self, url: str, label: str, timeout: float = 15) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if httpx.get(url, timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.3)
        raise RuntimeError(f"{label} 未在超时内就绪：{url}")

    def stop(self) -> None:
        for p in reversed(self.procs):
            p.stop()

    def restart_print(self) -> None:
        """只重启印制服务（同一数据库文件、新端口），模拟服务重启。"""
        old = next(p for p in self.procs if p.name == "print")
        old.stop()
        self.procs = [p for p in self.procs if p.name != "print"]
        self.pc_port = free_port()
        self.PC = f"http://127.0.0.1:{self.pc_port}"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        env["PRINTCENTER_DB"] = str(STATE / "print" / "print_center.db")
        env["PRINTCENTER_PORT"] = str(self.pc_port)
        env["FINANCE_URL"] = self.PROXY
        proc = Process("print",
                       [sys.executable, "-m",
                        "print_center.demo.run_server"],
                       env, STATE / "logs" / "print_restart.log")
        self.procs.append(proc)
        self._wait(self.PC + "/health", "重启后的印制服务")


# ---------------------------------------------------------------------------
# HTTP 小工具
# ---------------------------------------------------------------------------

def call(method: str, url: str, body=None, timeout: int = 10):
    return httpx.request(method, url, json=body, timeout=timeout)


def api(world: World, method: str, path: str, body=None,
        expect_error=None, timeout: int = 10):
    r = call(method, world.PC + path, body, timeout)
    if expect_error:
        codes = expect_error if isinstance(expect_error, (list, tuple)) \
            else [expect_error]
        ok = r.status_code >= 400 and r.json().get("error") in codes
        check(f"守卫生效：{codes[0]}", ok,
              f"HTTP {r.status_code} {r.text[:180]}")
        return r.json()
    if r.status_code >= 400:
        raise AssertionError(f"{method} {path} -> {r.status_code}: {r.text}")
    return r.json()


def fin(world: World, method: str, path: str, body=None):
    r = call(method, world.FIN + path, body)
    if r.status_code >= 400:
        raise AssertionError(f"FIN {method} {path} -> {r.status_code}: "
                             f"{r.text}")
    return r.json()


def arm_fault(world: World, request_id: str | None = None) -> None:
    """武装一次性断连。给 request_id 时只断该取件请求的转发（按请求体
    中的 request_id 精确匹配），重送与其他请求不受影响。"""
    if request_id is None:
        call("POST", world.PROXY + "/__control/arm")
    else:
        from urllib.parse import quote
        call("POST", world.PROXY +
             f"/__control/arm?path={quote(request_id)}")


def proxy_stats(world: World) -> dict:
    return call("GET", world.PROXY + "/__control/stats").json()


def make_ready(world: World, job: str, copies: int, pages: int) -> None:
    """用 HTTP 走完整代理流程：登记代理 -> 领活出纸回执 -> 现场核对，
    使指定工单的所有份满足原有交付条件（整份每页 verified）。"""
    agent = f"ag-{job.lower()}"
    call("POST", world.PC + "/agents/register", {"agent_id": agent})
    # 全局 FIFO 队列里可能还有其他工单的残留派工，因此持续领取直到
    # 本工单的全部原派工（copies*pages 个）都已收回执，而不是领固定次数。
    need = copies * pages
    got = 0
    seen: set[str] = set()
    for _ in range(need * 4 + 10):
        if got >= need:
            break
        r = call("POST", f"{world.PC}/agents/{agent}/claim")
        if r.status_code >= 400 or r.json().get("dispatch") is None:
            break
        d = r.json()["dispatch"]
        if d["dispatch_id"] in seen:
            break
        seen.add(d["dispatch_id"])
        if d["job_id"] != job:
            continue   # 替别的工单领的活，不计入本工单
        got += 1
        paper_id = (f"PAP-{job}-C{d['copy_no']}P{d['page_no']}"
                    f"A{d['attempt_no']}")
        receipt_id = (f"RCP-{job}-C{d['copy_no']}P{d['page_no']}"
                      f"A{d['attempt_no']}")
        rr = call("POST", world.PC + "/receipts", {
            "dispatch_id": d["dispatch_id"], "agent_id": agent,
            "paper_id": paper_id, "receipt_id": receipt_id})
        if rr.status_code >= 400:
            raise AssertionError(rr.text)
    if got != need:
        raise AssertionError(
            f"{job} 期望领取 {need} 个派工，实际 {got} 个")
    papers = call("GET", world.PC + f"/papers?job_id={job}").json()["papers"]
    for p in papers:
        if p["state"] in ("candidate", "attest"):
            call("POST",
                 f"{world.PC}/jobs/{job}/copies/{p['copy_no']}/pages/"
                 f"{p['page_no']}/retrieve", {"paper_id": p["paper_id"]})


def create_credit_job(world: World, job: str, copies: int, pages: int,
                      price: int, account: str):
    return api(world, "POST", "/jobs", {
        "job_id": job, "title": f"授信工单-{job}", "page_count": pages,
        "copy_count": copies, "credit_account_id": account,
        "price_per_copy": price})


# 每个场景使用独立的足额企业账户，避免不同场景的扣费互相挤占额度；
# 仅 G7 专门用一个低额度账户演示"同一企业多工单共享额度"。
SCENARIO_ACCOUNTS = {
    "G1": ("ENT-G1", 1000), "G2": ("ENT-G2", 500),
    "G3": ("ENT-G3", 1000), "G4": ("ENT-G4", 1000),
    "G5": ("ENT-G5", 1000), "G6": ("ENT-G6", 1000),
    "G8": ("ENT-G8", 1000),
}


def ensure_scenario_accounts(w: World) -> None:
    for tag, (acc, limit) in SCENARIO_ACCOUNTS.items():
        fin(w, "POST", "/accounts",
            {"account_id": acc, "credit_limit": limit, "name": f"企业-{tag}"})


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------

def scenario_g1_happy_path(w: World):
    print("\n=== G1：正常授信交付（锁额→交付→一次净扣费，清单冻结）===")
    ensure_scenario_accounts(w)
    create_credit_job(w, "G1", copies=1, pages=2, price=300,
                      account="ENT-G1")
    make_ready(w, "G1", copies=1, pages=2)
    out = api(w, "POST", "/jobs/G1/credit-pickups",
              {"copy_no": 1, "request_id": "G1-REQ-1"})
    check("授信取件一次完成：状态 delivered、资金 确已扣费",
          out["status"] == "delivered" and out["fund_status"] == "charged",
          json.dumps({k: out[k] for k in ("status", "fund_status")},
                     ensure_ascii=False))
    check("交付清单照旧冻结（2 页，含份号/页号/纸张号）",
          len(out["delivery_list"]) == 2
          and all(x["copy_no"] == 1 for x in out["delivery_list"]),
          json.dumps(out["delivery_list"], ensure_ascii=False))
    acc = fin(w, "GET", "/accounts/ENT-G1")
    check("企业账户：初始 1000、已扣 300、可用 700、无待清算残留",
          acc["credit_limit"] == 1000 and acc["settled_charges"] == 300
          and acc["available"] == 700 and acc["held_pending"] == 0,
          json.dumps(acc, ensure_ascii=False))
    fr = fin(w, "GET", "/requests/G1-REQ-1")
    check("财务台账：hold+settle 各一条，净费用恰为原价 300 一次",
          [x["kind"] for x in fr["ledger"]] == ["hold", "settle"]
          and fr["net_charge"] == 300,
          json.dumps(fr["ledger"], ensure_ascii=False))
    # 柜员查询取件结果与交付清单
    q = api(w, "GET", "/credit-pickups/G1-REQ-1")
    check("柜员可查取件结果及对应交付清单",
          q["status"] == "delivered" and len(q["delivery_list"]) == 2)


def scenario_g2_insufficient(w: World):
    print("\n=== G2：额度不足不得交付（无净费用、无残留锁定）===")
    create_credit_job(w, "G2", copies=1, pages=1, price=800,
                      account="ENT-G2")
    make_ready(w, "G2", 1, 1)
    before = fin(w, "GET", "/accounts/ENT-G2")
    out = api(w, "POST", "/jobs/G2/credit-pickups",
              {"copy_no": 1, "request_id": "G2-REQ-1"})
    check("额度不足：请求 rejected、资金状态 未产生净费用",
          out["status"] == "rejected"
          and out["fund_status"] == "no_net_charge"
          and "insufficient_credit" in out["last_error"],
          json.dumps({k: out.get(k) for k in
                      ("status", "fund_status", "last_error")},
                     ensure_ascii=False))
    after = fin(w, "GET", "/accounts/ENT-G2")
    check("额度不足不动钱：可用额度与调用前一致，财务无该请求台账",
          after["available"] == before["available"],
          f"before={before['available']} after={after['available']}")
    r = call("GET", w.FIN + "/requests/G2-REQ-1")
    check("财务侧查无此请求（未产生任何资金动作）",
          r.status_code == 404, r.text[:120])


def scenario_g3_request_id_guards(w: World):
    print("\n=== G3：请求号守卫与原流程隔离 ===")
    create_credit_job(w, "G3A", copies=1, pages=1, price=100,
                      account="ENT-G3")
    create_credit_job(w, "G3B", copies=1, pages=1, price=100,
                      account="ENT-G3")
    make_ready(w, "G3A", 1, 1)
    make_ready(w, "G3B", 1, 1)
    api(w, "POST", "/jobs/G3A/credit-pickups",
        {"copy_no": 1, "request_id": "G3-SHARED"})
    # 同号用于另一份取件 -> 拒绝
    api(w, "POST", "/jobs/G3B/credit-pickups",
        {"copy_no": 1, "request_id": "G3-SHARED"},
        expect_error="request_id_conflict")
    # 同一份换号 -> 已交付，拒绝第二次
    api(w, "POST", "/jobs/G3A/credit-pickups",
        {"copy_no": 1, "request_id": "G3-NEW-ID"},
        expect_error="already_delivered")
    # 原交付入口不能绕过授信
    api(w, "POST", "/jobs/G3B/copies/1/deliver", {},
        expect_error="credit_delivery_required")
    # 不使用授信的工单仍走原流程，且不触碰财务
    api(w, "POST", "/jobs",
        {"job_id": "G3C", "title": "普通工单", "page_count": 1,
         "copy_count": 1})
    make_ready(w, "G3C", 1, 1)
    dl = api(w, "POST", "/jobs/G3C/copies/1/deliver", {})
    check("非授信工单仍走原交付入口，正常交付", len(dl["items"]) == 1)
    # G3B 用自己的请求号仍可正常取件（前面被拒绝不影响）
    out = api(w, "POST", "/jobs/G3B/credit-pickups",
              {"copy_no": 1, "request_id": "G3B-OWN"})
    check("被拒请求不影响后续：G3B 用独立请求号交付成功",
          out["status"] == "delivered")
    ledger = fin(w, "GET", "/ledger")["ledger"]
    check("普通工单 G3C 在财务台账中没有任何记录",
          all("G3C" not in (x.get("request_id") or "") for x in ledger))


def scenario_g4_finance_down(w: World):
    print("\n=== G4：财务无响应 -> 明确待处理，不阻塞非授信交付 ===")
    create_credit_job(w, "G4", copies=1, pages=1, price=100,
                      account="ENT-G4")
    make_ready(w, "G4", 1, 1)
    # 停掉财务服务，制造真实无响应
    fin_proc = next(p for p in w.procs if p.name == "finance")
    fin_proc.stop()
    w.procs = [p for p in w.procs if p.name != "finance"]
    t0 = time.time()
    out = api(w, "POST", "/jobs/G4/credit-pickups",
              {"copy_no": 1, "request_id": "G4-REQ-1"})
    elapsed = time.time() - t0
    check("财务无响应：请求停在 pending（待处理），不臆断成功或未扣",
          out["status"] == "pending" and out["fund_status"] == "pending",
          json.dumps({k: out.get(k) for k in
                      ("status", "fund_status", "last_error")},
                     ensure_ascii=False))
    check(f"调用在超时内返回（{elapsed:.1f}s），没有长时间挂死",
          elapsed < 12, f"{elapsed:.1f}s")
    cp = call("GET", w.PC + "/jobs/G4").json()["copies"][0]
    check("待处理期间份未被标记交付", cp["status"] == "ready")
    # 非授信交付不受影响
    api(w, "POST", "/jobs",
        {"job_id": "G4L", "title": "普通", "page_count": 1,
         "copy_count": 1})
    make_ready(w, "G4L", 1, 1)
    dl = api(w, "POST", "/jobs/G4L/copies/1/deliver", {})
    check("财务宕机期间非授信交付照常完成", len(dl["items"]) == 1)
    # 重新拉起财务（同库），待处理请求保持可查
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["FINANCE_DB"] = str(STATE / "finance" / "finance.db")
    env["FINANCE_PORT"] = str(w.fin_port)
    proc = Process("finance",
                   [sys.executable, "-m",
                    "print_center.finance.run_server"],
                   env, STATE / "logs" / "finance_restart.log")
    w.procs.append(proc)
    w._wait(w.FIN + "/health", "重启后的财务服务")
    q = api(w, "GET", "/credit-pickups/G4-REQ-1")
    check("财务恢复后待处理请求仍可查、状态仍为待处理（未被擅自推进）",
          q["status"] == "pending")
    # 重送同号收敛
    out = api(w, "POST", "/jobs/G4/credit-pickups",
              {"copy_no": 1, "request_id": "G4-REQ-1"})
    check("恢复后重送同号：交付成功且只扣一次",
          out["status"] == "delivered"
          and fin(w, "GET", "/requests/G4-REQ-1")["net_charge"] == 100)


def scenario_g5_lost_reply_after_hold(w: World):
    print("\n=== G5：财务已处理(hold 落账)但应答丢失 -> 重送一致收敛 ===")
    create_credit_job(w, "G5", copies=1, pages=1, price=250,
                      account="ENT-G5")
    make_ready(w, "G5", 1, 1)
    avail_before = fin(w, "GET", "/accounts/ENT-G5")["available"]

    # 武装一次性断连：代理在财务 hold 已应答后丢弃应答并断连（只针对该请求）
    arm_fault(w, "G5-REQ-1")
    r = call("POST", w.PC + "/jobs/G5/credit-pickups",
             {"copy_no": 1, "request_id": "G5-REQ-1"})
    check("印制服务只看到连接错误（未收到应答）",
          r.status_code in (200, 202, 500, 502, 504),
          f"HTTP {r.status_code}")
    time.sleep(0.3)
    pstats = proxy_stats(w)
    check("故障代理确认：断连已注入一次，财务 hold 实际已转发处理",
          pstats["injected"] >= 1, json.dumps(pstats))

    # 财务侧：钱已锁定（hold 落账）；印制侧：结果未定，显示待处理
    fr = fin(w, "GET", "/requests/G5-REQ-1")
    acc = fin(w, "GET", "/accounts/ENT-G5")
    check("财务侧 hold 已生效：请求 held，可用额度已减少 250",
          fr["status"] == "held"
          and [x["kind"] for x in fr["ledger"]] == ["hold"]
          and acc["available"] == avail_before - 250,
          f"fin_status={fr['status']} avail={acc['available']}")
    q = api(w, "GET", "/credit-pickups/G5-REQ-1")
    check("印制侧如实显示待处理，绝不报告交付成功",
          q["status"] == "pending" and q["fund_status"] == "pending",
          json.dumps({k: q.get(k) for k in ("status", "fund_status")},
                     ensure_ascii=False))

    # 柜员用原请求号重送（故障已自动恢复）：hold 幂等 -> 交付 -> 清算
    out = api(w, "POST", "/jobs/G5/credit-pickups",
              {"copy_no": 1, "request_id": "G5-REQ-1"})
    check("重送后收敛为 delivered / 确已扣费",
          out["status"] == "delivered"
          and out["fund_status"] == "charged"
          and len(out["delivery_list"]) == 1,
          json.dumps({k: out.get(k) for k in
                      ("status", "fund_status")}, ensure_ascii=False))
    fr2 = fin(w, "GET", "/requests/G5-REQ-1")
    g5_ledger = fin(w, "GET", "/ledger?account_id=ENT-G5")["ledger"]
    g5_rows = [x for x in g5_ledger if x["request_id"] == "G5-REQ-1"]
    check("最终只留下原价一次净扣费（G5 仅 hold+settle，无重复扣费）",
          [x["kind"] for x in fr2["ledger"]] == ["hold", "settle"]
          and fr2["net_charge"] == 250
          and len(g5_rows) == 2,
          json.dumps(fr2["ledger"], ensure_ascii=False))
    # 再次重送仍幂等
    again = api(w, "POST", "/jobs/G5/credit-pickups",
                {"copy_no": 1, "request_id": "G5-REQ-1"})
    check("再次重送同号：仍是同一交付结果，不产生第二次扣费",
          again["status"] == "delivered"
          and fin(w, "GET", "/requests/G5-REQ-1")["net_charge"] == 250)
    check("同一份只有一条交付清单记录（未形成第二次交付）",
          len(call("GET", w.PC +
                   "/jobs/G5/delivery-list").json()["items"]) == 1)


def scenario_g6_conditions_fail_then_retry(w: World):
    print("\n=== G6：确认前条件失效 -> 释放恢复，之后新请求号重新取件 ===")
    create_credit_job(w, "G6", copies=1, pages=1, price=200,
                      account="ENT-G6")
    make_ready(w, "G6", 1, 1)
    avail0 = fin(w, "GET", "/accounts/ENT-G6")["available"]

    # 确定性构造"hold 已落账但应答丢失"：只对 G6-REQ-1 注入一次断连，
    # 随后在重送前判废保留纸，使确认前交付条件失效。
    arm_fault(w, "G6-REQ-1")
    call("POST", w.PC + "/jobs/G6/credit-pickups",
         {"copy_no": 1, "request_id": "G6-REQ-1"})
    time.sleep(0.3)
    check("G6 首次请求因应答丢失停在待处理，财务已 hold",
          api(w, "GET", "/credit-pickups/G6-REQ-1")["status"] == "pending"
          and fin(w, "GET", "/requests/G6-REQ-1")["status"] == "held")
    # 重送前，现场判废该份唯一保留纸 -> 原交付条件失效
    paper = call("GET", w.PC + "/papers?job_id=G6").json()["papers"][0]
    api(w, "POST", f"/jobs/G6/copies/1/pages/1/waste",
        {"paper_id": paper["paper_id"], "note": "交付前复检墨脏"})
    out = api(w, "POST", "/jobs/G6/credit-pickups",
              {"copy_no": 1, "request_id": "G6-REQ-1"})
    check("条件失效：交付不成立，锁定释放（released / 未产生净费用）",
          out["status"] == "released"
          and out["fund_status"] == "no_net_charge",
          json.dumps({k: out.get(k) for k in
                      ("status", "fund_status")}, ensure_ascii=False))
    fr = fin(w, "GET", "/requests/G6-REQ-1")
    check("财务台账 hold+release 相抵，净费用 0，额度恢复可用",
          [x["kind"] for x in fr["ledger"]] == ["hold", "release"]
          and fr["net_charge"] == 0
          and fin(w, "GET", "/accounts/ENT-G6")["available"] == avail0,
          json.dumps(fr["ledger"], ensure_ascii=False))

    # 原尝试已确定未交付且资金处理完毕：补打 -> 核对 -> 新请求号重新取件
    api(w, "POST", "/jobs/G6/copies/1/pages/1/reprint", {})
    agent = "ag-g6"
    call("POST", w.PC + "/agents/register", {"agent_id": agent})
    for _ in range(3):
        d = call("POST", f"{w.PC}/agents/{agent}/claim").json()
        if not d.get("dispatch"):
            break
        dd = d["dispatch"]
        call("POST", w.PC + "/receipts", {
            "dispatch_id": dd["dispatch_id"], "agent_id": agent,
            "paper_id": "PAP-G6-NEW", "receipt_id": "RCP-G6-NEW"})
    api(w, "POST", "/jobs/G6/copies/1/pages/1/retrieve",
        {"paper_id": "PAP-G6-NEW"})
    retry = api(w, "POST", "/jobs/G6/credit-pickups",
                {"copy_no": 1, "request_id": "G6-REQ-2"})
    check("用新请求号重新取件成功，仅第二次留下原价一次净扣费",
          retry["status"] == "delivered"
          and fin(w, "GET", "/requests/G6-REQ-2")["net_charge"] == 200
          and fin(w, "GET", "/requests/G6-REQ-1")["net_charge"] == 0)


def scenario_g7_shared_limit_and_no_refund(w: World):
    print("\n=== G7：共享额度 + 交付后纸张矛盾只追查不自动退钱 ===")
    fin(w, "POST", "/accounts",
        {"account_id": "ENT-SHARED", "credit_limit": 500, "name": "共享企业"})
    create_credit_job(w, "G7A", 1, 1, 300, account="ENT-SHARED")
    create_credit_job(w, "G7B", 1, 1, 300, account="ENT-SHARED")
    make_ready(w, "G7A", 1, 1)
    make_ready(w, "G7B", 1, 1)
    api(w, "POST", "/jobs/G7A/credit-pickups",
        {"copy_no": 1, "request_id": "G7A-REQ"})
    out = api(w, "POST", "/jobs/G7B/credit-pickups",
              {"copy_no": 1, "request_id": "G7B-REQ"})
    check("同一企业共享额度：已扣 300 后第二份 300 不足，被拒绝",
          out["status"] == "rejected"
          and "insufficient_credit" in out["last_error"])
    acc = fin(w, "GET", "/accounts/ENT-SHARED")
    check("共享账户账面：限额 500、已扣 300、可用 200",
          acc["settled_charges"] == 300 and acc["available"] == 200,
          json.dumps(acc, ensure_ascii=False))

    # 交付后晚回执制造纸张矛盾：只立追查，不自动退钱
    d1 = call("GET", w.PC + "/dispatches?job_id=G7A").json()[
        "dispatches"][0]
    late = api(w, "POST", "/receipts", {
        "dispatch_id": d1["dispatch_id"], "agent_id": "ag-g7a",
        "paper_id": "PAP-G7-LATE", "receipt_id": "RCP-G7-LATE"})
    check("交付后晚回执：只立追查，交付清单不回改",
          late["outcome"] == "investigated_after_delivery")
    fr = fin(w, "GET", "/requests/G7A-REQ")
    check("财务不自动退款：净扣费仍是 300，台账无 release/退款",
          fr["status"] == "settled" and fr["net_charge"] == 300
          and all(x["kind"] != "release" for x in fr["ledger"]),
          json.dumps(fr["ledger"], ensure_ascii=False))
    inv = api(w, "GET", "/investigations?job_id=G7A")["investigations"]
    check("印制侧已立追查项",
          any(i["reason"] == "late_receipt_after_delivery" for i in inv))


def scenario_g8_restart_persistence(w: World):
    print("\n=== G8：双服务重启后持久化有效，待处理请求重送收敛 ===")
    create_credit_job(w, "G8", 1, 1, 150, account="ENT-G8")
    make_ready(w, "G8", 1, 1)
    arm_fault(w, "G8-REQ-1")
    call("POST", w.PC + "/jobs/G8/credit-pickups",
         {"copy_no": 1, "request_id": "G8-REQ-1"})
    time.sleep(0.3)
    check("重启前：请求待处理、财务已 hold",
          api(w, "GET", "/credit-pickups/G8-REQ-1")["status"] == "pending"
          and fin(w, "GET", "/requests/G8-REQ-1")["status"] == "held")

    # 同时重启印制与财务（各自独立数据库文件不变）
    w.stop()
    w.procs = []
    w.start()
    q = api(w, "GET", "/credit-pickups/G8-REQ-1")
    fr = fin(w, "GET", "/requests/G8-REQ-1")
    check("双服务重启后：待处理请求与财务 hold 均仍持久有效",
          q["status"] == "pending" and fr["status"] == "held"
          and [x["kind"] for x in fr["ledger"]] == ["hold"],
          f"print={q['status']} finance={fr['status']}")
    out = api(w, "POST", "/jobs/G8/credit-pickups",
              {"copy_no": 1, "request_id": "G8-REQ-1"})
    check("重启后重送同号收敛：交付成功、仅一次净扣费",
          out["status"] == "delivered"
          and fin(w, "GET", "/requests/G8-REQ-1")["net_charge"] == 150)


def show_books(w: World):
    print("\n=== 财务端查询：可用额度 + 按取件请求追溯资金状态 ===")
    for a in fin(w, "GET", "/accounts")["accounts"]:
        print(f"  账户 {a['account_id']}: 限额 {a['credit_limit']} "
              f"待清算 {a['held_pending']} 已扣费 {a['settled_charges']} "
              f"可用 {a['available']}")
    for r in fin(w, "GET", "/requests")["requests"]:
        kinds = "+".join(x["kind"] for x in r["ledger"])
        print(f"  请求 {r['request_id']:<12} 账户 {r['account_id']:<10} "
              f"金额 {r['amount']:>4} 状态 {r['status']:<8} "
              f"台账[{kinds}] 净费用 {r['net_charge']}")
    print("\n数据库（物理隔离、不共用）：")
    print(f"  印制服务: {STATE / 'print' / 'print_center.db'}")
    print(f"  财务服务: {STATE / 'finance' / 'finance.db'}")


def main() -> int:
    if STATE.exists():
        shutil.rmtree(STATE)
    for sub in ("print", "finance", "logs"):
        (STATE / sub).mkdir(parents=True, exist_ok=True)

    w = World()
    w.start()
    print(f"演示已启动：印制 {w.PC} | 故障代理 {w.PROXY} | 财务 {w.FIN}")
    print(f"状态目录：{STATE}")
    try:
        scenario_g1_happy_path(w)
        scenario_g2_insufficient(w)
        scenario_g3_request_id_guards(w)
        scenario_g4_finance_down(w)
        scenario_g5_lost_reply_after_hold(w)
        scenario_g6_conditions_fail_then_retry(w)
        scenario_g7_shared_limit_and_no_refund(w)
        scenario_g8_restart_persistence(w)
        show_books(w)
    finally:
        w.stop()

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{'='*64}\n授信演示断言：{len(results) - len(failed)} 通过，"
          f"{len(failed)} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
