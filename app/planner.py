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

完备性（为何不按块枚举也不漏最优解）
----------------------------------
状态只记录每个区段当前起点；空闲区间由「当前占用 ∪ 保留区」在容量上求差
得到，时间/空间只与区段数有关，与容量（最多 2^64）无关。

每个自由洞中只需考察三类候选目的起点：洞左缘、洞右缘、区段自己的最终
目标起点。任何其它严格内部摆放都能在不增加写入与步数的前提下滑向这三类
位置（同洞内部本来空闲，滑动不覆盖任何数据）。再排除「纯嵌套」临时
摆放（目的洞整体罩住另一个尚未归位区段的源）：先搬走内层永远不劣。

搜索为带路径字典序的 Dijkstra：代价键
``(写入块数, 步数, 动作序列字典序)`` 是全序，因此每个状态只需保留到达
它的最优键；环依赖处才产生分支，实际状态数很小。SCC 下界用于提前丢弃
超预算标签。
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

def _heuristic(
    positions: tuple[int, ...],
    lengths: tuple[int, ...],
    targets: tuple[int, ...],
) -> tuple[int, int]:
    """
    对「剩余额外代价 (额外写入块数, 额外步数)」的可接纳下界。

    建依赖图：u -> v 表示 u 的最终目标与 v 当前占用相交（u 归位前 v 必须
    先让出）。含自环或规模 > 1 的 SCC 是死锁环；每个这样的 SCC 至少要先把其中
    一个完整区段复制到临时位置（一次长度 >= 环内最短长度的额外写入、一次
    额外步数）。
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
) -> Optional[list[tuple[str, int]]]:
    """
    返回 (segment_id, dest_start) 形式的逐步搬移序列；无解返回 None。
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

    # 快速不可行判定：若存在某个未归位区段，其长度大于当前最大连续空闲洞，
    # 且它的移动（无论搬到哪）都不能腾出足够空间，则无解。保守起见这里只
    # 检查「卷内不存在任何长度 >= 该长度的连续空闲区，且保留区+其它区段
    # 的布局无法产生这样的洞」——等价条件是该长度超过总空闲块数。
    total_free = capacity - sum(lengths) - sum(
        hi - lo for lo, hi in reserved if hi > lo
    )
    for i, length in enumerate(lengths):
        if start[i] != targets[i] and length > total_free:
            # 任何时刻连续空闲块数 <= 总空闲块数，该长度的目的永远凑不出。
            return None

    def legal_action_list(pos: tuple[int, ...]) -> list[tuple[int, int]]:
        return _legal_actions(pos, lengths, targets, reserved_t, capacity)

    # ================================================================== #
    # 阶段 A：轻量 Dijkstra，标签仅 (写入, 步数)；父指针用于恢复一条
    # 最优路径，真正的动作字典序最小化在阶段 B 完成。
    # ================================================================== #
    # g[pos] = (write, steps)；parent[pos] = (prev_pos, (seg_idx, dest))
    g: dict[tuple[int, ...], tuple[int, int]] = {start: (0, 0)}
    parent: dict[tuple[int, ...], tuple[tuple[int, ...], tuple[int, int]]] = {}
    heap: list = [(0, 0, 0, start)]
    tie = 0
    budget: Optional[tuple[int, int]] = None
    solved = False
    total_write = sum(lengths)
    start_settled = sum(
        lengths[i] for i in range(len(lengths)) if start[i] == targets[i]
    )
    # settled_w[pos] = 该状态下已在目标上的区段长度之和（用于 O(1) 写入下界）。
    settled_w: dict[tuple[int, ...], int] = {start: start_settled}

    try:
        while heap:
            write, steps, _, pos = heapq.heappop(heap)
            if g.get(pos) != (write, steps):
                continue
            if pos == goal:
                solved = True
                break

            # 出队时才用较精确的 SCC 下界做一次最终过滤（调用频率远低于松弛）。
            if budget is not None:
                hw, hk = _heuristic(pos, lengths, targets)
                unsettled = total_write - settled_w[pos]
                if (write + unsettled + hw, steps + hk) > budget:
                    continue

            for seg_idx, dest in legal_action_list(pos):
                nw, nk = write + lengths[seg_idx], steps + 1
                npos = _apply(pos, seg_idx, dest)
                # 增量维护“已归位区段的写入量”（考虑离开/到达目标两种变化）。
                was_settled = pos[seg_idx] == targets[seg_idx]
                now_settled = npos[seg_idx] == targets[seg_idx]
                n_settled = settled_w[pos] + (
                    lengths[seg_idx] if now_settled and not was_settled
                    else -lengths[seg_idx] if was_settled and not now_settled
                    else 0
                )
                if budget is not None:
                    if (nw, nk) > budget:
                        continue
                    # 松弛时只用 O(1) 的平凡写入下界：剩余未归位区段至少
                    # 各自整段写入一次。
                    if nw + (total_write - n_settled) > budget[0]:
                        continue
                prev = g.get(npos)
                if prev is not None and (nw, nk) >= prev:
                    continue
                if len(g) >= _MAX_SEARCH_STATES:
                    raise SearchLimitReached
                g[npos] = (nw, nk)
                settled_w[npos] = n_settled
                parent[npos] = (pos, (seg_idx, dest))
                tie += 1
                heapq.heappush(heap, (nw, nk, tie, npos))
                if npos == goal and (
                    budget is None or (nw, nk) < budget
                ):
                    budget = (nw, nk)
    except SearchLimitReached:
        return None

    if not solved:
        return None

    w_star, k_star = g[goal]

    # ================================================================== #
    # 阶段 B：在预算 (w_star, k_star) 内做按动作字典序展开的有界 DFS，
    # 取第一个完整方案，即 (区段 ID, 目的起点) 序列字典序最小的最优解。
    # ================================================================== #
    def ordered(pos: tuple[int, ...]) -> list[tuple[str, int, int]]:
        return sorted(
            (
                (ids[i], dest, i)
                for i, dest in legal_action_list(pos)
            ),
            key=lambda t: (t[0], t[1]),
        )

    # 帧: [pos, spent_w, spent_k, actions, next_idx, chosen]
    frames: list[list] = [[start, 0, 0, ordered(start), 0, None]]
    trace: list[tuple[str, int]] = []
    # seen[pos] = 此前到达该状态的 (spent_w, spent_k)。DFS 按字典序展开：
    # 仅当更早前缀在代价上严格占优（至少一维更小、另一维不更大）时才剪掉
    # 当前前缀；等代价的不同前缀不剪，以免丢掉字典序更小的完整序列。
    seen: dict[tuple[int, ...], tuple[int, int]] = {}

    def _strictly_le(a: tuple[int, int], b: tuple[int, int]) -> bool:
        # 代价对 (写入, 步数) 的严格支配。
        return a <= b and a != b

    while frames:
        pos, spent_w, spent_k, acts, idx, chosen = frames[-1]

        if idx == 0 and chosen is not None:
            trace.append((chosen[0], chosen[1]))
            cur = (spent_w, spent_k)
            prev = seen.get(pos)
            if prev is not None and _strictly_le(prev, cur):
                frames.pop()
                trace.pop()
                continue
            seen.setdefault(pos, cur)
            if pos == goal:
                if cur == (w_star, k_star):
                    return list(trace)
                frames.pop()
                trace.pop()
                continue

        if idx >= len(acts):
            frames.pop()
            if chosen is not None:
                trace.pop()
            continue

        seg_id, dest, seg_idx = acts[idx]
        frames[-1][4] = idx + 1

        nw, nk = spent_w + lengths[seg_idx], spent_k + 1
        if nw > w_star or nk > k_star:
            continue
        npos = _apply(pos, seg_idx, dest)

        # 可接纳下界硬过滤（与阶段 A 相同）。
        remain = sum(
            length
            for p, length, t in zip(npos, lengths, targets)
            if p != t
        )
        hw, hk = _heuristic(npos, lengths, targets)
        if nw + remain + hw > w_star or nk + hk > k_star:
            continue

        frames.append(
            [npos, nw, nk, ordered(npos), 0, (seg_id, dest)]
        )

    return None
