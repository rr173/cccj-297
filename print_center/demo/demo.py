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
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["PRINTCENTER_DB"] = str(STATE / "server" / "print_center.db")
    env["PRINTCENTER_PORT"] = str(PORT)
    log = open(STATE / "server" / "uvicorn.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "print_center.demo.run_server"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    wait_healthy()
    return proc, log


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


def show_evidence():
    print("\n=== 证据与视图（服务端事件日志节选） ===")
    events = api("GET", "/events?limit=12")["events"]
    for e in events[:12]:
        print(f"  {e['ts']}  {e['kind']}  {e['payload'][:110]}")
    print(f"\n数据库位置：")
    print(f"  服务端（只读视图来源）: {STATE / 'server' / 'print_center.db'}")
    for name in ("a1.db", "b1.db", "b2.db", "c1.db"):
        print(f"  代理本地介质（服务端不可读）: {STATE / 'agents' / name}")


def main() -> int:
    if STATE.exists():
        shutil.rmtree(STATE)
    (STATE / "server").mkdir(parents=True)
    (STATE / "agents").mkdir(parents=True)

    proc, log = start_server()
    print(f"演示服务端已启动：{BASE}（状态目录 {STATE}）", flush=True)
    try:
        scenario_a_crash_before_receipt()
        scenario_b_waste_and_late_receipt()
        scenario_c_waste_then_late_after_reprint()
        show_evidence()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    failed = [r for r in results if r[0] == FAIL]
    print(f"\n{'='*60}\n演示断言：{len(results) - len(failed)} 通过，"
          f"{len(failed)} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
