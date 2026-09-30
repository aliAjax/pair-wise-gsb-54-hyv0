# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误（可携带最新余量/缺口等明细）和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口、接续质量、时间窗与占用参数校验。
- `src/repository.py`：SQLite建表、事务、资源占用台账结算与旧数据迁移。
- `src/service.py`：用例编排、权限检查、乐观并发和台账生命周期联动。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、占用台账、并发确认和旧数据迁移测试。

## 抢修资源占用台账

调度员在任务**动员前**为每票任务登记资源占用：船机、接续班组、备缆批次、占用公里数与时间窗。

- **草稿（draft）**：登记后先保留草稿，不扣减余量。备缆余量不足时计算并写清缺口 `shortage_km`，时间窗与其他已确认占用冲突时返回 `window_conflicts` 提示，调度员可反复修改。
- **确认（confirmed）**：`BEGIN IMMEDIATE` 单事务内串行校验——同一时间窗内船机与班组均不能与其他已确认占用重叠、备缆批次余量足够——然后扣减批次余量。两个调度员同时确认同一资源时只有一票成功，失败者在 409 响应的 `details` 中看到最新余量与缺口，草稿保留。
- **消耗（consumed）**：接续成功后按 `spare_used_km` 结算，未消耗部分返还批次余量；实际消耗不得超过占用公里数。
- **释放（released）/ 作废（discarded）**：动员后取消、回港（`return_port`）、接续失败（`splice_fail`）时全额恢复批次余量并释放船机/班组时间窗；动员前取消仅作废草稿，从未扣减。

动员要求存在与本票一致的已确认占用（动员船机须与登记的 `vessel_code` 一致）。每票任务在 draft/confirmed 状态下只允许有一条占用（数据库部分唯一索引保证）。

### 待核对（旧数据迁移）

`POST /api/migrate/legacy`（仅 admin）为已经动员、但没有台账的历史任务补回占用：

- 能在 `spare_batches` 找到批次的：进行中任务补为 confirmed 并扣减余量；已接续/恢复的任务补为 consumed（按历史实际消耗记账，不重复扣减）。
- 找不到批次或余量不足的：补为草稿、任务标记 `reconcile_status=pending_batch` 进入**待核对**，不出现在区段冲突检查中，也不能登记新占用或执行业务动作。
- 补齐批次与公里数后调用核对接口（余量不足会继续保持待核对并返回缺口），核对完成才可重新参与安排。
- 迁移可重复执行，已补回的任务不会重复扣减。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表（旧库自动补列/补表）。

## 主要接口

任务与审计：

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计（含 `pending_reconciliation` 数量）。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
  动作含 `approve / mobilize / survey / splice / test / restore / cancel / return_port / splice_fail`。

资源台账（`dispatcher` 角色，迁移仅 `admin`）：

- `POST /api/batches`：登记备缆批次 `{"batch_no":"B-100","total_km":50}`。
- `GET /api/batches` / `GET /api/batches/{batch_no}`：批次余量。
- `POST /api/batches/{batch_no}/restock`：补充批次库存 `{"add_km":10}`。
- `POST /api/records/{id}/occupations`：登记/修改占用草稿，`data` 含 `vessel_code`、`crew_code`、`batch_no`、`reserve_km`、`window_start`、`window_end`（ISO8601 带时区）、可选 `note`。
- `POST /api/records/{id}/occupations/confirm`：确认占用 `{"expected_version":1}`。
- `GET /api/occupations?status=draft|confirmed|consumed|released`：占用台账。
- `GET /api/pending`：待核对任务列表。
- `POST /api/records/{id}/reconcile`：补齐待核对任务 `{"data":{"batch_no":"B-100","reserve_km":15.75}}`。
- `POST /api/migrate/legacy`：旧数据迁移补占用（admin）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。冲突响应形如：

```json
{"error":"conflict","message":"备缆余量不足，草稿已保留",
 "details":{"batch_no":"B-100","reserve_km":40.0,"batch_available_km":30.0,"shortage_km":10.0}}
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、时间窗互斥、备缆缺口草稿、并发确认只有一票成功、取消/回港/接续失败释放余量、接续结算与返还，以及旧数据迁移和待核对。
