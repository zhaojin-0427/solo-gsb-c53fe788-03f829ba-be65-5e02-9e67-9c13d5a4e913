"""请求/响应模型与输入语义校验。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from .planner import UINT64_MAX, Segment


class SegmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, description="区段 ID（非空字符串）")]
    current_start: Annotated[int, Field(ge=0, le=UINT64_MAX)]
    length: Annotated[int, Field(ge=1, le=UINT64_MAX)]
    target_start: Annotated[int, Field(ge=0, le=UINT64_MAX)]

    @field_validator("id")
    @classmethod
    def _id_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("区段 ID 不能为空白字符串")
        return v


class ReservedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: Annotated[int, Field(ge=0, le=UINT64_MAX)]
    length: Annotated[int, Field(ge=0, le=UINT64_MAX)]


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # 容量最大 2^64：合法块坐标为 0 .. 2^64-1。
    capacity: Annotated[int, Field(ge=0, le=1 << 64)]
    segments: list[SegmentIn]
    reserved: Annotated[list[ReservedIn], Field(default_factory=list)]


class MoveOut(BaseModel):
    segment_id: str
    destination_start: int
    length: int


class PlanResponse(BaseModel):
    feasible: bool
    moves: list[MoveOut]


class ValidationErrorItem(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    """供 OpenAPI 文档展示；实际错误由 app.exception_handler 按此形状返回。"""

    detail: list[ValidationErrorItem]


# --------------------------------------------------------------------------- #
# 语义校验
# --------------------------------------------------------------------------- #

class SemanticError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _overlap(a_lo: int, a_hi: int, b_lo: int, b_hi: int) -> bool:
    return a_lo < b_hi and b_lo < a_hi


def validate_semantics(req: PlanRequest) -> tuple[list[Segment], list[tuple[int, int]]]:
    """
    校验所有跨字段语义约束，返回 (segments, reserved)。
    坐标一律按半开区间 [start, start+length) 处理。
    """
    cap = req.capacity

    # 保留区：长度允许为 0（半开区间下不占任何块）。越界统一相对卷容量判定，
    # 容量最大为 2^64，故端点最大为 2^64（合法坐标仍为 0 .. 2^64-1）。
    reserved: list[tuple[int, int]] = []
    for i, r in enumerate(req.reserved):
        lo, hi = r.start, r.start + r.length
        if hi > cap:
            raise SemanticError(
                "RESERVED_OUT_OF_BOUNDS",
                f"保留区 #{i} [{lo},{hi}) 超出卷容量 {cap}",
            )
        reserved.append((lo, hi))

    # 保留区两两互不重叠（0 长度区间自然不会重叠）。
    for i in range(len(reserved)):
        for j in range(i + 1, len(reserved)):
            if _overlap(*reserved[i], *reserved[j]):
                raise SemanticError(
                    "RESERVED_OVERLAP",
                    f"保留区 #{i} 与 #{j} 互相重叠",
                )

    segments: list[Segment] = []
    seen_ids: set[str] = set()
    for i, s in enumerate(req.segments):
        if s.id in seen_ids:
            raise SemanticError("DUPLICATE_SEGMENT_ID", f"区段 ID 重复: {s.id!r}")
        seen_ids.add(s.id)

        cur_lo, cur_hi = s.current_start, s.current_start + s.length
        tgt_lo, tgt_hi = s.target_start, s.target_start + s.length

        if cur_hi > cap:
            raise SemanticError(
                "SEGMENT_OUT_OF_BOUNDS",
                f"区段 {s.id!r} 当前区间 [{cur_lo},{cur_hi}) 超出卷容量 {cap}",
            )
        if tgt_hi > cap:
            raise SemanticError(
                "SEGMENT_OUT_OF_BOUNDS",
                f"区段 {s.id!r} 目标区间 [{tgt_lo},{tgt_hi}) 超出卷容量 {cap}",
            )

        for k, (r_lo, r_hi) in enumerate(reserved):
            if _overlap(cur_lo, cur_hi, r_lo, r_hi):
                raise SemanticError(
                    "SEGMENT_TOUCHES_RESERVED",
                    f"区段 {s.id!r} 当前区间与保留区 #{k} 重叠",
                )
            if _overlap(tgt_lo, tgt_hi, r_lo, r_hi):
                raise SemanticError(
                    "SEGMENT_TOUCHES_RESERVED",
                    f"区段 {s.id!r} 目标区间与保留区 #{k} 重叠",
                )

        segments.append(
            Segment(
                id=s.id,
                current_start=s.current_start,
                length=s.length,
                target_start=s.target_start,
            )
        )

    # 各「当前位置」两两互不重叠，各「目标位置」两两互不重叠。
    # 注意：同一条区段的 current 与 target 允许相交（这是正常的滑动/环依赖），
    # 只需 current 集合内部、target 集合内部各自不重叠。
    for i in range(len(segments)):
        for j in range(i + 1, len(segments)):
            a, b = segments[i], segments[j]
            if _overlap(
                a.current_start,
                a.current_start + a.length,
                b.current_start,
                b.current_start + b.length,
            ):
                raise SemanticError(
                    "CURRENT_OVERLAP",
                    f"区段 {a.id!r} 与 {b.id!r} 的当前区间互相重叠",
                )
            if _overlap(
                a.target_start,
                a.target_start + a.length,
                b.target_start,
                b.target_start + b.length,
            ):
                raise SemanticError(
                    "TARGET_OVERLAP",
                    f"区段 {a.id!r} 与 {b.id!r} 的目标区间互相重叠",
                )

    return segments, reserved
