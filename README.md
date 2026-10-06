# 卷区段安全搬移规划服务（Volume Segment Move Planner）

基于 **Python + FastAPI** 的 HTTP 服务。给定卷容量、若干区段（extent）的
当前位置 / 长度 / 目标位置以及保留区，规划出一个**可逐步重放**的安全搬移序列。

- 每一步只把**完整区段**复制到一个**当时等长的连续空闲区间**；
- 源、目的不得重叠；**复制完成后才释放旧址**，因此绝不可能覆盖尚未迁出的数据；
- 目标互相占位时允许先借**临时空位**绕场，但**不拆分**区段。

优化目标（按优先级，全部满足后再比较下一项）：

1. **总写入块数** `Σ(每步区段长度)` 最少；
2. **动作步数**最少；
3. 仍同分时，动作序列按 `(区段 ID, 目的起点)` 的**字典序最小**。

已经全部在目标位置时返回**空序列**；不存在可行方案时返回
`feasible=false`；非法输入返回 **422**；搜索超出安全状态上限（极端病态
实例，既未证最优也未证无解）返回 **503** 与错误码 `SEARCH_LIMIT_REACHED`。

> 坐标为非负 64 位整数（容量最大 `2^64`，合法块坐标 `0 … 2^64-1`）。
> 规划在**区间**层面进行，**不按卷容量逐块枚举**：复杂度只与区段数量有关，
> 与容量无关，因此 `2^64` 规模的坐标与小容量一样快。

---

## 目录结构

```
app/
  planner.py    # 区间级规划器（空闲洞求差、动作生成、Dijkstra + 字典序恢复）
  schemas.py    # Pydantic 请求/响应模型与跨字段语义校验
  main.py       # FastAPI 应用、POST /plans、422 错误封装
tests/          # 单元、HTTP 端到端、以及对逐块暴力的差分测试
Dockerfile
docker-compose.yml
requirements.txt
```

## 一键启动（Docker Compose）

需要本机装有 Docker 与 Docker Compose（v2）。

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`。可访问：

- 健康检查：`GET http://localhost:8000/health`
- 交互式 API 文档（Swagger UI）：`http://localhost:8000/docs`
- OpenAPI 描述：`http://localhost:8000/openapi.json`

后台运行 / 停止：

```bash
docker compose up --build -d
docker compose down
```

## 本地直接运行（可选，无需 Docker）

```bash
python3 -m pip install -r requirements.txt
PYTHONPATH=. uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 接口：`POST /plans`

### 请求体

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `capacity` | integer | 卷容量，`0 … 2^64` |
| `segments` | array | 区段列表，ID 必须唯一且非空 |
| `segments[].id` | string | 区段 ID |
| `segments[].current_start` | integer | 当前起点，`0 … 2^64-1` |
| `segments[].length` | integer | 区段长度，**必须为正** |
| `segments[].target_start` | integer | 目标起点，`0 … 2^64-1` |
| `reserved` | array | 保留区列表（可选，默认空）；允许长度为 0（不占块） |
| `reserved[].start` / `reserved[].length` | integer | 保留区起点 / 长度 |

所有区间均按半开区间 `[start, start+length)` 处理。

**合法性约束**（违反返回 422）：

- 区段长度为正；所有坐标非负且不超过 64 位范围，区间端点不超出卷容量；
- 各区段的**当前位置两两互不重叠**；
- 各区段的**目标位置两两互不重叠**；
- 当前位置与目标位置都**不越界**、**不碰保留区**（保留区两两也不重叠）。

> 注意：同一条区段的当前区间与目标区间允许部分相交（例如整体滑动/自环依赖），
> 服务会在必要时先把它整体搬到临时位置。

### 响应

成功（HTTP 200）：

```json
{
  "feasible": true,
  "moves": [
    {"segment_id": "B", "destination_start": 4, "length": 1},
    {"segment_id": "A", "destination_start": 2, "length": 2},
    {"segment_id": "B", "destination_start": 0, "length": 1}
  ]
}
```

- `moves` 即**按顺序重放**的搬移动作；第 `k` 步把 `segment_id` 复制到
  `[destination_start, destination_start+length)`。`length` 随每步回显，便于执行端核对。
- 已全部到位：`{"feasible": true, "moves": []}`。
- 无可行方案（已证明）：`{"feasible": false, "moves": []}`。

搜索超出安全状态上限（HTTP 503，与「已证无解」严格区分）：

```json
{
  "detail": [
    {"code": "SEARCH_LIMIT_REACHED", "message": "搜索状态数超出安全上限，未能在资源限额内给出已证最优的规划结果"}
  ]
}
```

非法输入（HTTP 422）：

```json
{
  "detail": [
    {"code": "SEGMENT_TOUCHES_RESERVED", "message": "区段 'A' 目标区间与保留区 #0 重叠"}
  ]
}
```

典型错误码：`INVALID_REQUEST`（类型/范围/缺字段/多余字段）、
`DUPLICATE_SEGMENT_ID`、`CURRENT_OVERLAP`、`TARGET_OVERLAP`、
`SEGMENT_TOUCHES_RESERVED`、`RESERVED_OVERLAP`、`SEGMENT_OUT_OF_BOUNDS`、
`RESERVED_OUT_OF_BOUNDS`、`SEARCH_LIMIT_REACHED`（503）。

---

## 实际请求 / 响应示例

以下输出均由本实现真实返回。

### 1) 题目样例：容量 5，互换并借临时空位

`A:[0,2)→[2,4)`，`B:[2,3)→[0,1)`。三步最优，且**必须先搬 B 到 `[4,5)`**
腾位（若先搬 A 会无处可去）。

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "capacity": 5,
    "segments": [
      {"id": "A", "current_start": 0, "length": 2, "target_start": 2},
      {"id": "B", "current_start": 2, "length": 1, "target_start": 0}
    ]
  }'
```

```json
{"feasible":true,"moves":[{"segment_id":"B","destination_start":4,"length":1},{"segment_id":"A","destination_start":2,"length":2},{"segment_id":"B","destination_start":0,"length":1}]}
```

重放过程：

1. `B: [2,3) → [4,5)`（借临时空位，复制完成后释放 `[2,3)`）；
2. `A: [0,2) → [2,4)`（此刻 `[2,4)` 完整空闲）；
3. `B: [4,5) → [0,1)`（归位）。

### 2) 已全部到位 → 空序列

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{"capacity":4,"segments":[{"id":"A","current_start":0,"length":1,"target_start":0}]}'
```

```json
{"feasible":true,"moves":[]}
```

### 3) 无空闲、两段互换 → 无解

容量被完全占满，没有任何临时空位，谁都无法先动：

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "capacity": 2,
    "segments": [
      {"id": "A", "current_start": 0, "length": 1, "target_start": 1},
      {"id": "B", "current_start": 1, "length": 1, "target_start": 0}
    ]
  }'
```

```json
{"feasible":false,"moves":[]}
```

### 4) 带保留区的合法搬移

保留 `[1,4)`，`A` 从 `[0,1)` 直接搬到 `[4,5)`：

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "capacity": 5,
    "reserved": [{"start":1,"length":3}],
    "segments": [{"id":"A","current_start":0,"length":1,"target_start":4}]
  }'
```

```json
{"feasible":true,"moves":[{"segment_id":"A","destination_start":4,"length":1}]}
```

### 5) 目标碰保留区 → 422

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "capacity": 4,
    "reserved": [{"start":2,"length":1}],
    "segments": [{"id":"A","current_start":0,"length":1,"target_start":2}]
  }'
```

```json
{"detail":[{"code":"SEGMENT_TOUCHES_RESERVED","message":"区段 'A' 目标区间与保留区 #0 重叠"}]}
```

### 6) 64 位超大坐标（不逐块枚举）

容量 `2^64`，把位于最后一个块的区段搬到第一个块：

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "capacity": 18446744073709551616,
    "segments": [
      {"id":"X","current_start":18446744073709551615,"length":1,"target_start":0}
    ]
  }'
```

```json
{"feasible":true,"moves":[{"segment_id":"X","destination_start":0,"length":1}]}
```

### 7) 12 段可搬轮转：借 1 个空位 13 步归位

容量 13，ID 为 `"0"…"11"` 的 12 个长度 1 区段位于同号位置，目标为
`(i+1)%12`，位置 12 空闲。这是一个 12 环 + 1 洞的死锁：必须先把一段
停进空位，再依次让其余各段归位，最后收尾——最优写入 = 步数 = 13
（恰等于「12 段各搬一次 + 破环额外一次」的下界）。字典序最小的首步是
停 `"0"`（字符串序 `"0"<"1"<"10"<"11"<"2"<…`）。

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "capacity": 13,
    "segments": [
      {"id":"0","current_start":0,"length":1,"target_start":1},
      {"id":"1","current_start":1,"length":1,"target_start":2},
      {"id":"2","current_start":2,"length":1,"target_start":3},
      {"id":"3","current_start":3,"length":1,"target_start":4},
      {"id":"4","current_start":4,"length":1,"target_start":5},
      {"id":"5","current_start":5,"length":1,"target_start":6},
      {"id":"6","current_start":6,"length":1,"target_start":7},
      {"id":"7","current_start":7,"length":1,"target_start":8},
      {"id":"8","current_start":8,"length":1,"target_start":9},
      {"id":"9","current_start":9,"length":1,"target_start":10},
      {"id":"10","current_start":10,"length":1,"target_start":11},
      {"id":"11","current_start":11,"length":1,"target_start":0}
    ]
  }'
```

```json
{"feasible":true,"moves":[{"segment_id":"0","destination_start":12,"length":1},{"segment_id":"11","destination_start":0,"length":1},{"segment_id":"10","destination_start":11,"length":1},{"segment_id":"9","destination_start":10,"length":1},{"segment_id":"8","destination_start":9,"length":1},{"segment_id":"7","destination_start":8,"length":1},{"segment_id":"6","destination_start":7,"length":1},{"segment_id":"5","destination_start":6,"length":1},{"segment_id":"4","destination_start":5,"length":1},{"segment_id":"3","destination_start":4,"length":1},{"segment_id":"2","destination_start":3,"length":1},{"segment_id":"1","destination_start":2,"length":1},{"segment_id":"0","destination_start":1,"length":1}]}
```

重放过程：`"0"` 先停到 `[12,13)` → 洞移到 0，`"11"` 归位到 0 → 洞移到
11，`"10"` 归位……依次到 `"1"` 归位到 2 → 洞移到 1，最后 `"0"` 从
`[12,13)` 归位到 1，共 13 步。

---

## 算法与正确性

- **状态**只记录每个区段的当前起点（一个长度 `n` 的元组）。
- **空闲洞**由 `[0, capacity)` 与「当前占用 ∪ 保留区」做区间求差得到，
  全程是区间的排序/合并，复杂度 `O(n log n)`，与容量无关。
- 每个自由洞只需考察少量候选目的起点：洞的左右缘、区段自己的目标起点、
  以及与目标相切的 `target±length`；任何其它内部摆放都可在不增加写入与
  步数的情况下滑动到这些点，因此**不漏最优解**。
- 搜索分两阶段，先证最优值、再恢复字典序最小序列：
  1. **A\*（允许重开）**：优先键 `f = g + h`，其中 `g = (已花写入, 已花步数)`，
     `h` 为可接纳下界（见下）。目标首次以其最优 `g` 弹出即得全序最优值
     `(W*, K*)`。同一状态只保留字典序最小的 `g`——状态相同则剩余最优
     代价相同，较差前缀被严格占优（**归并规则一**）。若按 `g` 弹出则会
     退化为按深度 BFS 排列空间（12 段环 + 1 洞的轮转即可耗尽状态上限），
     按 `f` 弹出后目标导向，该轮转仅需约千次状态评估。
  2. **预算内字典序 DFS**：在 `(W*, K*)` 内按 `(区段 ID, 目的起点)` 展开，
     每个状态记录到达代价的 Pareto 前沿；被前沿某项按分量 `≤` 支配
     （含相等）的新前缀直接剪掉——严格支配时它不可能在预算内完成，
     相等时可选后缀完全相同而先到前缀字典序更小（**归并规则二**）。
     第一个到达目标的完整方案即字典序最小的最优序列。
- **可安全剪枝的下界**（两阶段共用）：每个未归位区段至少再整体搬一次
  （写入 ≥ 未归位长度和、步数 ≥ 未归位段数）；依赖图（`u→v`：`u` 的目标
  与 `v` 当前占用相交）中每个含环强连通分量至少再额外临时复制一次环内
  最短区段。两组下界按分量相加仍可接纳，剪枝只丢「无论如何都超预算」的
  分支，绝不提前返回未证最优的路径。
- 搜索状态数设有安全阀（默认 50 万）；耗尽时抛 `SearchLimitReached`，
  接口返回 **503 / SEARCH_LIMIT_REACHED**，与「已证无解」的
  `feasible=false` 严格区分。
- `tests/test_fuzz.py` 在数千个小规模随机实例上，将本规划器与
  **逐块枚举的暴力最优解**逐一比对（代价与字典序完全一致），并对返回序列做
  **逐块物理重放**校验（目的必须整块空闲、源目不重叠、复制后释放、最终到位）。

## 运行测试

```bash
python3 -m pip install pytest httpx
PYTHONPATH=. python3 -m pytest tests/test_planner.py tests/test_http.py -q
PYTHONPATH=. python3 tests/test_fuzz.py    # 与逐块暴力的差分测试（较慢）
```
