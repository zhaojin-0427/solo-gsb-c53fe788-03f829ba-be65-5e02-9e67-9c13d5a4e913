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
`feasible=false`；非法输入返回 **422**。

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
- 无可行方案：`{"feasible": false, "moves": []}`。

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
`RESERVED_OUT_OF_BOUNDS`。

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

---

## 算法与正确性

- **状态**只记录每个区段的当前起点（一个长度 `n` 的元组）。
- **空闲洞**由 `[0, capacity)` 与「当前占用 ∪ 保留区」做区间求差得到，
  全程是区间的排序/合并，复杂度 `O(n log n)`，与容量无关。
- 每个自由洞只需考察少量候选目的起点：洞的左右缘、区段自己的目标起点、
  以及与目标相切的 `target±length`；任何其它内部摆放都可在不增加写入与
  步数的情况下滑动到这些点，因此**不漏最优解**。
- 搜索分两阶段：
  1. 轻量 **Dijkstra**，代价 `(总写入块数, 步数)`，先求出最优值
     `(W*, K*)`（用强连通分量下界与已归位增量做剪枝）；
  2. 在预算 `(W*, K*)` 内做按 `(区段 ID, 目的起点)` 展开的有界 DFS，
     第一个完整方案即字典序最小的最优序列。
- `tests/test_fuzz.py` 在数千个小规模随机实例上，将本规划器与
  **逐块枚举的暴力最优解**逐一比对（代价与字典序完全一致），并对返回序列做
  **逐块物理重放**校验（目的必须整块空闲、源目不重叠、复制后释放、最终到位）。

## 运行测试

```bash
python3 -m pip install pytest httpx
PYTHONPATH=. python3 -m pytest tests/test_planner.py tests/test_http.py -q
PYTHONPATH=. python3 tests/test_fuzz.py    # 与逐块暴力的差分测试（较慢）
```
