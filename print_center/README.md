# 小型印制中心续打服务

按页码编号的单面稿件续打系统。同一工单可印多份，**每份的同一页至多一张可交付纸**。
服务端不接触代理本地介质，只通过两类输入感知物理世界：

1. 代理发来的**产出回执**（可能重复、延迟、乱序）；
2. 现场人员提交的**核对结果**（未出纸 / 找回成品 / 废页 / 矛盾选页）。

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

## 目录结构

```
print_center/
├── server/            # FastAPI + SQLite 服务端
│   ├── schema.py      # 表结构（含全部状态 CHECK 约束）
│   ├── db.py          # 连接、串行写事务、审计日志
│   ├── service.py     # 领域状态机（全部业务规则）
│   ├── models.py      # 请求模型
│   └── app.py         # HTTP 路由
├── agent/             # 独立本地打印代理（独立 SQLite，服务端不可读）
│   ├── local_store.py # 代理本地介质
│   └── print_agent.py # CLI：领活/出纸/回执/对账/停用，含崩溃注入
├── demo/
│   ├── run_server.py  # uvicorn 启动入口
│   └── demo.py        # 可复跑端到端演示（真实子进程）
└── tests/
    └── test_service.py# 状态机回归测试（unittest，15 例）
```

## 运行

依赖：Python 3.11+，`pip install -r print_center/requirements.txt`。

### 一键演示（推荐）

```bash
PYTHONPATH=. python -m print_center.demo.demo
```

每次运行清空 `/tmp/print_center_demo/` 重建，包含三个场景：

* **A**：代理在"纸已产出、回执未发"时退出（退出码 4）→ 重启对账 →
  停用确认 → 现场核对 → 补打 → 交付；
* **B**：出纸前失联（退出码 3）→ 停用/未出纸 → 补打 → 旧派工晚回执矛盾
  → 隔离选页 → 交付；
* **C**：判废 → 补打 → 交付后晚回执 → 清单不回改、立追查。

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
POST   /jobs                                              建工单（自动生成全部原派工）
POST   /agents/{id}/claim                                 领活（FIFO，仅 queued）
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
GET    /papers | /dispatches | /investigations | /events  视图与证据链
```

## 测试

```bash
PYTHONPATH=. python -m unittest print_center.tests.test_service -v
```

## 设计说明

* 服务端所有写操作走 `BEGIN IMMEDIATE` + 进程内锁串行化，状态机检查与更新在
  同一事务内完成；全量事件写入 `event_log`，核对动作写入 `verifications`，
  回执写入 `receipts`，均只追加，形成完整证据链。
* 代理"出纸"先在本地 SQLite 落盘纸张号与回执键，再发 HTTP 回执；
  因此任何时刻崩溃，恢复后都只会**重发同一回执**而不会产生第二张纸。
* 同一派工若收到两张不同纸张的回执，判定为疑似重复出纸，置矛盾并暂停该份，
  交现场隔离选页。
