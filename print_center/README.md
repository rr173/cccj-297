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

## 批次召回与柜台换货

库管可把**已登记到货**的纸批标记为永久召回。批次只按真实交付纸张追溯：
`delivery_items.paper_id → papers.dispatch_id → withdrawals.batch_id`；
不读取工单允许批次、份承诺批次或当前排产。未纳入纸料追踪的旧工单没有领用
批次，仍照旧办理。

### 状态与查询

* 每次真实交付都在 `copy_custody` 建立物理份持有关系。当前
  `status='with_customer'` 的物理份若有任一张实际用纸来自召回批，就进入
  `/recalls/pending` 待收回名单，并给出整份当前交付纸号、召回批次和原因。
* 从未交付的份没有 custody，不进待收回名单；但召回后普通交付与授信确认都会
  经同一个 `_check_deliverable` 守卫拒绝 `batch_recalled`。
* 授信取件若已在本地固化交付，继续原 settle/清算；若 hold 后、确认交付前
  遇到召回，复用既有 `release_pending → release` 路径释放额度预留，净费用
  为零。
* 换货只移动 custody 当前指针并追加 `copy_exchanges`；原始和各备品的
  `delivery_items` 均只追加、不回改。查询
  `/jobs/{job}/copies/{original_copy}/history` 可同时看到当前在手物理份、
  待召回原因、当前交付清单、最初交付清单和历次换货快照。

### 备品与换货规则

* `POST /jobs/{job}/spares` 按原工单页数创建 `copy_kind='spare'` 的新份，
  可传 `copy_count`/`copies`/`count` 与 `allowed_batches`/`batch_ids`/
  `batches`/`batch_id`。备品仍创建槽位、原派工和 `copy_commitments`，所以
  照常领纸、锁定批次、领用扣库存、核对和补打，并受同一库存可行性规则约束。
* 备品只能由换货交给原工单客户；普通 `/deliver` 与授信取件返回
  `spare_exchange_only`。
* `POST /exchanges`（或
  `/jobs/{job}/copies/{copy}/exchange`）提交
  `request_id`/`exchange_request_id`、原交付份号或当前在手份号、
  `recovered_paper_ids`、`spare_copy_no`。服务在一个 `BEGIN IMMEDIATE`
  事务中核对：
  * 该物理份当前确在客户手中且确因召回待收回；
  * 纸号集合与其当前冻结交付清单完全一致（必须收回完整一份）；
  * 备品属于同一原工单、整份每页 verified、未被其他换货使用，且实际保留纸
    的领用批次均未召回。
* 核对通过后，同一事务内固化备品交付、把旧物理 custody 置为 `returned`、
  把备品 custody 指向逻辑原交付份并写入换货记录。换货不收费、不写回库/到货
  或冲减 `withdrawals`，所以旧纸不会回库存。
* 换货请求号幂等：成功结果保存快照，同号重送（即使该份以后又换过）返回当次
  结果；同号改换工单号、份号、纸号或备品返回 `request_id_conflict`。退回份
  和备品上的唯一约束、custody 部分唯一索引及全局写事务共同防止同一在手份或
  同一备品被两次成功换货。
* 备品后来被召回时，pending 查询沿 custody 找到的是上一次换出的物理份及其
  `delivery_items`，因此要求收回上次的纸而不是最初的纸。

本地演示（直接走领域服务和同一个 SQLite 文件，含重连/启动迁移）：

```bash
PYTHONPATH=. python -m print_center.demo.recall_demo
```

它会清空并重建 `/tmp/print_center_recall_demo/`，走完“原件 B1 召回 →
换 B2 备品 → 重启持久化 → B2 再召回 → 换 B3 备品 → EX-1 快照重放”。

## 企业授信取件

独立的**模拟财务服务**（`finance/`，可独立启动，独立 SQLite，与印制服务
不共用数据库、不接真实支付渠道）管理各企业的初始共享额度（整数分）。
同一企业的不同工单共用同一账户额度。新工单可同时指定
`credit_account_id` 与 `price_per_copy`；两者都省略则仍走原交付流程。

### 资金与请求状态机

* 财务侧每笔取件按**取件请求号**（request_id）幂等：`held`（待清算，
  额度已锁定，其他取件不能再动用）→ `settled`（确已扣费）或
  `voided`（已释放，未产生净费用）。资金台账只追加 hold/settle/release，
  成功交付的一份最终只有 `hold+settle`，即**原价一次净扣费**。
* 印制侧取件请求状态：`pending`（财务结果未定，明确显示待处理）→
  `held` → `delivered`（终态，确已扣费）/ `rejected`（额度不足等，
  无净费用）/ `released`（确认前条件失效，锁定释放、无净费用）。

### 关键规则

| 规则 | 实现位置 |
| --- | --- |
| 确认交付那一刻，成品仍须满足原有交付条件（整份每页 verified），且费用已有确定资金保障（hold 成功后才固化交付） | `service._pickup_held_stage` + `_check_deliverable` |
| 额度不足不得交付，且不产生任何资金台账；原交付入口对授信工单强制拒绝，不能绕过授信 | `finance.service.hold`、`service.confirm_delivery` |
| 同一企业不同工单共用额度；锁定额他人不可动用 | `finance.service._totals` |
| 原请求号重送不多扣费（财务 hold/settle 均幂等）；请求号已用于其他取件则拒绝；同一份换号不能第二次交付/扣费 | `finance.service.hold`、`service.request_credit_pickup` |
| 结果未定时显示"待处理"，绝不凭超时报告成功或认定没扣款 | `finance_client.FinanceUnavailable` + pending/held 状态 |
| 财务无响应不阻塞原有回执、核对和非授信交付 | 财务调用仅限授信取件路径，原流程不依赖财务服务 |
| 恢复后无需人工改账：重送/对账自动收敛，成功交付只留一次净扣费 | `service.reconcile_pickup` / `_drive_pickup` |
| 确认前原交付条件失效：交付不成立，已锁额度必须恢复可用；原尝试终结后可用新请求号重新取件 | `service._pickup_release` |
| 交付清单照旧冻结；交付后纸张矛盾只按原规则追查，不自动退钱（已 settle 不可 release） | 原 `investigations` 流程 + `finance.service.release` 守卫 |

### 两端断连/重启的一致性

印制服务与财务服务是两个进程、两个库，跨服务调用全部携带幂等键，
任何一步"已生效但应答丢失"都靠重送收敛：

* **hold 应答丢失**：印制侧 `pending`，财务侧可能已 `held`；重送同号，
  财务按 request_id 幂等返回 held，随后继续交付与清算，不会重复锁钱。
* **settle 应答丢失**：交付清单已固化、钱仍在 hold；重送补做幂等清算。
* **release 应答丢失**：请求保持 `held` 且标记 `release_pending`，
  重送完成释放。待处理状态在服务重启后仍持久有效。

## 目录结构

```
print_center/
├── server/            # 印制服务 FastAPI + SQLite
│   ├── schema.py      # 表结构（含全部状态 CHECK 约束、纸料账表、授信取件表）
│   ├── db.py          # 连接、串行写事务、审计日志
│   ├── service.py     # 领域状态机（全部业务规则，含纸料承诺与授信取件）
│   ├── finance_client.py # 调用模拟财务服务的 HTTP 客户端（故障=待处理，不臆断）
│   ├── models.py      # 请求模型
│   └── app.py         # HTTP 路由
├── finance/           # 模拟财务服务（独立进程、独立 SQLite，可独立启动）
│   ├── schema.py      # 企业账户、取件资金请求、只追加资金台账
│   ├── db.py          # 独立连接与串行写事务
│   ├── service.py     # hold/settle/release 幂等状态机与额度查询
│   ├── app.py         # 财务 HTTP 路由（默认端口 8078）
│   └── run_server.py  # 财务服务独立启动入口
├── agent/             # 独立本地打印代理（独立 SQLite，服务端不可读）
│   ├── local_store.py # 代理本地介质
│   └── print_agent.py # CLI：领活/出纸/回执/对账/停用，含崩溃注入
├── demo/
│   ├── run_server.py  # 印制服务 uvicorn 启动入口
│   ├── demo.py        # 可复跑端到端演示（真实子进程，含服务重启）
│   ├── recall_demo.py # 召回/两次换货/持久化的本地可复跑演示
│   ├── credit_demo.py # 授信取件端到端演示（印制+财务+故障代理三进程）
│   └── fault_proxy.py # 演示用故障注入代理（复现"已生效但应答丢失"）
└── tests/
    ├── test_service.py# 原有状态机回归测试（unittest，31 例）
    ├── test_recall.py # 批次召回、备品、原子换货与并发回归测试
    └── test_credit.py # 财务服务与授信取件回归测试（unittest，25 例）
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

### 企业授信取件演示（独立财务服务）

```bash
PYTHONPATH=. python -m print_center.demo.credit_demo
```

每次运行清空 `/tmp/print_center_credit_demo/` 重建，真实拉起三个进程
（印制服务、模拟财务服务、故障注入代理），两个业务数据库物理隔离。
八个场景 G1–G8 覆盖：正常一次净扣费、额度不足拒绝、请求号与原入口
守卫、财务宕机待处理且不阻塞非授信交付、**财务已处理(hold 落账)但
应答丢失后重送一致收敛（核心场景 G5）**、确认前条件失效释放后用新
请求号重取、共享额度与交付后不自动退款、双服务重启后持久化收敛。

也可分别独立启动两个服务（各用各的库）：

```bash
# 财务服务（独立库，默认 8078）
FINANCE_DB=/tmp/pc/finance.db FINANCE_PORT=8078 PYTHONPATH=. \
  python -m print_center.finance.run_server
# 印制服务（独立库，默认 8077），通过 FINANCE_URL 访问财务服务
FINANCE_URL=http://127.0.0.1:8078 PRINTCENTER_DB=/tmp/pc/print.db \
  PRINTCENTER_PORT=8077 PYTHONPATH=. python -m print_center.demo.run_server
```

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
POST   /jobs                                              建工单（可带 allowed_batches；或 credit_account_id+price_per_copy 走授信）
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
POST   /jobs/{j}/copies/{c}/deliver                       确认交付（非授信工单；授信工单在此被拒）
GET    /jobs/{j}/delivery-list                            交付清单（份号/页号/纸张号）
POST   /jobs/{j}/credit-pickups                           授信取件（body: copy_no + request_id，幂等）
GET    /credit-pickups/{request_id}                       取件结果 + 资金状态 + 交付清单
GET    /credit-pickups?job_id=...                         取件请求列表
POST   /credit-pickups/{request_id}/reconcile             待处理请求主动对账收敛（同号重送等价）
POST   /paper/batches/{b}/arrivals                        登记批次到货（同批可再入库）
GET    /paper/batches | /paper/batches/{b}                各批尚未发出张数与承诺占用
GET    /jobs/{j}/commitments                              各份承诺状态及已锁定批次
POST   /jobs/{j}/copies/{c}/reassign-batch                未开印份换批
POST   /jobs/{j}/copies/{c}/cancel                        未开印份整份撤销
GET    /dispatches/{d}/withdrawal                         从派工追溯领用批次
POST   /paper/batches/{b}/recall                          库管永久召回纸批（幂等）
GET    /recalls | /recalls/{b}                            召回记录及各批待收回份
GET    /recalls/pending?batch_id=&job_id=                当前在手且因召回待收回的份
GET    /paper/batches/{b}/pending                         某召回批的待收回名单
POST   /jobs/{j}/spares | /jobs/{j}/spare-copies          按原工单补制备品并指定纸批
GET    /jobs/{j}/spares | /spares                         备品列表/是否已具备换货条件
POST   /exchanges | /exchange-requests                    柜台原子换货（请求号幂等）
POST   /jobs/{j}/copies/{c}/exchange                      路径带原交付份的换货入口
GET    /exchanges/{request_id} | /exchanges               当次换货快照/历次换货
GET    /holdings | /copies/in-hand                        当前客户持有物理份
GET    /jobs/{j}/copies/{c}/history                       当前在手、召回原因、历次换货
GET    /papers | /dispatches | /investigations | /events  视图与证据链
```

模拟财务服务（默认 8078，独立库）：

```
POST   /accounts                                          登记企业账户与初始额度（整数分）
GET    /accounts | /accounts/{id}                         当前可用额度（限额−锁定−已扣费）
POST   /holds                                             按 request_id 锁定额度（幂等）
POST   /requests/{id}/settle                              确认扣费（held→settled，幂等）
POST   /requests/{id}/release                             释放锁定（held→voided，幂等）
GET    /requests/{id} | /requests                         按取件请求追溯资金状态与台账
GET    /ledger | /events                                  只追加资金台账与事件
```

## 测试

```bash
# 原有状态机回归（31 例）+ 财务与授信取件回归（25 例）+ 召回换货回归
PYTHONPATH=. python -m unittest print_center.tests.test_service \
  print_center.tests.test_credit print_center.tests.test_recall -v
# 召回换货本地演示（无需财务服务）
PYTHONPATH=. python -m print_center.demo.recall_demo
# 端到端演示（真实三进程，40 项断言）
PYTHONPATH=. python -m print_center.demo.credit_demo
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
* 授信取件的跨服务一致性：两个服务各自用 `BEGIN IMMEDIATE` + 进程内锁
  串行化本地写事务，互不共享数据库，只通过携带 request_id 的幂等 HTTP
  交互。资金侧先 hold 再 settle，确保确认交付时费用已有确定保障、无需
  企业补钱；印制侧把"交付清单固化"放在本地事务内，把资金清算做成可
  安全重放的独立步骤。任一步应答丢失都不会被臆断为成功或未扣——请求
  停在 `pending`/`held` 并明确展示，重送同号或对账时按财务真实状态
  收敛，因此成功交付最终只有一次原价净扣费，条件失效则释放为零净费用。
* 模式变更说明（授信）：`jobs` 新增 `credit_account_id`、
  `price_per_copy` 两列，并新增 `delivery_requests` 表；财务库为全新
  独立文件。旧的印制数据库文件请删除后重建（演示每次自动重建）。
