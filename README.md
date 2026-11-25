# 巡展道具箱 · 点阵牌识别 API

FastAPI + SQLite 实现的 5×5 黑白点阵牌册与确认读数服务。临时添牌也必须保住
已确认箱号。

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000/docs
```

Compose 启动时会自动幂等写入演示数据（`SEED_DEMO=1`）。在 `/docs` 的
`POST /api/boxes` 里直接 **Try it out → Execute**（请求体已预填“拦截牌”），
即可看到一条四角遮挡的已确认读数拦下新牌：

```json
HTTP 409
{
  "error": "registration_conflict",
  "conflicting_box_numbers": ["BOX-DEMO-01"],
  "conflicting_box_number": "BOX-DEMO-01",
  "related_reading_ids": ["RD-0001"],
  "related_reading_id": "RD-0001",
  "conflicts": [{
    "kind": "confirmed_reading_matches_new_tag",
    "box_number": "BOX-DEMO-01",
    "reading_id": "RD-0001",
    "match": {"orientation": 0, "mismatches": 0, "visible_cells": 21}
  }],
  "index_audit": {"full_reading_table_scan": false,
                  "confirmed_readings_total": 1,
                  "chunk_index_candidates": 1,
                  "verified_conflicts": 1}
}
```

## 30 秒演示路径

1. `docker compose up --build`，打开 <http://localhost:8000/docs>。
2. `POST /api/demo/seed` → Execute（Compose 已自动执行过；该接口幂等）。
3. `POST /api/boxes` → 请求体已预填拦截牌（或在 Examples 下拉选择
   **demo_blocked_by_occluded_reading**）→ Execute → **409**，
   返回冲突箱号 `BOX-DEMO-01` 与相关读数 `RD-0001`。
4. `POST /api/scans` → 示例 **occluded_corners** → Execute：
   唯一命中 `BOX-DEMO-01`，确认编号 `RD-0001`。
5. 再 Execute 一次：`reused=true`、`pinned=true`，仍是 `RD-0001`，
   候选只有原箱。
6. 换 **valid_new_tag** 示例挂牌 → **201**（差异足够大，不冲突）。

也可以在宿主机跑无依赖脚本（Python 标准库）：

```bash
python3 scripts/walkthrough.py http://localhost:8000
python3 scripts/race_check.py  http://localhost:8000 16   # 同时挂牌+扫码
```

## 规则如何落地

| 需求 | 实现 |
| --- | --- |
| 5×5 黑白码登记 | `POST /api/boxes`，单元格 `0/1`；完整码不允许遮挡 |
| 同尺寸格阵、最多 4 格看不清 | `POST /api/scans`；`null`（或 `?`/`x`/`-1`/`2`）表示看不清，>4 格返回 422 |
| 四旋转朝向 + 可见格至多错 1 格 | `app/codes.py::match_grid`，枚举 0/90/180/270°，只统计可见格误差 |
| 返回候选箱号 | 响应字段 `candidate_box_numbers`（同时给别名 `candidates`）与每个命中的 `orientation/mismatches` |
| 唯一箱号才确认并编号 | 仅当候选箱号集合大小为 1 时写 `readings`，编号 `RD-0001` 顺序生成 |
| 重扫同一格阵沿用编号 | `readings.grid_key`（25 字符，遮挡记 `?`）唯一；命中已有读数直接返回原编号，`reused=true` |
| 已确认读数始终只命中原箱 | 同格阵重扫走“钉住”记录，只返回其 `box_number`（`pinned=true`）；且冲突挂牌会被拒绝，库内不存在“读数同时命中别箱”的状态 |
| 新完整码命中旧箱 → 拒绝 | 挂牌前对牌册做同规则匹配，409 + `complete_code_matches_existing_box` |
| 任一已确认读数命中新牌 → 拒绝并给箱号/读数编号 | 分块索引召回 + 精确复核，409 + `confirmed_reading_matches_new_tag`，给 `conflicting_box_numbers` / `related_reading_ids` |
| 核查不能全扫已确认读数 | `reading_chunks` 索引表，6 次索引查询取候选，再按主键批量取回复核（见下） |
| 挂牌/扫码并发正确性 | 进程内 `RLock` 串行化检查+写入；每个操作单事务 `BEGIN IMMEDIATE`，SQLite 写锁兜底多进程；Compose 固定单 worker |

### “不能全扫已确认读数”的索引原理

25 格按行主序切成 **6 个互不相交的块**（5 块×4 格 + 1 块×5 格）。
一次命中最多有 `4 个遮挡格 + 1 个错误格 = 5` 个“坏格”。5 个坏格放进 6 个块，
由抽屉原理，**至少一个块完全干净**：该块在读数中全部可见且与新牌逐格相同。

因此每条确认读数写入时，把它 **4 个旋转朝向**下每个“全可见块”的精确比特签名
写入 `reading_chunks(chunk_index, bits, reading_id)`；挂牌时对新牌的 6 个块做
索引查询即可召回所有潜在冲突读数，再用完整匹配器排除假阳性。

- 召回保证：4000 组随机用例 + 对抗性布局测试验证“零漏报”；
- `EXPLAIN QUERY PLAN` 显示块查询走主键索引 `SEARCH ... USING PRIMARY KEY`；
- 挂牌链路不调用 `repo.list_readings`（有 monkeypatch 测试），
  取行只走 `WHERE id IN (...)` 主键批量取；
- 1200 条读数规模下，索引结果与测试内暴力全扫预言机完全一致。

## HTTP 接口

| 方法/路径 | 说明 |
| --- | --- |
| `POST /api/boxes` | 登记箱牌；可给 `box_number`，省略则分配 `BX-0001`；冲突 409 |
| `GET  /api/boxes` / `GET /api/boxes/{box_number}` | 牌册查询 |
| `POST /api/scans` | 扫码识别；返回候选、确认状态、读数编号 |
| `GET  /api/readings` / `GET /api/readings/{RD-0001或数字id}` | 确认读数 |
| `POST /api/demo/seed` / `GET /api/demo/scenario` | 演示数据与现场脚本 |
| `GET /health` | 健康检查 + 索引计数 |
| `GET /docs` | Swagger UI（静态资源已自托管，不依赖 CDN） |

兼容别名（不在 `/docs` 展示）：`/boxes`、`/register`、`/scans`、`/scan`、
`/recognize`、`/api/register`、`/api/recognize`。
字段别名：箱号接受 `box_number/boxNumber/box_no`，格阵接受
`grid/code/matrix/cells/pattern`。格阵除标准 5×5 数组外，也接受一维 25 格、
5 个字符串行等常见扫描器格式。

## 本地开发与测试

```bash
python3 -m pip install -r requirements-dev.txt
DB_PATH=data/exhibit.db SEED_DEMO=1 \
  python3 -m uvicorn app.main:app --reload --port 8000
python3 -m pytest          # 29 个用例：规则/API/索引/并发（含真实 HTTP 竞速）
```

SQLite 文件位置由 `DB_PATH` 决定（Compose 中为 `/data/exhibit.db` 命名卷）。

## 目录结构

```
app/
  codes.py        # 格阵解析、旋转、匹配、分块签名（纯逻辑）
  db.py           # SQLite schema、连接、BEGIN IMMEDIATE 事务
  repository.py   # 牌册/读数/块索引的数据访问
  services.py     # 挂牌、扫码、冲突核查、并发锁、编号钉住
  schemas.py      # pydantic 模型与 /docs 示例
  demo.py         # 遮挡拦截演示数据
  main.py         # FastAPI 路由、自托管 Swagger、OpenAPI 示例
  static/vendor/  # 本地 swagger-ui js/css
scripts/
  walkthrough.py  # 纯标准库端到端演示
  race_check.py   # 纯标准库并发挂牌/扫码校验
tests/            # 29 个 pytest 用例
Dockerfile docker-compose.yml
```

## 实测结果（本次收尾核对）

- `python3 -m pytest`：**29 passed**（含 4000 组随机索引完备性、
  1200 条读数暴力预言机比对、真实 uvicorn + 14 线程 HTTP 同时挂牌/扫码）。
- 真实 uvicorn 进程 + curl：seed → 拦截牌 **409**
  `['BOX-DEMO-01'] / ['RD-0001']`；重扫同格阵 **200** 且
  `reused=pinned=True`、编号不变；差异新牌 **201**。
- `scripts/walkthrough.py`：全部断言通过。
- `scripts/race_check.py`（16 线程同时到达）：8 次挂牌全部 409、
  8 次扫码全部确认到同一 `RD-0002` 且只命中原箱；另跑 30 轮进程内竞速，
  全局“读数只命中钉住箱”违例 **0**；两种合法串行顺序
  （扫码先→挂牌 409；挂牌先→扫码歧义不确认）均单独验证。
- `/docs` 200，自托管 JS/CSS 200；OpenAPI 含 3 个挂牌示例与 3 个扫码示例。

## 已知限制 / 待完成事项

- 本构建沙箱**没有安装 Docker/Podman 守护进程**，因此 `docker compose up
  --build` 未能在此实际执行；`docker-compose.yml` 已通过 YAML 校验，
  `requirements.txt` 的锁定版本已在干净 target 目录解析安装成功，
  应用本身已用等价的本地 uvicorn 进程实测。需在有 Docker 的环境补一次
  镜像构建/Compose 启动确认。
- 为保证“检查+写入”串行，Compose 固定 **1 个 uvicorn worker**；多 worker
  部署下正确性由 `BEGIN IMMEDIATE` 事务兜底，但进程内锁不再跨进程，
  高并发时可能出现 `database is locked`（当前 busy_timeout=15s）。
- 无鉴权/多租户/审计日志；读数列表默认返回最近 200 条
  （`?limit=` 可调，上限 10000）。
- 只实现了需求中的四旋转，不包含镜像翻转。
