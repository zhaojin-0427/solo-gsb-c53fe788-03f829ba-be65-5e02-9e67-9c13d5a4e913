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

每个自由洞中只需考察少量候选目的起点：洞左缘、洞右缘、区段自己的最终
目标起点、与目标相切的 target±length。任何其它严格内部摆放都能在不增加
写入与步数的前提下滑向这些位置（同洞内部本来空闲，滑动不覆盖任何数据）。
再排除「纯嵌套」临时摆放（目的洞整体罩住另一个尚未归位区段的源）：
先搬走内层永远不劣。

搜索框架（两阶段）
----------------
阶段 A 求最优值：以 f = g + h 为优先键的 A*（允许重开）。
  g = (已花写入块数, 已花步数)，h 为下述可接纳下界。h 不满足一致性，
  因此同一状态以更优 g 到达时会重新入堆（陈旧堆项出队即弃）；目标首次
  以其最优 g 弹出时，g(goal) 即全序最优值 (W*, K*)。若按 g 弹出（旧
  Dijkstra 做法），本服务退化为按深度 BFS 排列空间——12 段环 + 1 洞的
  轮转即会耗尽状态上限；按 f 弹出后目标导向，百余状态即可收敛。
阶段 B 恢复序列：在预算 (W*, K*) 内按 (区段 ID, 目的起点) 字典序做有界
  DFS，第一个到达目标的完整方案即字典序最小的最优序列。

同一状态不同前缀的归并规则
------------------------
* 阶段 A：状态相同则剩余最优代价相同，故每状态只保留字典序最小的
  g=(写入, 步数)；较差前缀被严格占优，直接丢弃（动作序列不在此阶段比较，
  字典序恢复全部留给阶段 B）。
* 阶段 B：DFS 前序展开即动作序列字典序，首次到达某状态的前缀必然字典序
  最小。每状态记录已到代价的 Pareto 前沿；新前缀若被前沿中某项按分量
  <= 支配（含相等）即剪掉——严格支配时它根本不存在预算内的完成（否则
  与 (W*, K*) 的最优性矛盾）；相等时两前缀等长、可选后缀集合完全相同，
  而先到前缀字典序更小，剪掉不丢最优。

可安全剪枝的下界
--------------
* 每个未归位区段至少再整体搬一次：剩余写入 >= 未归位长度之和，
  剩余步数 >= 未归位段数；
* 依赖图（u->v 表示 u 的最终目标与 v 当前占用相交）中每个含环 SCC，
  至少需要把环内一个区段额外复制到临时位置：额外写入 >= 环内最短长度，
  额外步数 >= 1；各 SCC 顶点互不相交，额外量可累加；
* 两组下界按分量相加仍可接纳（SCC 的额外复制发生在「每段各一次」之外），
  同时用于阶段 A 的优先键 f 与阶段 B 的预算过滤。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Optional


UINT64_MAX = (1 << 64) - 1


class SearchLimitReached(Exception):
    """搜索状态数超过安全阀：既未证最优也未证无解，HTTP 层映射为 503。"""


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
# 下界
# --------------------------------------------------------------------------- #

def _scc_extra(
    positions: tuple[int, ...],
    lengths: tuple[int, ...],
    targets: tuple[int, ...],
) -> tuple[int, int]:
    """
    依赖图中含环强连通分量带来的「额外」代价 (额外写入块数, 额外步数)。

    建依赖图：u -> v 表示 u 的最终目标与 v 当前占用相交（u 归位前 v 必须
    先让出）。含自环或规模 > 1 的 SCC 是死锁环；每个这样的 SCC 至少要先把
    其中一个完整区段复制到临时位置（一次长度 >= 环内最短长度的额外写入、
    一次额外步数）。各 SCC 顶点互不相交，额外量可安全累加。
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
) -> tuple[int, int]:
    """
    剩余代价 (写入块数, 步数) 的可接纳下界，两个分量分别可接纳：

    * 每个未归位区段至少再整体搬一次：写入 >= 未归位长度之和，
      步数 >= 未归位段数；
    * 每个含环 SCC 在上述「每段各一次」之外，至少再额外临时复制一次
      环内最短区段（见 _scc_extra）。

    用于阶段 A 的优先键 f = g + h 与阶段 B 的预算过滤；剪枝只会丢掉
    「无论如何都超预算」的分支，不会丢掉最优解。
    """
    unsettled_write = 0
    unsettled_count = 0
    for p, length, t in zip(positions, lengths, targets):
        if p != t:
            unsettled_write += length
            unsettled_count += 1
    extra_write, extra_steps = _scc_extra(positions, lengths, targets)
    return unsettled_write + extra_write, unsettled_count + extra_steps


# --------------------------------------------------------------------------- #
# 规划器
# --------------------------------------------------------------------------- #

# 搜索安全阀：极端病态（如可用空位极少、区段极多）时限状态数，避免无界计算。
# 触发时抛 SearchLimitReached，由 HTTP 层映射为 503（而非误报无解）。
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
    返回 (segment_id, dest_start) 形式的逐步搬移序列；已证无解返回 None；
    已全部到位时返回 []。

    搜索状态数超过 _MAX_SEARCH_STATES 时抛 SearchLimitReached——此时既未
    证最优也未证无解，调用方不得按「无解」处理。
    """
    ids = [s.id for s in segments]
    lengths = tuple(s.length for s in segments)
    targets = tuple(s.target_start for s in segments)
    reserved_t = tuple(reserved)
    start = tuple(s.current_start for s in segments)
    goal = targets

    if start == goal:
        return []

    # 快速不可行判定：任何时刻连续空闲块数 <= 总空闲块数；若某未归位区段
    # 长度超过总空闲块数，它的目的区间永远凑不出来。
    total_free = capacity - sum(lengths) - sum(
        hi - lo for lo, hi in reserved_t if hi > lo
    )
    for i, length in enumerate(lengths):
        if start[i] != targets[i] and length > total_free:
            return None

    # ================================================================== #
    # 阶段 A：A*（允许重开）求全序最优值 (W*, K*)。
    # 堆项 (f_w, f_k, g_w, g_k, tie, pos)，f = g + 可接纳下界。
    # ================================================================== #
    # g[pos] = 到达 pos 的最优 (写入, 步数)——归并规则一：同状态只留最小 g。
    g: dict[tuple[int, ...], tuple[int, int]] = {start: (0, 0)}
    hw0, hk0 = _lower_bound(start, lengths, targets)
    heap: list = [(hw0, hk0, 0, 0, 0, start)]
    tie = 0
    budget: Optional[tuple[int, int]] = None
    solved = False

    while heap:
        fw, fk, gw, gk, _, pos = heapq.heappop(heap)
        if g.get(pos) != (gw, gk):
            continue  # 陈旧堆项：该状态已被更优的 g 重开
        if pos == goal:
            # 可接纳下界 + 允许重开的 A*：目标首次以最优 g 弹出即全序最优。
            solved = True
            break
        if budget is not None and (fw, fk) > budget:
            continue  # 下界已超已知最优解，展开无意义

        for seg_idx, dest in _legal_actions(pos, lengths, targets, reserved_t, capacity):
            nw, nk = gw + lengths[seg_idx], gk + 1
            npos = _apply(pos, seg_idx, dest)
            prev = g.get(npos)
            if prev is not None and (nw, nk) >= prev:
                continue  # 归并规则一：同状态的较差前缀被严格占优
            hw, hk = _lower_bound(npos, lengths, targets)
            if budget is not None and (nw + hw, nk + hk) > budget:
                continue  # 可接纳下界超预算：此分支不可能改进已知最优
            if len(g) >= _MAX_SEARCH_STATES:
                raise SearchLimitReached
            g[npos] = (nw, nk)
            tie += 1
            heapq.heappush(heap, (nw + hw, nk + hk, nw, nk, tie, npos))
            if npos == goal and (budget is None or (nw, nk) < budget):
                budget = (nw, nk)  # 目标处下界为 0，f == g

    if not solved:
        return None

    w_star, k_star = g[goal]

    # ================================================================== #
    # 阶段 B：在预算 (W*, K*) 内按 (区段 ID, 目的起点) 字典序做有界 DFS，
    # 第一个到达目标的完整方案即字典序最小的最优序列。
    # ================================================================== #
    def ordered(pos: tuple[int, ...]) -> list[tuple[str, int, int]]:
        return sorted(
            (
                (ids[i], dest, i)
                for i, dest in _legal_actions(pos, lengths, targets, reserved_t, capacity)
            ),
            key=lambda t: (t[0], t[1]),
        )

    # 帧: [pos, spent_w, spent_k, actions, next_idx, chosen]
    frames: list[list] = [[start, 0, 0, ordered(start), 0, None]]
    trace: list[tuple[str, int]] = []
    # seen[pos] = 到达 pos 的代价 Pareto 前沿——归并规则二：被前沿某项按
    # 分量 <= 支配（含相等）的新前缀直接剪掉；DFS 前序保证先到者字典序更小。
    seen: dict[tuple[int, ...], list[tuple[int, int]]] = {}
    expanded = 0

    while frames:
        pos, spent_w, spent_k, acts, idx, chosen = frames[-1]

        if idx == 0 and chosen is not None:
            front = seen.setdefault(pos, [])
            if any(w <= spent_w and k <= spent_k for w, k in front):
                frames.pop()
                trace.pop()
                continue
            # 前沿中反过来被新代价支配的项可安全丢弃（其前缀字典序更小、
            # 代价不更优；凡被它们支配的后来者同样被新代价支配）。
            front[:] = [
                (w, k) for w, k in front if not (spent_w <= w and spent_k <= k)
            ]
            front.append((spent_w, spent_k))
            if pos == goal:
                if (spent_w, spent_k) == (w_star, k_star):
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

        # 与阶段 A 相同的可接纳下界硬过滤：超预算分支不可能属于最优解。
        hw, hk = _lower_bound(npos, lengths, targets)
        if nw + hw > w_star or nk + hk > k_star:
            continue

        expanded += 1
        if expanded > _MAX_SEARCH_STATES:
            raise SearchLimitReached
        trace.append((seg_id, dest))
        frames.append([npos, nw, nk, ordered(npos), 0, (seg_id, dest)])

    return None  # 理论不可达：阶段 A 已证 (W*, K*) 可达
