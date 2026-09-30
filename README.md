# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验（含资源冲突与时间窗工具）。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口和接续质量和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询，含备缆批次、资源占用台账和待核对表。
- `src/service.py`：用例编排、权限检查、乐观并发和审计，含占用登记/确认/释放与旧数据迁移。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景与资源台账（含并发确认、迁移待核对）测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 抢修资源占用台账

调度员（角色`dispatcher`，`admin`同样可用）在任务批准后、动员前统一登记资源占用，避免同船机/班组多票撞单、取消后余量不恢复等问题。

流程：任务`approved` → 登记占用（缺缆时保留**草稿**并写清缺口）→ 确认占用（原子扣减批次余量）→ `mobilize`动员 → 终态（取消/回港/接续失败/恢复）自动释放占用并退还未消耗备缆。

台账与批次接口：

- `POST /api/spare-batches`：登记备缆批次，请求体`{"batch_no":"B-1","total_km":20}`。
- `GET /api/spare-batches`：批次列表与最新余量。
- `POST /api/records/{id}/occupations`：登记占用（任务须为`approved`），请求体`{"data":{"vessel_name":"CS-1","splice_crew":"CREW-A","batch_no":"B-1","reserve_km":18,"window_start":"2026-10-01T00:00:00Z","window_end":"2026-10-03T00:00:00Z"}}`。备缆余量不足时返回`status=draft`、`shortfall_km`缺口和文字说明，草稿不扣减余量；同船机或班组时间窗冲突会写入`warnings`。
- `POST /api/occupations/{id}/confirm`：确认占用。船机/班组时间窗查重与备缆扣减在同一`BEGIN IMMEDIATE`事务内完成，两个调度员并发确认只有一票成功；失败者得到`409 resource_conflict`，响应`context`中带批次**最新余量**与缺口。
- `GET /api/occupations?status=draft|active|released`：台账列表。
- `GET /api/records/{id}/occupation`：单票任务的占用明细。
- `POST /api/records/{id}/occupation/cancel`：动员前取消占用，生效占用全额恢复批次余量，草稿直接丢弃。
- `GET /api/reviews`：待核对任务列表。
- `POST /api/records/{id}/review/resolve`：补齐待核对任务的船机/班组/批次/公里数/时间窗后解除冻结。
- `POST /api/occupations/backfill`：旧数据迁移。扫描已动员（`mobilized`及之后状态）但没有台账的任务，按任务数据补登占用并冻结余量；已`restored`的任务只扣实际消耗量。找不到备缆批次（`payload.spare_batch_no`缺失或不存在）的任务进入**待核对**，补齐前不能执行任何调度动作，也不会再次被迁移扫描（接口幂等）。

释放规则：

- `cancel`：动员前取消释放全部占用备缆；动员后取消释放船机班组并退还未消耗备缆。
- `return`（回港，`mobilized`/`surveyed`/`splice_failed` → `returned`）：全部未消耗占用恢复余量。
- `splice_failed`（接续失败）：请求体带`consumed_km`，仅扣除实际消耗，剩余恢复。
- `restore`（恢复完成）：接续时登记`spare_used_km`，终态退还占用与实际消耗的差额。

`mobilize`时`vessel_name`必须与已确认占用一致，且占用公里数不少于任务需求`required_spare_km`，否则拒绝动员。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
