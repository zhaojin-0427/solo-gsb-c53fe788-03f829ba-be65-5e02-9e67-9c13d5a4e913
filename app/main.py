"""卷区段安全搬移规划 HTTP 服务。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .planner import plan_moves
from .schemas import (
    ErrorEnvelope,
    PlanRequest,
    PlanResponse,
    MoveOut,
    SemanticError,
    validate_semantics,
)

app = FastAPI(
    title="卷区段安全搬移规划服务",
    version="1.0.0",
    description=(
        "在不逐块枚举卷容量的前提下，规划完整区段（extent）的安全搬移序列：\n\n"
        "1. 总写入块数最少；\n"
        "2. 步数最少；\n"
        "3. 同分时动作序列按 (区段 ID, 目的起点) 字典序最小。"
    ),
)


@app.exception_handler(SemanticError)
async def semantic_error_handler(_: Request, exc: SemanticError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": [{"code": exc.code, "message": exc.message}]},
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "code": "INVALID_REQUEST",
                    "message": err.get("msg", "请求不合法"),
                    "location": ".".join(
                        str(p) for p in err.get("loc", []) if p != "body"
                    ),
                }
                for err in exc.errors()
            ]
        },
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/plans",
    response_model=PlanResponse,
    responses={422: {"model": ErrorEnvelope}},
    summary="生成区段安全搬移序列",
)
async def create_plan(req: PlanRequest) -> PlanResponse:
    segments, reserved = validate_semantics(req)

    result = plan_moves(req.capacity, segments, reserved)
    if result is None:
        return PlanResponse(feasible=False, moves=[])

    length_by_id = {s.id: s.length for s in segments}
    return PlanResponse(
        feasible=True,
        moves=[
            MoveOut(
                segment_id=seg_id,
                destination_start=dest,
                length=length_by_id[seg_id],
            )
            for seg_id, dest in result
        ],
    )
