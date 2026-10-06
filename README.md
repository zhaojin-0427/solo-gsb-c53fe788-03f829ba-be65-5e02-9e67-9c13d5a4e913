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
`feasible=false`；非法输入返回 **422**；搜索触及内部状态安全阀、**无法证明
可行或无解**时返回 **503 + `SEARCH_LIMIT_REACHED`**（这不代表无解，区别于
已证无解）。

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
- 搜索资源耗尽、未能证明可行或无解（HTTP 503）：

```json
{
  "detail": [
    {
      "code": "SEARCH_LIMIT_REACHED",
      "message": "搜索触及内部状态上限，未能在资源限额内证明可行或无解；该结果不代表无解。"
    }
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

### 2) 十二段轮转：容量 13，单个空位，13 步全序最优

区段 `0..11` 均长度 1，依次位于块 `0..11`，目标为 `(i+1) mod 12`（区段 11
回到块 0），块 `12` 空闲。每段的目标都被下一段占着（环依赖），必须先把一段
借到空块 `12` 才能链式归位。最优写入量 = 12（各归位一次）+ 1（借位的一次
额外复制）= **13 块、13 步**：

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{
    "capacity": 13,
    "segments": [
      {"id": "0",  "current_start": 0,  "length": 1, "target_start": 1},
      {"id": "1",  "current_start": 1,  "length": 1, "target_start": 2},
      {"id": "2",  "current_start": 2,  "length": 1, "target_start": 3},
      {"id": "3",  "current_start": 3,  "length": 1, "target_start": 4},
      {"id": "4",  "current_start": 4,  "length": 1, "target_start": 5},
      {"id": "5",  "current_start": 5,  "length": 1, "target_start": 6},
      {"id": "6",  "current_start": 6,  "length": 1, "target_start": 7},
      {"id": "7",  "current_start": 7,  "length": 1, "target_start": 8},
      {"id": "8",  "current_start": 8,  "length": 1, "target_start": 9},
      {"id": "9",  "current_start": 9,  "length": 1, "target_start": 10},
      {"id": "10", "current_start": 10, "length": 1, "target_start": 11},
      {"id": "11", "current_start": 11, "length": 1, "target_start": 0}
    ]
  }'
```

```json
{"feasible":true,"moves":[{"segment_id":"0","destination_start":12,"length":1},{"segment_id":"11","destination_start":0,"length":1},{"segment_id":"10","destination_start":11,"length":1},{"segment_id":"9","destination_start":10,"length":1},{"segment_id":"8","destination_start":9,"length":1},{"segment_id":"7","destination_start":8,"length":1},{"segment_id":"6","destination_start":7,"length":1},{"segment_id":"5","destination_start":6,"length":1},{"segment_id":"4","destination_start":5,"length":1},{"segment_id":"3","destination_start":4,"length":1},{"segment_id":"2","destination_start":3,"length":1},{"segment_id":"1","destination_start":2,"length":1},{"segment_id":"0","destination_start":1,"length":1}]}
```

重放过程：

1. `0: [0,1) → [12,13)`（借临时空位并释放块 0）；
2. `11 → 0`、`10 → 11`、…、`1 → 2`（沿环反向链式归位，共 11 步）；
3. `0: [12,13) → [1,2)`（借位段归位）。

### 3) 已全部到位 → 空序列

```bash
curl -s -X POST http://localhost:8000/plans \
  -H 'Content-Type: application/json' \
  -d '{"capacity":4,"segments":[{"id":"A","current_start":0,"length":1,"target_start":0}]}'
```

```json
{"feasible":true,"moves":[]}
```

### 4) 无空闲、两段互换 → 无解

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

### 5) 带保留区的合法搬移

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

### 6) 目标碰保留区 → 422

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

### 7) 64 位超大坐标（不逐块枚举）

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
  1. **f 值排序的可重开 A\***，代价 `(总写入块数, 步数)`，堆键为
     `g + h = (已写入 + 写入下界, 已走步数 + 步数下界)`。同一状态的不同前缀
     只保留字典序最小的代价对：代价只取决于状态，更差前缀接同样后缀不可能
     更优。终点第一次被弹出即最优值 `(W*, K*)`；堆耗尽仍未到终点才是
     **已证无解**。
  2. 在预算 `(W*, K*)` 内做按 `(区段 ID, 目的起点)` 展开的有界 DFS，第一个
     完整方案即字典序最小的最优序列。同状态保留一个**帕累托标签集**：仅当
     早先进入该状态的某条前缀在写入、步数两分量上都不更大（弱支配）时才
     剪掉当前前缀——**代价相同的不同前缀不互相剪**，否则会丢掉字典序更小
     的完整序列。
- **安全下界**（分量可接纳，过度估计才会漏最优解）：
  1. 每个未归位区段至少还要整段写入一次（写其全长、占一步）；
  2. 依赖图（`u` 的目标与 `v` 当前占用相交则有边 `u→v`）中每个**含环 SCC**
     至少要把环内一段先复制到非目标的临时位置（额外写入 ≥ 环内最短长度、
     ≥ 1 步；SCC 互不相交，代价相加）。
  若未归位区段诱导的依赖图无环、且没有目标被已归位区段或保留区永久挡住，
  则下界还是**可达的精确剩余代价**（总有目标完全空闲的区段可一步归位、
  逐轮剥离），A\* 无需展开这些状态即可定出经它们到终点的最优代价。
- **资源语义**：搜索状态数超过安全阀（默认 50 万）时抛
  `SEARCH_LIMIT_REACHED`，HTTP 返回 **503**——它既不冒充可行也不冒充已证
  无解；只有堆耗尽（或廉价的可行性预检失败）才返回 `feasible=false`。
- `tests/test_fuzz.py` 在数千个小规模随机实例上，将本规划器与
  **逐块枚举的暴力最优解**逐一比对（代价与字典序完全一致），并对返回序列做
  **逐块物理重放**校验（目的必须整块空闲、源目不重叠、复制后释放、最终到位）。

## 运行测试

```bash
python3 -m pip install pytest httpx
PYTHONPATH=. python3 -m pytest tests/test_planner.py tests/test_http.py -q
PYTHONPATH=. python3 tests/test_fuzz.py    # 与逐块暴力的差分测试（较慢）
```
