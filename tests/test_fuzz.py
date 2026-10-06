"""差分测试：小规模随机实例下，规划器结果与逐块 BFS 暴力枚举一致；并物理重放。"""

import itertools
import random

from app.planner import Segment, plan_moves


def replay(cap, segs, reserved, moves):
    """按 1 块粒度物理重放，违规则抛错；成功返回最终占用。"""
    grid = [None] * cap
    home = {}
    length = {}
    for idx, (sid, c, l, t) in enumerate(segs):
        for x in range(c, c + l):
            assert grid[x] is None, f"初始占用冲突 @{x}"
            grid[x] = sid
        home[sid] = t
        length[sid] = l
    for lo, hi in reserved:
        for x in range(lo, hi):
            assert grid[x] is None
            grid[x] = "#"

    for sid, dest in moves:
        l = length[sid]
        src = grid.index(sid)
        # 目的必须整块空闲
        for x in range(dest, dest + l):
            assert grid[x] is None, f"搬 {sid} 时目的块 {x} 被 {grid[x]} 占用"
        # 源/目的不重叠
        assert not (dest < src + l and src < dest + l), f"搬 {sid} 时源目重叠"
        # 复制完成才释放：先写副本
        for x in range(dest, dest + l):
            grid[x] = sid
        for x in range(src, src + l):
            if not (dest <= x < dest + l):
                grid[x] = None

    for sid, t in home.items():
        for x in range(t, t + length[sid]):
            assert grid[x] == sid, f"最终 {sid} 未到位 @{x}: {grid[x]}"
    return grid


def brute_force(cap, segs, reserved):
    """逐块状态 BFS 求 (写入, 步数, 动作字典序) 最优；无解 None。"""
    n = len(segs)
    ids = [s[0] for s in segs]
    lens = [s[2] for s in segs]
    tgts = [s[3] for s in segs]
    start = tuple(s[1] for s in segs)
    goal = tuple(tgts)

    def free(pos):
        used = [False] * cap
        for lo, hi in reserved:
            for x in range(lo, hi):  # 零长度区间自然不标记任何块
                used[x] = True
        for p, l in zip(pos, lens):
            for x in range(p, p + l):
                used[x] = True
        holes = []
        i = 0
        while i < cap:
            if not used[i]:
                j = i
                while j < cap and not used[j]:
                    j += 1
                holes.append((i, j))
                i = j
            else:
                i += 1
        return holes

    def actions(pos):
        out = []
        for i, (p, l) in enumerate(zip(pos, lens)):
            if p == tgts[i]:
                continue
            for lo, hi in free(pos):
                if hi - lo < l:
                    continue
                # 小规模暴力：枚举洞内每一个可能起点（独立于规划器的边缘候选）
                for d in range(lo, hi - l + 1):
                    if d == p:
                        continue
                    if d < p + l and p < d + l:
                        continue
                    out.append((i, d))
        return sorted(set(out))

    # Dijkstra，代价 (写入, 步数)；同一状态在相同代价下保留路径字典序最小者
    best = {start: (0, 0)}
    best_path = {start: []}
    import heapq

    def lex_key(path):
        return tuple((ids[a], b) for a, b in path)

    h = [(0, 0, (), start)]
    while h:
        w, k, _key, pos = heapq.heappop(h)
        if best.get(pos) != (w, k) or _key != lex_key(best_path[pos]):
            continue
        if pos == goal:
            return [(ids[i], d) for i, d in best_path[pos]]
        for i, d in actions(pos):
            np_ = list(pos)
            np_[i] = d
            np_ = tuple(np_)
            nw, nk = w + lens[i], k + 1
            npath = best_path[pos] + [(i, d)]
            prev = best.get(np_)
            nkey = lex_key(npath)
            if prev is None or (nw, nk) < prev or (
                (nw, nk) == prev and nkey < lex_key(best_path[np_])
            ):
                best[np_] = (nw, nk)
                best_path[np_] = npath
                heapq.heappush(h, (nw, nk, nkey, np_))
    return None


def random_instance(rng, cap):
    n = rng.randint(0, 4)
    occupied = [False] * cap
    segs = []
    used_ids = set()
    for i in range(n):
        l = rng.randint(1, max(1, cap // 2 + 1))
        free_starts = [
            s
            for s in range(0, cap - l + 1)
            if not any(occupied[s : s + l])
        ]
        if not free_starts:
            break
        c = rng.choice(free_starts)
        t = rng.choice(free_starts)
        for x in range(c, c + l):
            occupied[x] = True
        sid = chr(ord("A") + i)
        used_ids.add(sid)
        segs.append((sid, c, l, t))
    reserved = []
    # 少量保留区
    for _ in range(rng.randint(0, 1)):
        l = rng.randint(0, max(0, cap // 3))
        starts = [
            s for s in range(0, cap - l + 1) if not any(occupied[s : s + l])
        ]
        if not starts:
            continue
        s = rng.choice(starts)
        reserved.append((s, s + l))
        for x in range(s, s + l):
            occupied[x] = True
    return segs, reserved


def main():
    rng = random.Random(20261006)
    total = 0
    for trial in range(3000):
        cap = rng.randint(1, 7)
        segs, reserved = random_instance(rng, cap)
        want = brute_force(cap, segs, reserved)
        got = plan_moves(
            cap,
            [Segment(*s) for s in segs],
            reserved,
        )
        total += 1
        assert got == want, (
            f"MISMATCH cap={cap} segs={segs} reserved={reserved}\n"
            f"want={want}\ngot ={got}"
        )
        if got:
            replay(cap, segs, reserved, got)
    print(f"differential OK: {total} random instances (cap<=7) matched brute force")


if __name__ == "__main__":
    main()
