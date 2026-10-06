"""
卷区段安全搬移规划器（区间级，不按容量逐块枚举）。

规则
----
* 每次动作把一个完整区段从源区间复制到一个当时等长的连续空闲区间；
* 源、目的不得重叠；复制完成后才释放源，因此目的区间必须当前完全空闲；
* 不允许拆分、覆盖尚未迁出的数据；
* 可借助临时空位（临时目的可位于另一区段的最终目标上）。

优化目标（字典序）
----------------
1. 总写入块数  sum(每步长度) 最少；
2. 动作步数最少；
3. (区段 ID, 目的起点) 动作序列字典序最小。

搜索（同一状态的不同前缀怎样归并）
----------------------------------
状态只记录每个区段当前起点；空闲区间由「当前占用 ∪ 保留区」在容量上求差
得到，时间/空间只与区段数有关，与容量（最多 2^64）无关。

* **阶段 A：f 值排序的可重开 A\***（代价为字典序对 (写入, 步数)）。
  堆键 = g + h = (已写入 + 写入下界, 已走步 + 步数下界)。同一状态只保留
  字典序最小的代价对 (w,k)：代价只取决于状态，任何更差前缀接续同样后缀
  都不可能更优。h 分量可接纳，故终点第一次被弹出时 (W*,K*) 即最优值；
  堆耗尽仍未到终点才是**已证无解**；中途越过状态安全阀则抛
  SearchLimitReached（不允许当成无解）。
* **阶段 B：预算 (W*,K*) 内按 (区段 ID, 目的起点) 展开的有界 DFS。**
  同一状态保留一个**帕累托标签集**：仅当先前进入该状态的某条前缀在两个
  分量上都不更大（弱支配）时才剪掉当前前缀——代价相同的不同前缀不能在
  阶段 B 互相剪，因为更便宜/更早的前缀所允许的后缀集合是超集，DFS 先展开
  的子树已穷尽所有可行后缀。第一个抵达终点的完整方案就是字典序最小的
  最优全序序列。

安全下界（分量可接纳，过度估计才会漏解）
----------------------------------------
对任意剩余规划：
1. 每个未归位区段至少还要整段写入一次 → 写入 ≥ Σ其长度，步数 ≥ 其个数；
2. 依赖图（u 的目标与 v 当前占用相交则 u→v）中每个含环 SCC，必须先把其中
   一个区段复制到**非目标**的临时位置（环内谁都无法第一步直接归位）：
   额外写入 ≥ 环内最短长度、额外步数 ≥ 1。SCC 互不相交，代价可相加。
   下界第三项是「精确标志」：含环 SCC 数为 0 且无目标被保留区占据时，
   依赖图是 DAG，源点可直接归位、逐轮剥离，下界即为可达的精确剩余代价，
   A* 不必展开这些状态即可定出经它们到终点的最优代价。

每个自由洞只考察洞左缘、洞右缘、区段目标起点及 target±length 等少数候选
（其它严格内部摆放可在同洞内无代价滑动到这些点），再排除纯嵌套临时摆放。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Optional


UINT64_MAX = (1 << 64) - 1


class SearchLimitReached(Exception):
    """搜索状态数超过安全阀（理论上正常规模不会触发）。"""


# --------------------------------------------------------------------------- #
# 区间工具
# --------------------------------------------------------------------------- #

def _merge_disjoint(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """排序合并互不重叠区间；相邻的 [a,b) 与 [b,c) 合并。零长度区间忽略。"""
    positive = [(lo, hi) for lo, hi in intervals if hi > lo]
    if not positive:
        return []
    ordered = sorted(positive)
    merged: list[list[int]] = [list(ordered[0])]
    for lo, hi in ordered[1:]:
        if hi <= merged[-1][1]:
            continue  # 完全落在尾段内
        if lo <= merged[-1][1]:  # 输入合法时只会出现相邻
            merged[-1][1] = hi
        else:
            merged.append([lo, hi])
    return [(a, b) for a, b in merged]


def compute_free_holes(
    positions: tuple[int, ...],
    lengths: tuple[int, ...],
    reserved: tuple[tuple[int, int], ...],
    capacity: int,
) -> list[tuple[int, int]]:
    """当前空闲区间：[0, capacity) 减去所有占用（保留区中的零长度区间不占块）。"""
    occupied = [(lo, hi) for lo, hi in reserved if hi > lo]
    for start, length in zip(positions, lengths):
        occupied.append((start, start + length))
    merged = _merge_disjoint(occupied)

    holes: list[tuple[int, int]] = []
    cursor = 0
    for lo, hi in merged:
        if lo > cursor:
            holes.append((cursor, lo))
        cursor = max(cursor, hi)
    if cursor < capacity:
        holes.append((cursor, capacity))
    return holes


# --------------------------------------------------------------------------- #
# 动作生成
# --------------------------------------------------------------------------- #

def _candidate_starts(
    hole_lo: int,
    hole_hi: int,
    length: int,
    target_start: Optional[int] = None,
) -> list[int]:
    """
    单个可容纳 length 的洞中需要考察的候选目的起点：

    * 洞左缘、洞右缘；
    * 区段最终目标起点（若在洞内）；
    * 与目标相切的两个位置 target_start ± length（若在洞内）：
      先停在与目标相邻处，释放源后新洞正好与目标洞打通，下一段即可归位。

    任何其它严格内部摆放都可在不增加写入/步数的前提下，在这些点之间滑动；
    完备性经与逐起点枚举暴力对照验证。
    """
    if hole_hi - hole_lo < length:
        return []
    result = {hole_lo, hole_hi - length}
    if target_start is not None:
        for cand in (
            target_start - length,
            target_start,
            target_start + length,
        ):
            if hole_lo <= cand <= hole_hi - length:
                result.add(cand)
    return list(result)


def _legal_actions(
    positions: tuple[int, ...],
    lengths: tuple[int, ...],
    targets: tuple[int, ...],
    reserved: tuple[tuple[int, int], ...],
    capacity: int,
) -> list[tuple[int, int]]:
    """
    全部应纳入搜索的合法动作 (seg_index, dest_start)，按
    (seg_index, dest_start) 去重排序。已在目标上的区段不动。
    """
    holes = compute_free_holes(positions, lengths, reserved, capacity)
    actions: set[tuple[int, int]] = set()

    for i, (src, length, tgt) in enumerate(zip(positions, lengths, targets)):
        if src == tgt:
            continue
        src_end = src + length
        for lo, hi in holes:
            for dest in _candidate_starts(lo, hi, length, tgt):
                dest_end = dest + length
                if dest == src:
                    continue
                # 源/目的重叠则不可（复制完成才释放源）。
                if dest < src_end and src < dest_end:
                    continue
                # 纯嵌套临时摆放剪枝：目的洞整体罩住另一个未归位区段的源，
                # 则先搬内层不劣（少一次让/占，字典序也不更差）。
                if dest != tgt:
                    nested = False
                    for j, (pj, lj) in enumerate(zip(positions, lengths)):
                        if j == i or positions[j] == targets[j]:
                            continue
                        if dest <= pj and pj + lj <= dest_end:
                            nested = True
                            break
                    if nested:
                        continue
                actions.add((i, dest))

    return sorted(actions)


def _apply(positions: tuple[int, ...], seg: int, dest: int) -> tuple[int, ...]:
    new_pos = list(positions)
    new_pos[seg] = dest
    return tuple(new_pos)


# --------------------------------------------------------------------------- #
# 下界：依赖图强连通分量（每个含环 SCC 至少一次额外整段复制）
# --------------------------------------------------------------------------- #

def _scc_cycle_bound(
    positions: tuple[int, ...],
    lengths: tuple[int, ...],
    targets: tuple[int, ...],
) -> tuple[int, int]:
    """
    「打破全部依赖环」的额外代价下界 (额外写入块数, 额外步数)。

    建依赖图：u -> v 表示 u 的最终目标与 v 当前占用相交（u 归位前 v 必须
    先让出）。含自环或规模 > 1 的 SCC 是死锁环；每个这样的 SCC 至少要先把其中
    一个完整区段复制到非目标的临时位置（一次长度 >= 环内最短长度的额外写入、
    一次额外步数）。不同 SCC 的区段集合互不相交，代价可直接相加。
    """
    n = len(positions)
    succ: list[list[int]] = [[] for _ in range(n)]

    for u in range(n):
        if positions[u] == targets[u]:
            continue  # 已归位，不再参与依赖
        tlo, thi = targets[u], targets[u] + lengths[u]
        for v in range(n):
            if v == u:
                continue
            vlo, vhi = positions[v], positions[v] + lengths[v]
            if tlo < vhi and vlo < thi:
                succ[u].append(v)
        # 自环：目标与自身当前位置部分相交（必须先整体让出）。
        slo, shi = positions[u], positions[u] + lengths[u]
        if tlo < shi and slo < thi:
            succ[u].append(u)

    # Tarjan SCC（迭代版，避免区段很多时递归栈过深）。
    index_of = [-1] * n
    lowlink = [0] * n
    on_stack = [False] * n
    stack: list[int] = []
    counter = 0
    sccs: list[list[int]] = []

    def strongconnect(root: int) -> None:
        nonlocal counter
        work: list[tuple[int, int]] = [(root, 0)]
        index_of[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True

        while work:
            v, edge_idx = work[-1]
            if edge_idx < len(succ[v]):
                w = succ[v][edge_idx]
                work[-1] = (v, edge_idx + 1)
                if index_of[w] == -1:
                    index_of[w] = lowlink[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack[w] = True
                    work.append((w, 0))
                elif on_stack[w]:
                    lowlink[v] = min(lowlink[v], index_of[w])
            else:
                if lowlink[v] == index_of[v]:
                    comp: list[int] = []
                    while True:
                        w = stack.pop()
                        on_stack[w] = False
                        comp.append(w)
                        if w == v:
                            break
                    sccs.append(comp)
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[v])

    for v in range(n):
        if index_of[v] == -1:
            strongconnect(v)

    extra_write = 0
    extra_steps = 0
    for comp in sccs:
        cyclic = len(comp) > 1 or (len(comp) == 1 and comp[0] in succ[comp[0]])
        if cyclic:
            extra_write += min(lengths[v] for v in comp)
            extra_steps += 1
    return extra_write, extra_steps


def _lower_bound(
    positions: tuple[int, ...],
    lengths: tuple[int, ...],
    targets: tuple[int, ...],
    reserved: tuple[tuple[int, int], ...] = (),
) -> tuple[int, int, int]:
    """
    剩余代价 (写入块数, 步数, 精确标志) 的分量可接纳下界。

    * 每个未归位区段至少还要整段写入一次（写其全长）、贡献一步；
    * 再叠加打破每个依赖环 SCC 所需的额外一次整段复制（第三项 = 含环 SCC 数）。

    前两个分量分别可接纳。仅当第三项为 1 时下界是**可达的精确剩余代价**，
    需同时满足：

    1. 未归位区段诱导的依赖图无含环 SCC（环必须借临时位置、各付一次额外
       复制才能打破）；
    2. 没有未归位区段的目标与**已归位**区段相交。已归位区段在「各段只写
       一次」的下界方案中不能再动（再动就要额外写两次），它挡在目标上意味着
       下界不可达（甚至可能无解）；
    3. 没有未归位区段的目标落在保留区（豁免仅针对同段 current/target 相交
       的特例：合法输入中目标不与保留区相交）。

    三条都满足时，依赖图必有可剥离的汇点（目标完全空闲，可一步归位），
    逐轮剥离即可全部一步到位。下界本身在任何情况下都保持可接纳。
    """
    unsettled_write = 0
    unsettled_count = 0
    exact = 1
    settled_intervals = [
        (p, p + l)
        for p, l, t in zip(positions, lengths, targets)
        if p == t
    ]
    for pos, length, tgt in zip(positions, lengths, targets):
        if pos == tgt:
            continue
        unsettled_write += length
        unsettled_count += 1
        tlo, thi = tgt, tgt + length
        # 条件 3：目标被保留区永久占据。
        for r_lo, r_hi in reserved:
            if r_hi > r_lo and tlo < r_hi and r_lo < thi:
                exact = 0
        # 条件 2：目标与某个已归位区段相交（它不会在下界方案中再让出）。
        for slo, shi in settled_intervals:
            if tlo < shi and slo < thi:
                exact = 0
                break
    extra_write, extra_steps = _scc_cycle_bound(positions, lengths, targets)
    if extra_steps:  # 条件 1
        exact = 0
    return unsettled_write + extra_write, unsettled_count + extra_steps, exact


# --------------------------------------------------------------------------- #
# 规划器
# --------------------------------------------------------------------------- #

# 搜索安全阀：极端病态（如可用空位极少、区段极多）时限状态数，避免无界计算。
_MAX_SEARCH_STATES = 500_000


@dataclass(frozen=True)
class Segment:
    id: str
    current_start: int
    length: int
    target_start: int


def plan_moves(
    capacity: int,
    segments: list[Segment],
    reserved: list[tuple[int, int]],
    max_states: int = _MAX_SEARCH_STATES,
) -> Optional[list[tuple[str, int]]]:
    """
    返回 (segment_id, dest_start) 形式的逐步搬移序列；已证无解返回 None；
    搜索耗尽安全阀（未能证明最优/无解）时抛 SearchLimitReached。

    已全部到位时返回 []。
    """
    ids = [s.id for s in segments]
    lengths = tuple(s.length for s in segments)
    targets = tuple(s.target_start for s in segments)
    reserved_t = tuple(reserved)
    start = tuple(s.current_start for s in segments)
    goal = targets

    if start == goal:
        return []

    # 快速不可行判定 1：任何时刻连续空闲块数 <= 总空闲块数。未归位区段若比
    # 总空闲还长，就永远凑不出等长的目的区间，已证无解。
    total_free = capacity - sum(lengths) - sum(
        hi - lo for lo, hi in reserved if hi > lo
    )
    for i, length in enumerate(lengths):
        if start[i] != targets[i] and length > total_free:
            return None

    # 快速不可行判定 2：目标布局本身不可达（HTTP 层通常已以 422 拒绝，此处
    # 兜底，保证直接调用规划器也能快速判无解，而不是耗光搜索预算）。
    # * 两个不同区段的目标重叠 ⇒ 终点不可能（同一块不能同时放两段）；
    # * 目标与保留区相交 ⇒ 终点被永久占据。
    def _ov(a_lo: int, a_hi: int, b_lo: int, b_hi: int) -> bool:
        return a_lo < b_hi and b_lo < a_hi

    for i in range(len(lengths)):
        tlo_i, thi_i = targets[i], targets[i] + lengths[i]
        for r_lo, r_hi in reserved_t:
            if r_hi > r_lo and _ov(tlo_i, thi_i, r_lo, r_hi):
                return None
        for j in range(i + 1, len(lengths)):
            if _ov(tlo_i, thi_i, targets[j], targets[j] + lengths[j]):
                return None

    def legal_action_list(pos: tuple[int, ...]) -> list[tuple[int, int]]:
        return _legal_actions(pos, lengths, targets, reserved_t, capacity)

    h_cache: dict[tuple[int, ...], tuple[int, int, int]] = {}

    def lower_bound(pos: tuple[int, ...]) -> tuple[int, int, int]:
        bound = h_cache.get(pos)
        if bound is None:
            bound = _lower_bound(pos, lengths, targets, reserved_t)
            if len(h_cache) < max_states:
                # 与 g 一样受安全阀约束；缓存未命中只损失速度，不影响正确性。
                h_cache[pos] = bound
        return bound

    def guard(n_states: int) -> None:
        if n_states >= max_states:
            raise SearchLimitReached

    # ================================================================== #
    # 阶段 A：f 值排序的可重开 A*
    #
    # 代价对 (write, steps) 是字典序全序；h=(h_w,h_k) 分量可接纳，故堆键
    # f=(write+h_w, steps+h_k) 下 goal 第一次被弹出即为最优值 (W*,K*)。
    # 同一状态只保留字典序最小的代价对：更差前缀接任意同样后缀都不会更优。
    #
    # 精确剪枝：下界第三项为 1（依赖图是 DAG 且无目标被保留区占据）时，
    # 下界即可达的精确剩余代价（源点可直接归位、逐轮剥离），无需展开该
    # 状态即可定出经它到终点的最优代价；A* 按 f 序弹出，取其中最小者即
    # 为全局最优。
    # ================================================================== #
    g: dict[tuple[int, ...], tuple[int, int]] = {start: (0, 0)}
    hw0, hk0, exact0 = lower_bound(start)
    heap: list = [(hw0, hk0, 0, start, 0, 0)]
    serial = 0
    optimal: Optional[tuple[int, int]] = None
    best_exact: Optional[tuple[int, int]] = None
    if exact0:
        best_exact = (hw0, hk0)

    try:
        while heap:
            fw, fk, _, pos, write, steps = heapq.heappop(heap)
            if g.get(pos) != (write, steps):
                continue  # 已被更优标签取代的陈旧堆项
            if pos == goal:
                optimal = (write, steps)
                break
            if best_exact is not None and (fw, fk) >= best_exact:
                # 之后所有弹出项的 f 都不会更小；精确候选已是全局最优。
                optimal = best_exact
                break

            h_w, h_k, exact = lower_bound(pos)
            if exact:
                cand = (write + h_w, steps + h_k)
                if best_exact is None or cand < best_exact:
                    best_exact = cand
                # 下界精确 ⇒ 子节点的 f 不会小于当前 f；无需再展开。
                continue

            for seg_idx, dest in legal_action_list(pos):
                nw = write + lengths[seg_idx]
                nk = steps + 1
                npos = _apply(pos, seg_idx, dest)
                prev = g.get(npos)
                # 同状态不同前缀归并：保留字典序最小的代价对。
                if prev is not None and (nw, nk) >= prev:
                    continue
                guard(len(g))
                nh_w, nh_k, n_exact = lower_bound(npos)
                if n_exact:
                    cand = (nw + nh_w, nk + nh_k)
                    if best_exact is None or cand < best_exact:
                        best_exact = cand
                g[npos] = (nw, nk)
                serial += 1
                heapq.heappush(
                    heap, (nw + nh_w, nk + nh_k, serial, npos, nw, nk)
                )
    except SearchLimitReached:
        raise

    if optimal is None and best_exact is not None:
        optimal = best_exact

    if optimal is None:
        # 堆耗尽仍未到终点：在候选动作完备的前提下，这是已证无解。
        return None

    w_star, k_star = optimal

    # ================================================================== #
    # 阶段 B：预算 (w_star, k_star) 内按动作字典序展开的有界 DFS。
    # 第一个抵达 goal 的完整方案，即 (区段 ID, 目的起点) 序列字典序最小的
    # 最优解。
    #
    # 同状态的不同前缀用帕累托标签集归并：labels[pos] 保存此前进入该状态、
    # 且两两互不支配的 (write, steps)。只有当存在旧标签在两个分量上都不
    # 更大（弱支配）时才剪当前前缀——代价相同的不同前缀不能互相剪，否则会
    # 丢掉字典序更小的完整序列。
    # ================================================================== #
    labels: dict[tuple[int, ...], list[tuple[int, int]]] = {}

    def dominated(pos: tuple[int, ...], cost: tuple[int, int]) -> bool:
        for w0, k0 in labels.get(pos, ()):  # type: ignore[arg-type]
            if w0 <= cost[0] and k0 <= cost[1]:
                return True
        return False

    def remember(pos: tuple[int, ...], cost: tuple[int, int]) -> None:
        kept = [
            (w0, k0)
            for w0, k0 in labels.get(pos, ())  # type: ignore[arg-type]
            if not (cost[0] <= w0 and cost[1] <= k0)
        ]
        kept.append(cost)
        labels[pos] = kept

    def ordered(pos: tuple[int, ...]) -> list[tuple[str, int, int]]:
        return sorted(
            ((ids[i], dest, i) for i, dest in legal_action_list(pos)),
            key=lambda t: (t[0], t[1]),
        )

    # 帧: [pos, spent_w, spent_k, actions, next_idx, chosen]
    frames: list[list] = [[start, 0, 0, ordered(start), 0, None]]
    trace: list[tuple[str, int]] = []
    visited = 1

    try:
        while frames:
            pos, spent_w, spent_k, acts, idx, chosen = frames[-1]

            # 首次进入该帧：登记前缀、检查归并、判定终点。
            if idx == 0 and chosen is not None:
                trace.append((chosen[0], chosen[1]))
                cur = (spent_w, spent_k)
                if dominated(pos, cur):
                    frames.pop()
                    trace.pop()
                    continue
                remember(pos, cur)
                if pos == goal:
                    # 下界与预算保证此时代价必为 (w_star, k_star)。
                    return list(trace)

            if idx >= len(acts):
                frames.pop()
                if chosen is not None:
                    trace.pop()
                continue

            seg_id, dest, seg_idx = frames[-1][3][idx]
            frames[-1][4] = idx + 1

            nw = spent_w + lengths[seg_idx]
            nk = spent_k + 1
            if nw > w_star or nk > k_star:
                continue
            npos = _apply(pos, seg_idx, dest)

            # 分量可接纳下界硬过滤：剩余代价小于下界的前缀不可能在预算内成解。
            h_w, h_k, _ = lower_bound(npos)
            if nw + h_w > w_star or nk + h_k > k_star:
                continue
            # 同状态标签归并：被旧前缀弱支配则不必再展开。
            if dominated(npos, (nw, nk)):
                continue

            visited += 1
            guard(visited)
            frames.append(
                [npos, nw, nk, ordered(npos), 0, (seg_id, dest)]
            )
    except SearchLimitReached:
        raise

    # 阶段 B 理论上不应失败（阶段 A 已证明存在 (w_star,k_star) 路径）；
    # 若真发生，按“未能证明”处理而非误报无解。
    raise SearchLimitReached
