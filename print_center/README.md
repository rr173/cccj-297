# 小型印制中心续打服务

按页码编号的单面稿件续打系统。同一工单可印多份，**每份的同一页至多一张可交付纸**。
服务端不接触代理本地介质，只通过两类输入感知物理世界：

1. 代理发来的**产出回执**（可能重复、延迟、乱序）；
2. 现场人员提交的**核对结果**（未出纸 / 找回成品 / 废页 / 矛盾选页）。

另提供**纸料承诺服务**：库管按批次登记到货，新工单可指定允许使用的批次
编号（同一份必须用同一批，不同份可用不同批）；未指定批次的工单走旧流程，
不参与纸料账。

## 核心规则（状态机不变量）

| 规则 | 实现位置 |
| --- | --- |
| 已领取无回执的派工是**未知态**：不算失败也不算成功，超时不重派 | `dispatches.status='claimed'`，仅 `queued` 可被领取 |
| 停用确认只表示"代理不会再执行该派工"，**不替代出纸检查**；本地已执行时只能产生 `attest` 见证纸 | `service.deactivate_dispatch` |
| 宣告未出纸前必须有**原代理的停用确认** | `_require_deactivation` |
| 补打双前置：原派工已停用 **且** 现场判定 `no_paper`/`waste` | `service.reprint` |
| 代理凭本地介质区分"已执行派工"与"首次见到的派工"，崩溃恢复不重复出纸 | `agent/print_agent.py` + `agent/local_store.py` |
| 回执按 `receipt_id` 与（派工+纸张）双重幂等 | `service.receive_receipt` |
| 废页 `waste` 永久不可被晚回执复活，也不能用于交付 | 纸张状态机 + `_quarantine` 不覆盖 waste |
| 晚回执不得替换已交付的补印页；交付清单只追加不回改，矛盾立 `investigations` | `late_receipt_after_delivery` 分支 |
| 晚回执与现场结论矛盾：纸张隔离、暂停该份、现场选定唯一保留页 | `conflict_*` 分支 + `resolve_conflict` |
| 无候选成品的页仍需补打 | `resolve_conflict(keep_paper_id=None)` |
| 整份每页均 `verified` 才能确认交付；清单保留份号、页号、纸张号 | `confirm_delivery` / `delivery_items` |

## 纸料承诺规则

| 规则 | 实现位置 |
| --- | --- |
| 库内可用 = 批次到货合计 − 已发出合计；只有登记到货与领活发纸改变库存 | `stock_arrivals` / `withdrawals`（均只追加） |
| 核对未出纸、判废、停用、重收/晚收回执都**不恢复库存也不再扣减** | 上述流程不写库存表（结构性保证） |
| 指定批次的工单：每份全部原印页能落实到同一允许批次、且不挤掉已接工单原印用纸才受理；否则整单不建并说明缺料 | `service.create_job` + `_solve_assignment` |
| 只要存在能兑现全部承诺的安排就不得拒单：接单时对**未开印份**整体重排求解 | `_solve_assignment`（回溯 + 分数松弛剪枝） |
| 同一批可再入库；未到货的批次按零库存看待 | `register_arrival` / `_available_by_batch` 缺省 0 |
| 未开印的份可换批/整份撤销；撤销后不再派工或交付 | `reassign_copy_batch` / `cancel_copy` |
| 首次领到某份页任务时**对外锁定该份批次**；此后剩余原印与补打都只用这一批，哪怕首张没印出来 | `claim_dispatch` 中 `copy_batch_locked` |
| 每次领到派工就从对应批次发出一张，**领用与派工在同一事务内生效** | `claim_dispatch` 写 `withdrawals` |
| 补打仍按原核对规则申请；领取补打须同批且**不吃掉任何已承诺的原印用纸**，缺料则等待、不挡其他可领任务，同批补纸后继续 | `_claim_decision`（余量检查 + FIFO 跳过） |
| 指定批次工单的成品只能凭**已有领用记录的派工**认定 | `receive_receipt` / `onsite_retrieve` 守卫 |
| 各批尚未发出张数、各份承诺状态与锁定批次、从派工追溯领用批次均可查 | `GET /paper/batches`、`/jobs/{id}/commitments`、`/dispatches/{id}/withdrawal` |

## 目录结构

```
print_center/
├── server/            # FastAPI + SQLite 服务端
│   ├── schema.py      # 表结构（含全部状态 CHECK 约束与纸料账表）
│   ├── db.py          # 连接、串行写事务、审计日志
│   ├── service.py     # 领域状态机（全部业务规则，含纸料承诺）
│   ├── models.py      # 请求模型
│   └── app.py         # HTTP 路由
├── agent/             # 独立本地打印代理（独立 SQLite，服务端不可读）
│   ├── local_store.py # 代理本地介质
│   └── print_agent.py # CLI：领活/出纸/回执/对账/停用，含崩溃注入
├── demo/
│   ├── run_server.py  # uvicorn 启动入口
│   └── demo.py        # 可复跑端到端演示（真实子进程，含服务重启）
└── tests/
    └── test_service.py# 状态机回归测试（unittest，31 例）
```

## 运行

依赖：Python 3.11+，`pip install -r print_center/requirements.txt`。

### 一键演示（推荐）

```bash
PYTHONPATH=. python -m print_center.demo.demo
```

每次运行清空 `/tmp/print_center_demo/` 重建，包含六个场景：

* **A**：代理在"纸已产出、回执未发"时退出（退出码 4）→ 重启对账 →
  停用确认 → 现场核对 → 补打 → 交付；
* **B**：出纸前失联（退出码 3）→ 停用/未出纸 → 补打 → 旧派工晚回执矛盾
  → 隔离选页 → 交付；
* **C**：判废 → 补打 → 交付后晚回执 → 清单不回改、立追查；
* **D**：批次到货 → **库存总数相同但能否接单不同**（3 份×2 页 vs
  2 份×3 页，同为 6 张需求）→ 未到货批次按零库存 → 各批账面查询；
* **E**：未开印份换批（余量守卫 → 同批补货 → 换批成功）→ 整份撤销
  （不再派工/交付）→ 首次领活锁定批次 → 派工追溯领用批次 → 交付；
* **F**：首张没印出来也锁定批次 → 补打只吃余量、**缺纸不挡已接工单
  原印** → **服务重启后承诺与领用仍有效** → 同批补纸后续打 → 交付，
  全程展示发出的纸不再回库。

### 手动分步

```bash
# 终端 1：服务端
PRINTCENTER_DB=/tmp/pc/server.db PYTHONPATH=. \
  python -m print_center.demo.run_server

# 终端 2：代理（本地介质在 /tmp/pc/agent.db）
python -m print_center.agent.print_agent --agent ag1 \
  --db /tmp/pc/agent.db register
python -m print_center.agent.print_agent --agent ag1 \
  --db /tmp/pc/agent.db work --max-jobs 5
# 崩溃注入：--crash after_produce（出纸后回执前）/ after_claim（领取后出纸前）

# 现场与管理（HTTP）
curl -s localhost:8077/onsite/pending | jq
curl -s -XPOST localhost:8077/jobs/J1/copies/1/pages/1/no-paper -d '{}'

# 纸料账（HTTP）
curl -s -XPOST localhost:8077/paper/batches/B1/arrivals -d '{"sheets": 10}'
curl -s -XPOST localhost:8077/jobs -d '{"job_id":"J2","title":"手册", \
  "page_count":3,"copy_count":2,"allowed_batches":["B1","B2"]}'
curl -s localhost:8077/paper/batches | jq          # 各批尚未发出张数
curl -s localhost:8077/jobs/J2/commitments | jq    # 各份承诺状态与锁定批次
```

### 代理 CLI

| 命令 | 含义 |
| --- | --- |
| `register` | 向服务端登记 |
| `work [--max-jobs N] [--crash ...]` | 领活并出纸、发回执 |
| `reconcile` | 回连对账：输出 `first_seen` / `executed` / `seen_but_not_executed` |
| `flush-receipts` | 用原 `receipt_id` 重发所有已出纸未确认回执（幂等） |
| `deactivate <dispatch_id>` | 停用确认（自动查本地介质决定 `executed_local`） |
| `local-state` | 查看本地介质（服务端无法读取） |

加 `--json` 可让命令类操作只输出 JSON。

### HTTP 接口摘要

```
POST   /jobs                                              建工单（可带 allowed_batches 指定批次）
POST   /agents/{id}/claim                                 领活（FIFO；缺料补打自动跳过）
POST   /receipts                                          回执（幂等，处理延迟/乱序）
POST   /deactivations                                     停用确认
GET    /onsite/pending                                    待现场核对项
POST   /jobs/{j}/copies/{c}/pages/{p}/no-paper            现场确认未出纸
POST   /jobs/{j}/copies/{c}/pages/{p}/retrieve            找回成品并确认有效
POST   /jobs/{j}/copies/{c}/pages/{p}/waste               记录废页
POST   /jobs/{j}/copies/{c}/pages/{p}/resolve-conflict    矛盾选页（keep_paper_id 可空）
POST   /jobs/{j}/copies/{c}/pages/{p}/reprint             补打
GET    /jobs/{j}/delivery-readiness                       交付前阻塞检查
POST   /jobs/{j}/copies/{c}/deliver                       确认交付（整页 verified 前置）
GET    /jobs/{j}/delivery-list                            交付清单（份号/页号/纸张号）
POST   /paper/batches/{b}/arrivals                        登记批次到货（同批可再入库）
GET    /paper/batches | /paper/batches/{b}                各批尚未发出张数与承诺占用
GET    /jobs/{j}/commitments                              各份承诺状态及已锁定批次
POST   /jobs/{j}/copies/{c}/reassign-batch                未开印份换批
POST   /jobs/{j}/copies/{c}/cancel                        未开印份整份撤销
GET    /dispatches/{d}/withdrawal                         从派工追溯领用批次
GET    /papers | /dispatches | /investigations | /events  视图与证据链
```

## 测试

```bash
PYTHONPATH=. python -m unittest print_center.tests.test_service -v
```

## 设计说明

* 服务端所有写操作走 `BEGIN IMMEDIATE` + 进程内锁串行化，状态机检查与
  更新在同一事务内完成；全量事件写入 `event_log`，核对动作写入
  `verifications`，回执写入 `receipts`，均只追加，形成完整证据链。
  并发接单、领活、撤销因此不会留下无法兑现的承诺或重复领用；承诺与
  领用都在 SQLite 中持久化，服务重启后仍然有效。
* 代理"出纸"先在本地 SQLite 落盘纸张号与回执键，再发 HTTP 回执；
  因此任何时刻崩溃，恢复后都只会**重发同一回执**而不会产生第二张纸。
* 同一派工若收到两张不同纸张的回执，判定为疑似重复出纸，置矛盾并暂停该份，
  交现场隔离选页。
* 纸料账只有两类写入：库管登记到货（`stock_arrivals`）与领活发纸
  （`withdrawals`，与派工状态变更同事务）。任何核对/判废/停用/回执流程
  都不触碰这两张表，所以库存不会被恢复、也不会被重复扣减。
* 接单可行性 = 已锁定需求 + 未开印可重排的份 + 新单各份，能否全部落实到
  允许批次且不超过各批库内可用。每份不可拆分（同份同批）使该问题一般情形
  为 NP-hard，实现用回溯搜索 + 最大流分数松弛剪枝：只要存在可行安排就
  一定找到，单印制中心规模下为毫秒级。
* 模式变更说明：`job_copies.status` 与 `dispatches.status` 的 CHECK 新增
  `cancelled` 取值；旧的数据库文件请删除后重建（演示每次自动重建）。
