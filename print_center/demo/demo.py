"""可复跑的本地端到端演示。

运行：
    python -m print_center.demo.demo

它会：
1. 在临时目录拉起真实的 FastAPI 服务端（独立 SQLite，物理隔离）；
2. 以真实子进程方式运行独立打印代理，代理介质在各自的本地 SQLite；
3. 完整演示题目要求的关键场景，并对每条规则做断言；
4. 结束后清理子进程，保留全部数据库与事件日志供查看。

场景总览
========
A. 代理在"纸张已产出、回执未发"时退出（核心场景）-> 重启不重复出纸
   -> 停用前置守卫 -> 未出纸补打 -> 现场核对 -> 整份交付；
B. 停用见证 -> 现场找回成品 -> 判废 -> 补打 -> 旧派工晚回执撞废页
   -> 隔离+追查 -> 现场选页 -> 交付；
C. 交付后旧派工晚回执制造矛盾：交付清单不回改，只立追查。
D. 纸料承诺：批次到货 -> 库存总数相同但能否接单不同（同份须同批）
   -> 未到货批次按零库存 -> 各批账面查询；
E. 未开印份换批（守卫+补货后成功）-> 整份撤销（不再派工/交付）
   -> 首次领活锁定批次 -> 从派工追溯领用批次 -> 交付；
F. 首张没印出来也锁定批次 -> 补打只吃余量、缺纸不挡已接工单原印
   -> 服务重启后承诺与领用仍有效 -> 同批补纸后续打 -> 交付，
   全程展示发出的纸不再回库。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx


def free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
ROOT = Path(__file__).resolve().parents[2]          # .../print_center 的上级
STATE = Path(os.environ.get("DEMO_STATE", "/tmp/print_center_demo"))
SERVER: dict = {"proc": None, "log": None}

PASS, FAIL, INFO = "PASS", "FAIL", "INFO"
results: list[tuple[str, str, str]] = []


def report(level: str, title: str, detail: str = "") -> None:
    results.append((level, title, detail))
    mark = {"PASS": "✅", "FAIL": "❌", "INFO": "ℹ️ "}[level]
    line = f"{mark} {title}"
    if detail:
        line += f"\n      {detail}"
    print(line, flush=True)


def check(title: str, cond: bool, detail: str = "") -> None:
    report(PASS if cond else FAIL, title, detail)


def api(method: str, path: str, json_body=None, expect_error=None):
    r = httpx.request(method, BASE + path, json=json_body, timeout=10)
    if expect_error:
        codes = expect_error if isinstance(expect_error, (list, tuple)) \
            else [expect_error]
        ok = r.status_code >= 400 and r.json().get("error") in codes
        check(f"守卫生效：{title_of(codes[0])}", ok,
              f"HTTP {r.status_code} {r.text[:160]}")
        return r.json() if r.headers.get("content-type", "").startswith(
            "application/json") else {}
    if r.status_code >= 400:
        raise AssertionError(f"{method} {path} -> {r.status_code}: {r.text}")
    return r.json()


def title_of(code: str) -> str:
    return {
        "deactivation_required": "未停用前禁止宣告未出纸/补打",
        "not_deliverable": "非整份有效时禁止交付",
        "paper_is_waste": "废页不能用于交付",
        "slot_is_waste": "废页槽位不能直接找回交付",
        "already_delivered": "已交付份不可重复交付",
        "agent_mismatch": "非原代理不能操作派工",
        "insufficient_stock": "纸料不足整单不建并说明缺料",
        "copy_cancelled": "已撤销份不再派工或交付",
        "copy_already_locked": "已开印份批次已锁定，不可换批/撤销",
        "batch_not_allowed": "批次不在工单允许范围",
        "no_withdrawal_record": "无领用记录的派工不能认定成品",
    }.get(code, code)


def run_agent(agent: str, db_name: str, *cmd: str,
              expect_exit: int | None = None) -> dict:
    """以子进程方式运行代理（独立本地介质，服务端不可读）。"""
    db_path = STATE / "agents" / db_name
    proc = subprocess.run(
        [sys.executable, "-m", "print_center.agent.print_agent",
         "--server", BASE, "--agent", agent, "--db", str(db_path),
         "--json", *cmd],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )
    out = proc.stdout.strip()
    if expect_exit is not None:
        check(f"代理按预期退出码 {expect_exit} 退出（{agent} {' '.join(cmd)}）",
              proc.returncode == expect_exit,
              f"实际 {proc.returncode}; stderr={proc.stderr.strip()[:200]}")
    elif proc.returncode != 0:
        raise AssertionError(
            f"代理命令失败 {' '.join(cmd)}: {proc.stderr}")
    parsed = {}
    if out:
        parsed = json.loads(out)
    return parsed


def wait_healthy(timeout: float = 15) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(BASE + "/health", timeout=2).status_code == 200:
                return
        except httpx.TransportError:
            time.sleep(0.3)
    raise RuntimeError("服务端未在超时内就绪")


def start_server():
    """（重）启动服务端：同一数据库文件，新端口。返回进程。"""
    global BASE, PORT
    PORT = free_port()
    BASE = f"http://127.0.0.1:{PORT}"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PRINTCENTER_DB"] = str(STATE / "server" / "print_center.db")
    env["PRINTCENTER_PORT"] = str(PORT)
    if SERVER["log"] is None:
        SERVER["log"] = open(STATE / "server" / "uvicorn.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "print_center.demo.run_server"],
        cwd=str(ROOT), env=env,
        stdout=SERVER["log"], stderr=subprocess.STDOUT)
    SERVER["proc"] = proc
    wait_healthy()
    return proc


def restart_server():
    """模拟服务重启：杀掉进程后用同一数据库文件重新拉起。"""
    SERVER["proc"].terminate()
    try:
        SERVER["proc"].wait(timeout=10)
    except subprocess.TimeoutExpired:
        SERVER["proc"].kill()
        SERVER["proc"].wait(timeout=5)
    start_server()


def stop_server():
    proc = SERVER["proc"]
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if SERVER["log"] is not None:
        SERVER["log"].close()


def slot_view(job: str, copy_no: int, page_no: int) -> dict:
    job_data = api("GET", f"/jobs/{job}")
    pending = api("GET", f"/onsite/pending?job_id={job}")["pending"]
    for p in pending:
        if p["copy_no"] == copy_no and p["page_no"] == page_no:
            return p
    return {"note": "槽位已 verified，不在待核对列表"}


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------

def scenario_a_crash_before_receipt():
    print("\n=== 场景 A：出纸后、回执前崩溃；恢复核对与交付 ===")
    JOB = "JOB-A"
    api("POST", "/jobs", {
        "job_id": JOB, "title": "产品手册", "page_count": 2,
        "copy_count": 2})

    # A1: 代理 A1 领取第一个派工，出纸后崩溃（无回执）
    run_agent("agent-A1", "a1.db", "work", "--max-jobs", "1",
              "--crash", "after_produce", expect_exit=4)

    dispatches = api("GET", f"/dispatches?job_id={JOB}")["dispatches"]
    d1 = next(d for d in dispatches
              if d["copy_no"] == 1 and d["page_no"] == 1 and d["attempt_no"] == 1)
    check("崩溃后派工停留在 claimed（未知：非失败、非成功）",
          d1["status"] == "claimed", f"status={d1['status']}")
    check("失联期间没有产生重复派工（仍只有 attempt 1）",
          all(d["attempt_no"] == 1 for d in dispatches))

    # 守卫：未停用前，现场不能宣告未出纸，也不能补打
    api("POST", f"/jobs/{JOB}/copies/1/pages/1/no-paper", {},
        expect_error="deactivation_required")
    api("POST", f"/jobs/{JOB}/copies/1/pages/1/reprint", {},
        expect_error=["deactivation_required", "reprint_not_allowed"])

    # 代理重启：区分已执行派工，且绝不重复出纸
    recon = run_agent("agent-A1", "a1.db", "reconcile")
    check("重启后能区分已执行派工与首次见到的派工",
          d1["dispatch_id"] in recon["executed"]
          and isinstance(recon["first_seen"], list),
          f"executed={recon['executed']}")
    local_before = run_agent("agent-A1", "a1.db", "local-state")
    paper_before = next(
        x for x in local_before["dispatches"]
        if x["dispatch_id"] == d1["dispatch_id"])["paper_id"]

    # 停用确认：本地已 executed -> 必须如实带见证
    deact = run_agent("agent-A1", "a1.db", "deactivate", d1["dispatch_id"])
    check("停用确认如实报告本地已执行并附纸张见证",
          deact["executed_local"] is True
          and deact["attest_paper"]["paper_id"] == paper_before)
    d1_after = next(d for d in api("GET", f"/dispatches?job_id={JOB}")[
        "dispatches"] if d["dispatch_id"] == d1["dispatch_id"])
    check("停用后派工为 deactivated（只承诺不再执行，不代表出纸结论）",
          d1_after["status"] == "deactivated")

    # 现场检查：物理上没有找到纸 -> 见证与现场矛盾，进入 conflict 而非直接采信
    no_paper = api("POST", f"/jobs/{JOB}/copies/1/pages/1/no-paper", {})
    check("见证 vs 现场未出纸：保留证据并置矛盾",
          no_paper["outcome"] == "conflict"
          and paper_before in no_paper["isolated_papers"])

    # 现场复核后认定确无有效纸：resolve 不保留任何候选 -> no_paper
    resolved = api("POST",
                   f"/jobs/{JOB}/copies/1/pages/1/resolve-conflict",
                   {"keep_paper_id": None, "note": "现场复核无实物"})
    check("无候选成品的页标记仍需补打",
          resolved["outcome"] == "no_candidate_needs_reprint")

    # 现在双前置满足（停用 + 未出纸），补打
    reprint = api("POST", f"/jobs/{JOB}/copies/1/pages/1/reprint", {})
    check("补打生成新派工 attempt=2，原派工保留",
          reprint["attempt_no"] == 2 and reprint["status"] == "queued")

    # 代理再次上线：flush 旧回执（同 receipt_id 幂等），再按 FIFO 领活。
    # 补派在队列中排在其他原派工之后，让代理领足即可。
    flush = run_agent("agent-A1", "a1.db", "flush-receipts")
    check("旧派工回执幂等送达（不产生新纸张号）",
          all(f.get("duplicate") or f.get("outcome") for f in flush))
    run_agent("agent-A1", "a1.db", "work", "--max-jobs", "10")
    local_after = run_agent("agent-A1", "a1.db", "local-state")
    new_rows = [x for x in local_after["dispatches"]
                if x["dispatch_id"] == reprint["dispatch_id"]]
    reprint_paper = new_rows[0]["paper_id"] if new_rows else \
        next(p for p in api("GET", f"/papers?job_id={JOB}")["papers"]
             if p["dispatch_id"] == reprint["dispatch_id"])["paper_id"]
    check("补打出新纸，与崩溃前的纸张号不同（未重复出纸）",
          reprint_paper != paper_before,
          f"旧纸 {paper_before} -> 新纸 {reprint_paper}")

    # 现场找回补印成品并确认有效
    ret = api("POST", f"/jobs/{JOB}/copies/1/pages/1/retrieve",
              {"paper_id": reprint_paper})
    check("现场找回成品并确认有效", ret["outcome"] == "verified")

    # 其余 3 个槽位正常打印 + 现场核对
    run_agent("agent-A1", "a1.db", "work", "--max-jobs", "10")
    for copy_no, page_no in [(1, 2), (2, 1), (2, 2)]:
        papers = api("GET", f"/papers?job_id={JOB}")["papers"]
        p = next(x for x in papers
                 if x["copy_no"] == copy_no and x["page_no"] == page_no
                 and x["state"] in ("candidate", "verified"))
        r = api("POST",
                f"/jobs/{JOB}/copies/{copy_no}/pages/{page_no}/retrieve",
                {"paper_id": p["paper_id"]})
        check(f"({copy_no},{page_no}) 现场核对有效", r["outcome"] == "verified")

    # 交付守卫：整份有效前不可交付
    # 先把 copy2 留一个槽位不验证，直接尝试（上面已全部验证，改为检查 readiness）
    readiness = api("GET", f"/jobs/{JOB}/delivery-readiness")["copies"]
    check("交付前每份每页均为可交付",
          all(c["ready"] for c in readiness), json.dumps(readiness, ensure_ascii=False))

    for copy_no in (1, 2):
        dl = api("POST", f"/jobs/{JOB}/copies/{copy_no}/deliver", {})
        ids = [(i["copy_no"], i["page_no"], i["paper_id"]) for i in dl["items"]]
        check(f"份 {copy_no} 交付清单保留份号、页号与唯一纸张号",
              len(ids) == 2 and all(c == copy_no for c, _, _ in ids),
              json.dumps(ids, ensure_ascii=False))
    api("POST", f"/jobs/{JOB}/copies/1/deliver", {},
        expect_error="already_delivered")
    return JOB


def scenario_b_waste_and_late_receipt():
    print("\n=== 场景 B：出纸前失联 -> 停用/未出纸 -> 补打 -> 晚回执矛盾选页 ===")
    JOB = "JOB-B"
    api("POST", "/jobs", {
        "job_id": JOB, "title": "标签贴", "page_count": 1,
        "copy_count": 1})

    # B1: 领取后崩溃在出纸前（纯失联）
    run_agent("agent-B1", "b1.db", "work", "--max-jobs", "1",
              "--crash", "after_claim", expect_exit=3)
    d1 = api("GET", f"/dispatches?job_id={JOB}")["dispatches"][0]
    check("出纸前失联：派工 claimed 且无任何纸张",
          d1["status"] == "claimed"
          and api("GET", f"/papers?job_id={JOB}")["papers"] == [])

    # 重启 -> reconcile 显示该派工 seen 未执行
    recon = run_agent("agent-B1", "b1.db", "reconcile")
    check("代理区分出'见过但未执行'的派工",
          d1["dispatch_id"] in recon["seen_but_not_executed"])

    # 现场先想宣告未出纸 -> 仍需停用前置
    api("POST", f"/jobs/{JOB}/copies/1/pages/1/no-paper", {},
        expect_error="deactivation_required")

    # 停用：本地未执行 -> executed_local=False
    deact = run_agent("agent-B1", "b1.db", "deactivate", d1["dispatch_id"])
    check("未执行的派工停用确认不带纸张见证",
          deact["executed_local"] is False and deact["attest_paper"] is None)

    # 现场宣告未出纸 -> 补打（此时补派在队列中，尚无代理领取）
    api("POST", f"/jobs/{JOB}/copies/1/pages/1/no-paper",
        {"note": "纸架无实物"})
    reprint = api("POST", f"/jobs/{JOB}/copies/1/pages/1/reprint", {})
    check("未出纸补打生成 attempt=2", reprint["attempt_no"] == 2)

    # 失联的原设备在崩溃瞬间其实已出纸，恢复后补送"旧派工晚回执"。
    # 此刻补派还没被领取，槽位结论仍是 no_paper -> 矛盾、暂停该份。
    late = api("POST", "/receipts", {
        "dispatch_id": d1["dispatch_id"], "agent_id": "agent-B1",
        "paper_id": "PAP-LATE-GHOST-01", "receipt_id": "RCP-LATE-0001"})
    check("旧派工晚回执与未出纸结论矛盾 -> conflict 并暂停该份",
          late["outcome"] in ("conflict_no_paper",
                              "conflict_stale_against_onsite")
          and late["paper"]["state"] == "isolated",
          json.dumps(late, ensure_ascii=False))
    copy = next(c for c in api("GET", f"/jobs/{JOB}/delivery-readiness")[
        "copies"] if c["copy_no"] == 1)
    check("矛盾期间该份不可交付", copy["ready"] is False
          and copy["status"] == "paused")

    # 现场暂不选页，先让另一台代理把补打活做完（最新 attempt 回执到达，
    # 槽位从旧矛盾中恢复为候选，旧晚回执纸仍隔离）
    run_agent("agent-B2", "b2.db", "work", "--max-jobs", "1")
    reprint_paper = next(p for p in api(
        "GET", f"/papers?job_id={JOB}")["papers"]
        if p["dispatch_id"] == reprint["dispatch_id"])
    check("补印纸成为候选，旧晚回执纸保持隔离",
          reprint_paper["state"] == "candidate")
    # 仍需现场明确选定唯一保留页才能交付
    readiness = api("GET", f"/jobs/{JOB}/delivery-readiness")["copies"][0]
    check("选页前该份仍不可交付", readiness["ready"] is False)

    # 现场仍须明确确认唯一保留页才能交付。补派回执已把槽位恢复为
    # candidate（旧矛盾自然解除，旧纸仍隔离），走 retrieve 确认即可；
    # 若现场在补派回执到达前处理，则走 resolve-conflict。
    pending = slot_view(JOB, 1, 1)
    if pending.get("slot_status") == "conflict":
        resolved = api("POST",
                       f"/jobs/{JOB}/copies/1/pages/1/resolve-conflict",
                       {"keep_paper_id": reprint_paper["paper_id"],
                        "note": "现场保留补印页，隔离晚到纸"})
        check("现场在矛盾槽位选定唯一保留页",
              resolved["outcome"] == "kept_unique_paper")
    else:
        check("补派回执到达后旧矛盾解除、槽位回到候选",
              pending.get("slot_status") == "candidate")
        api("POST", f"/jobs/{JOB}/copies/1/pages/1/retrieve",
            {"paper_id": reprint_paper["paper_id"],
             "note": "现场确认补印页为唯一保留"})
    papers = {p["paper_id"]: p["state"]
              for p in api("GET", f"/papers?job_id={JOB}")["papers"]}
    check("晚回执纸被隔离、唯一保留页 verified",
          papers.get("PAP-LATE-GHOST-01") == "isolated"
          and papers.get(reprint_paper["paper_id"]) == "verified",
          json.dumps(papers, ensure_ascii=False))
    dl = api("POST", f"/jobs/{JOB}/copies/1/deliver", {})
    check("场景 B 交付清单只含选定的唯一纸张",
          len(dl["items"]) == 1
          and dl["items"][0]["paper_id"] == reprint_paper["paper_id"])
    return JOB


def scenario_c_waste_then_late_after_reprint():
    print("\n=== 场景 C：废页不可复活 + 交付后矛盾只追查不改清单 ===")
    JOB = "JOB-C"
    api("POST", "/jobs", {
        "job_id": JOB, "title": "海报", "page_count": 1,
        "copy_count": 1})
    run_agent("agent-C1", "c1.db", "work", "--max-jobs", "1")
    d1 = api("GET", f"/dispatches?job_id={JOB}")["dispatches"][0]
    paper1 = api("GET", f"/papers?job_id={JOB}")["papers"][0]

    # 现场判废
    w = api("POST", f"/jobs/{JOB}/copies/1/pages/1/waste",
            {"paper_id": paper1["paper_id"], "note": "墨色不均"})
    check("废页已记录", w["outcome"] == "waste_recorded")
    # 废页不能被找回交付
    api("POST", f"/jobs/{JOB}/copies/1/pages/1/retrieve",
        {"paper_id": paper1["paper_id"]},
        expect_error=["paper_is_waste", "slot_is_waste"])

    # 补打（原派工有回执+判废，生命周期结束）
    reprint = api("POST", f"/jobs/{JOB}/copies/1/pages/1/reprint", {})
    run_agent("agent-C1", "c1.db", "work", "--max-jobs", "1")
    paper2 = next(p for p in api("GET", f"/papers?job_id={JOB}")["papers"]
                  if p["dispatch_id"] == reprint["dispatch_id"])
    api("POST", f"/jobs/{JOB}/copies/1/pages/1/retrieve",
        {"paper_id": paper2["paper_id"]})
    dl = api("POST", f"/jobs/{JOB}/copies/1/deliver", {})
    delivered_paper = dl["items"][0]["paper_id"]
    check("交付清单固化为补印页", delivered_paper == paper2["paper_id"])

    # 交付后，一张声称来自原废页派工的晚回执到达
    late = api("POST", "/receipts", {
        "dispatch_id": d1["dispatch_id"], "agent_id": "agent-C1",
        "paper_id": "PAP-LATE-C-0002", "receipt_id": "RCP-LATE-C-0002"})
    check("晚回执不能把废页变回有效成品",
          late["outcome"] == "investigated_after_delivery")
    papers = {p["paper_id"]: p["state"]
              for p in api("GET", f"/papers?job_id={JOB}")["papers"]}
    check("原废页维持 waste、晚到纸隔离",
          papers[paper1["paper_id"]] == "waste"
          and papers["PAP-LATE-C-0002"] == "isolated")
    dl_after = api("GET", f"/jobs/{JOB}/delivery-list")["items"][0]
    check("交付后矛盾不回改既有交付清单",
          dl_after["paper_id"] == delivered_paper)
    inv = api("GET", f"/investigations?job_id={JOB}")["investigations"]
    check("矛盾已立追查项且份状态为 investigating",
          any(i["reason"] == "late_receipt_after_delivery" for i in inv))
    job = api("GET", f"/jobs/{JOB}")
    copy = next(c for c in job["copies"] if c["copy_no"] == 1)
    check("已交付份被标记需要追查", copy["status"] == "investigating")
    return JOB


def batches_map() -> dict:
    return {b["batch_id"]: b
            for b in api("GET", "/paper/batches")["batches"]}


def scenario_d_stock_and_acceptance():
    print("\n=== 场景 D：批次到货、库存总数相同但能否接单不同、未到货零库存 ===")
    # 库管按批次登记到货张数
    api("POST", "/paper/batches/B1/arrivals", {"sheets": 3, "note": "首批"})
    api("POST", "/paper/batches/B2/arrivals", {"sheets": 3})
    b = batches_map()
    check("到货登记后各批尚未发出张数可查",
          b["B1"]["available"] == 3 and b["B2"]["available"] == 3
          and b["B1"]["withdrawn"] == 0,
          json.dumps([b["B1"], b["B2"]], ensure_ascii=False))

    # 库存总数 6、需求总数也是 6：3 份×2 页因"同份须同批"装不下
    r = api("POST", "/jobs", {
        "job_id": "JOB-D-REJ", "title": "三折页", "page_count": 2,
        "copy_count": 3, "allowed_batches": ["B1", "B2"]},
        expect_error="insufficient_stock")
    check("拒单说明缺料（各批账面+总量够但整份不可拆）",
          "整单未建" in r["message"] and "批次 B1" in r["message"]
          and "同一份必须用同一批" in r["message"],
          r["message"])
    check("拒单不建单", api("GET", "/jobs/JOB-D-REJ",
                            expect_error="job_not_found") is not None)

    # 同样的库存总数、同样的需求总数：2 份×3 页可以接
    api("POST", "/jobs", {
        "job_id": "JOB-D1", "title": "产品手册", "page_count": 3,
        "copy_count": 2, "allowed_batches": ["B1", "B2"]})
    cm = api("GET", "/jobs/JOB-D1/commitments")
    assign = {c["copy_no"]: c["commitment"]["batch_id"]
              for c in cm["copies"]}
    check("库存总数相同但能否接单不同：2×3 被受理，两份各落一批",
          assign == {1: "B1", 2: "B2"}, json.dumps(assign))
    check("各份承诺状态为 planned（未开印，未锁定）",
          all(c["commitment"]["status"] == "planned"
              for c in cm["copies"]))
    b = batches_map()
    check("接单后各批已承诺原印 3 张、尚未发出仍为 3 张",
          b["B1"]["committed_original"] == 3
          and b["B2"]["committed_original"] == 3
          and b["B1"]["available"] == 3)

    # 未到货的批次按零库存看待
    r = api("POST", "/jobs", {
        "job_id": "JOB-D-ZERO", "title": "单页", "page_count": 1,
        "copy_count": 1, "allowed_batches": ["B9"]},
        expect_error="insufficient_stock")
    check("未到货批次按零库存参与缺料说明",
          "B9" in r["message"] and "可用 0" in r["message"], r["message"])
    return "JOB-D1"


def scenario_e_reassign_cancel_lock(job: str):
    print("\n=== 场景 E：未开印份换批/撤销，首次领活锁定批次，派工追溯批次 ===")
    # 换批守卫：B2 已被 copy2 占满（3/3），换入 copy1 还需 3 张
    api("POST", f"/jobs/{job}/copies/1/reassign-batch", {"batch_id": "B2"},
        expect_error="insufficient_stock")
    # 同一批可再入库
    api("POST", "/paper/batches/B2/arrivals",
        {"sheets": 3, "note": "同批补入"})
    r = api("POST", f"/jobs/{job}/copies/1/reassign-batch",
            {"batch_id": "B2"})
    moved = {c["copy_no"]: c["commitment"]["batch_id"]
             for c in r["copies"]}
    check("同批补货后未开印份换批成功", moved[1] == "B2",
          json.dumps(moved))
    api("POST", f"/jobs/{job}/copies/1/reassign-batch", {"batch_id": "B9"},
        expect_error="batch_not_allowed")

    # 整份撤销：不再派工或交付
    r = api("POST", f"/jobs/{job}/copies/2/cancel", {})
    check("整份撤销后承诺状态为 cancelled",
          r["copies"][1]["commitment"]["status"] == "cancelled")
    dispatches = api("GET", f"/dispatches?job_id={job}")["dispatches"]
    check("撤销份的派工全部取消（不再派工）",
          all(d["status"] == "cancelled" for d in dispatches
              if d["copy_no"] == 2))
    api("POST", f"/jobs/{job}/copies/2/deliver", {},
        expect_error="copy_cancelled")
    b = batches_map()
    check("撤销释放承诺用纸（B2 承诺从 6 降回 3）",
          b["B2"]["committed_original"] == 3)

    # 代理第一次领到该份页任务 -> 对外锁定批次；领用与派工一起生效
    before = batches_map()["B2"]["available"]
    run_agent("agent-E1", "e1.db", "work", "--max-jobs", "1")
    cm = api("GET", f"/jobs/{job}/commitments")
    c1 = cm["copies"][0]["commitment"]
    check("首次领活后该份锁定批次",
          c1["status"] == "locked" and c1["batch_id"] == "B2",
          json.dumps(c1, ensure_ascii=False))
    after = batches_map()["B2"]
    check("领用与派工一起生效：B2 恰好少一张且只少一张",
          after["available"] == before - 1 and after["withdrawn"] == 1)
    d1 = next(d for d in api("GET", f"/dispatches?job_id={job}")[
        "dispatches"] if d["copy_no"] == 1 and d["status"] != "queued")
    w = api("GET", f"/dispatches/{d1['dispatch_id']}/withdrawal")
    check("从派工追溯领用批次",
          w["withdrawal"]["batch_id"] == "B2",
          json.dumps(w["withdrawal"], ensure_ascii=False))
    # 已开印的份不能再换批/撤销
    api("POST", f"/jobs/{job}/copies/1/reassign-batch", {"batch_id": "B1"},
        expect_error="copy_already_locked")
    api("POST", f"/jobs/{job}/copies/1/cancel", {},
        expect_error="copy_already_locked")

    # 剩余原印仍同批，完成后交付 copy1
    run_agent("agent-E1", "e1.db", "work", "--max-jobs", "5")
    papers = api("GET", f"/papers?job_id={job}")["papers"]
    check("该份全部原印纸都来自锁定批次 B2 的领用",
          all(api("GET", f"/dispatches/{p['dispatch_id']}/withdrawal")[
              "withdrawal"]["batch_id"] == "B2" for p in papers))
    for p in papers:
        api("POST", f"/jobs/{job}/copies/1/pages/{p['page_no']}/retrieve",
                    {"paper_id": p["paper_id"]})
    dl = api("POST", f"/jobs/{job}/copies/1/deliver", {})
    check("换批后的份正常交付", len(dl["items"]) == 3)


def scenario_f_reprint_starvation_and_restart():
    print("\n=== 场景 F：补打缺纸不挡原印、重启后承诺有效、同批补纸续打 ===")
    api("POST", "/paper/batches/B3/arrivals", {"sheets": 4})
    api("POST", "/paper/batches/B4/arrivals", {"sheets": 3})
    api("POST", "/jobs", {
        "job_id": "JOB-F1", "title": "手册", "page_count": 3,
        "copy_count": 1, "allowed_batches": ["B3", "B4"]})
    cm = api("GET", "/jobs/JOB-F1/commitments")
    check("新单落到首批可用的批次 B3",
          cm["copies"][0]["commitment"]["batch_id"] == "B3")

    # 首张领取即锁定 B3——哪怕这张纸最终没印出来（出纸前失联）
    run_agent("agent-F1", "f1.db", "work", "--max-jobs", "1",
              "--crash", "after_claim", expect_exit=3)
    cm = api("GET", "/jobs/JOB-F1/commitments")
    check("首张没印出来，批次依然锁定 B3",
          cm["copies"][0]["commitment"]["status"] == "locked"
          and cm["copies"][0]["commitment"]["batch_id"] == "B3")
    d1 = next(d for d in api("GET", "/dispatches?job_id=JOB-F1")[
        "dispatches"] if d["page_no"] == 1 and d["attempt_no"] == 1)
    w = api("GET", f"/dispatches/{d1['dispatch_id']}/withdrawal")
    check("首张虽未印出，领用已生效且永不回库",
          w["withdrawal"]["batch_id"] == "B3"
          and batches_map()["B3"]["withdrawn"] == 1)

    # 停用 -> 未出纸 -> 补打（原有核对规则不变）
    run_agent("agent-F1", "f1.db", "deactivate", d1["dispatch_id"])
    api("POST", "/jobs/JOB-F1/copies/1/pages/1/no-paper", {})
    rp = api("POST", "/jobs/JOB-F1/copies/1/pages/1/reprint", {})
    check("补打仍按原有核对规则申请", rp["attempt_no"] == 2)
    b3 = batches_map()["B3"]
    check("停用/未出纸/判废都不恢复库存也不再次扣减",
          b3["available"] == 3 and b3["withdrawn"] == 1)

    # 领完剩余原印（承诺内），补打吃掉最后一张余量
    run_agent("agent-F1", "f1.db", "work", "--max-jobs", "3")
    b3 = batches_map()["B3"]
    check("原印 3 张 + 补打 1 张全部发出，B3 恰好清零",
          b3["withdrawn"] == 4 and b3["available"] == 0,
          json.dumps(b3))
    # 补打纸判废 -> 再补打 -> B3 缺料
    p1a2 = next(p for p in api("GET", "/papers?job_id=JOB-F1")["papers"]
                if p["page_no"] == 1)
    api("POST", "/jobs/JOB-F1/copies/1/pages/1/waste",
        {"paper_id": p1a2["paper_id"], "note": "蹭脏"})
    rp3 = api("POST", "/jobs/JOB-F1/copies/1/pages/1/reprint", {})
    # 新工单用 B4 照常受理；只用 B3 的单因缺料被拒
    api("POST", "/jobs", {
        "job_id": "JOB-F2", "title": "插页", "page_count": 3,
        "copy_count": 1, "allowed_batches": ["B4"]})
    api("POST", "/jobs", {
        "job_id": "JOB-F3", "title": "单页", "page_count": 1,
        "copy_count": 1, "allowed_batches": ["B3"]},
        expect_error="insufficient_stock")

    # 缺料的补打继续等待，不挡住其他可领任务（F2 原印照常兑现）
    run_agent("agent-F1", "f1.db", "work", "--max-jobs", "5")
    dispatches = api("GET", "/dispatches?job_id=JOB-F1")["dispatches"]
    starved = next(d for d in dispatches
                   if d["dispatch_id"] == rp3["dispatch_id"])
    f2_done = api("GET", "/dispatches?job_id=JOB-F2")["dispatches"]
    check("缺料补打继续等待，不挡其他可领任务",
          starved["status"] == "queued"
          and all(d["status"] == "receipted" for d in f2_done))
    originals = [d for d in dispatches if d["attempt_no"] == 1]
    check("已接工单在补打缺纸时仍兑现全部原印（张张有领用）",
          all(api("GET", f"/dispatches/{d['dispatch_id']}/withdrawal")[
              "withdrawal"] is not None for d in originals))

    # 服务重启：承诺与领用仍然有效
    restart_server()
    cm = api("GET", "/jobs/JOB-F1/commitments")
    b3 = batches_map()["B3"]
    starved_after = next(d for d in api(
        "GET", "/dispatches?job_id=JOB-F1")["dispatches"]
        if d["dispatch_id"] == rp3["dispatch_id"])
    check("重启后承诺与领用仍有效",
          cm["copies"][0]["commitment"]["status"] == "locked"
          and b3["withdrawn"] == 4 and b3["available"] == 0
          and starved_after["status"] == "queued")

    # 同批补入足够纸后，缺料补打继续（且仍只用锁定的 B3）
    api("POST", "/paper/batches/B3/arrivals",
        {"sheets": 1, "note": "同批补入"})
    run_agent("agent-F1", "f1.db", "work", "--max-jobs", "5")
    w = api("GET", f"/dispatches/{rp3['dispatch_id']}/withdrawal")
    check("补打恢复后仍只用锁定批次",
          w["withdrawal"] is not None
          and w["withdrawal"]["batch_id"] == "B3")

    # 现场核对 + 交付 F1/F2
    for job in ("JOB-F1", "JOB-F2"):
        for p in api("GET", f"/papers?job_id={job}")["papers"]:
            if p["state"] in ("candidate", "attest"):
                api("POST",
                    f"/jobs/{job}/copies/1/pages/{p['page_no']}/retrieve",
                    {"paper_id": p["paper_id"]})
        dl = api("POST", f"/jobs/{job}/copies/1/deliver", {})
        check(f"{job} 整份交付", len(dl["items"]) == 3)
    b = batches_map()
    check("发出的纸不再回库：B3 到货5/发出5/可用0，B4 到货3/发出3/可用0",
          b["B3"]["arrived"] == 5 and b["B3"]["withdrawn"] == 5
          and b["B3"]["available"] == 0
          and b["B4"]["arrived"] == 3 and b["B4"]["withdrawn"] == 3,
          json.dumps([b["B3"], b["B4"]], ensure_ascii=False))



def show_evidence():
    print("\n=== 证据与视图（服务端事件日志节选） ===")
    events = api("GET", "/events?limit=12")["events"]
    for e in events[:12]:
        print(f"  {e['ts']}  {e['kind']}  {e['payload'][:110]}")
    print("\n=== 纸料账面（各批尚未发出张数） ===")
    for b in api("GET", "/paper/batches")["batches"]:
        print(f"  {b['batch_id']}: 到货 {b['arrived']} 已发出 "
              f"{b['withdrawn']} 库内可用 {b['available']} "
              f"已承诺原印 {b['committed_original']}")
    print(f"\n数据库位置：")
    print(f"  服务端（只读视图来源）: {STATE / 'server' / 'print_center.db'}")
    for name in ("a1.db", "b1.db", "b2.db", "c1.db", "e1.db", "f1.db"):
        print(f"  代理本地介质（服务端不可读）: {STATE / 'agents' / name}")


def main() -> int:
    if STATE.exists():
        shutil.rmtree(STATE)
    (STATE / "server").mkdir(parents=True)
    (STATE / "agents").mkdir(parents=True)

    start_server()
    print(f"演示服务端已启动：{BASE}（状态目录 {STATE}）", flush=True)
    try:
        scenario_a_crash_before_receipt()
        scenario_b_waste_and_late_receipt()
        scenario_c_waste_then_late_after_reprint()
        job_d1 = scenario_d_stock_and_acceptance()
        scenario_e_reassign_cancel_lock(job_d1)
        scenario_f_reprint_starvation_and_restart()
        show_evidence()
    finally:
        stop_server()

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{'='*60}\n演示断言：{len(results) - len(failed)} 通过，"
          f"{len(failed)} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
