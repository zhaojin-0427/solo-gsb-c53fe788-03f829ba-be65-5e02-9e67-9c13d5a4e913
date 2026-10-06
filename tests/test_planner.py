"""规划器算法级测试（不经过 HTTP）。"""

from app.planner import Segment, plan_moves


def run(cap, segs, reserved=None):
    return plan_moves(
        cap,
        [Segment(i, c, l, t) for i, c, l, t in segs],
        reserved or [],
    )


def check(name, got, want):
    assert got == want, f"{name}: got {got}, want {want}"
    print(f"OK  {name}: {got}")


# 题目样例：容量 5，A:[0,2)->[2,4)，B:[2,3)->[0,1)
# 三步最优，必须先搬 B 到 [4,5)。
check(
    "sample",
    run(5, [("A", 0, 2, 2), ("B", 2, 1, 0)]),
    [("B", 4), ("A", 2), ("B", 0)],
)

# 已到目标 -> 空序列
check("empty", run(5, [("A", 0, 2, 0), ("B", 2, 1, 2)]), [])

# 无空闲、互换 -> 无解
check("swap-dead", run(2, [("A", 0, 1, 1), ("B", 1, 1, 0)]), None)

# 有一个洞的互换：先让一个进洞
# cap3, A:[0,1)->[1,2), B:[1,2)->[0,1), hole [2,3)
check(
    "swap-hole",
    run(3, [("A", 0, 1, 1), ("B", 1, 1, 0)]),
    [("A", 2), ("B", 0), ("A", 1)],
)

# 字典序：同为直接归位动作，ID 小的在前
# cap4, A:[0,1)->[2,3), B:[1,2)->[3,4)，两者都可直接归位
check(
    "lex-order",
    run(4, [("A", 0, 1, 2), ("B", 1, 1, 3)]),
    [("A", 2), ("B", 3)],
)

# 自部分重叠（slide）：cap3, A:[0,2)->[1,3)，洞 [2,3)
# A 目标与自身重叠且空间不够 -> 无解（无法在不覆盖自身的情况下滑动）
check("self-overlap-dead", run(3, [("A", 0, 2, 1)]), None)

# 自部分重叠但有足够临时位：cap5, A:[0,2)->[1,3)，洞 [2,5)
# 先 A->[3,5)（完全让出旧区），再 A->[1,3)
check("self-overlap-temp", run(5, [("A", 0, 2, 1)]), [("A", 3), ("A", 1)])

# 保留区挡路：cap5, A:[0,1)->[4,5)，[1,4) 保留，洞 [4,5) 可直接搬
check(
    "reserved-ok",
    run(5, [("A", 0, 1, 4)], reserved=[(1, 4)]),
    [("A", 4)],
)

# 保留区把唯一的合适洞占掉：cap3, A:[0,1)->[1,2)，保留 [1,2)
# 当前 A 在 [0,1)，洞 [2,3)，目标 [1,2) 被保留 -> 永远不可达
check("reserved-blocks-target", run(3, [("A", 0, 1, 1)], reserved=[(1, 2)]), None)

# 64 位大坐标（验证不按容量枚举）
BIG = (1 << 64) - 1
check("uint64", run(BIG + 1, [("A", BIG, 1, 0)]), [("A", 0)])

# 大环：3 段循环依赖，一个洞
# cap4: A:[0,1)->[1,2), B:[1,2)->[2,3), C:[2,3)->[0,1), hole[3,4)
check(
    "cycle3",
    run(4, [("A", 0, 1, 1), ("B", 1, 1, 2), ("C", 2, 1, 0)]),
    [("A", 3), ("C", 0), ("B", 2), ("A", 1)],
)

# 写入量优先于动作字典序：
# cap6, A len2 [0,2)->[2,4), B len1 [2,3)->[0,1), 洞 [3,6)
# B 若先搬到 3（字典序更小的目的），会堵住 A 的目标迫使 A 也绕场；
# 而 B->5 可让 A 直接归位，总写入更少。
check(
    "write-first",
    run(6, [("A", 0, 2, 2), ("B", 2, 1, 0)]),
    [("B", 5), ("A", 2), ("B", 0)],
)

print("\nall planner tests passed")
