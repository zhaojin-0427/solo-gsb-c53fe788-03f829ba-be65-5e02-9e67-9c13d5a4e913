"""HTTP 层端到端测试（TestClient，无需起真实端口）。"""

from fastapi.testclient import TestClient

from app.main import app
from app.planner import SearchLimitReached

client = TestClient(app)


def post(body):
    return client.post("/plans", json=body)


def test_sample_from_spec():
    # 容量 5：A:[0,2)->[2,4)，B:[2,3)->[0,1)
    r = post(
        {
            "capacity": 5,
            "segments": [
                {"id": "A", "current_start": 0, "length": 2, "target_start": 2},
                {"id": "B", "current_start": 2, "length": 1, "target_start": 0},
            ],
        }
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "feasible": True,
        "moves": [
            {"segment_id": "B", "destination_start": 4, "length": 1},
            {"segment_id": "A", "destination_start": 2, "length": 2},
            {"segment_id": "B", "destination_start": 0, "length": 1},
        ],
    }


def test_empty_when_all_home():
    r = post(
        {
            "capacity": 4,
            "segments": [
                {"id": "A", "current_start": 0, "length": 1, "target_start": 0}
            ],
        }
    )
    assert r.status_code == 200
    assert r.json() == {"feasible": True, "moves": []}


def test_infeasible_swap_full_volume():
    r = post(
        {
            "capacity": 2,
            "segments": [
                {"id": "A", "current_start": 0, "length": 1, "target_start": 1},
                {"id": "B", "current_start": 1, "length": 1, "target_start": 0},
            ],
        }
    )
    assert r.status_code == 200
    assert r.json() == {"feasible": False, "moves": []}


def test_uint64_not_block_enumerated():
    big = 2**64 - 1
    r = post(
        {
            "capacity": 2**64,
            "segments": [
                {
                    "id": "X",
                    "current_start": big,
                    "length": 1,
                    "target_start": 0,
                }
            ],
        }
    )
    assert r.status_code == 200, r.text
    assert r.json()["feasible"] is True
    assert r.json()["moves"][0]["destination_start"] == 0


def test_422_when_current_overlap():
    r = post(
        {
            "capacity": 4,
            "segments": [
                {"id": "A", "current_start": 0, "length": 2, "target_start": 2},
                {"id": "B", "current_start": 1, "length": 1, "target_start": 3},
            ],
        }
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert any(item["code"] == "CURRENT_OVERLAP" for item in detail)


def test_422_when_target_touches_reserved():
    r = post(
        {
            "capacity": 4,
            "reserved": [{"start": 2, "length": 1}],
            "segments": [
                {"id": "A", "current_start": 0, "length": 1, "target_start": 2}
            ],
        }
    )
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "SEGMENT_TOUCHES_RESERVED"


def test_422_when_out_of_bounds():
    r = post(
        {
            "capacity": 4,
            "segments": [
                {"id": "A", "current_start": 3, "length": 2, "target_start": 0}
            ],
        }
    )
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "SEGMENT_OUT_OF_BOUNDS"


def test_422_when_length_not_positive():
    r = post(
        {
            "capacity": 4,
            "segments": [
                {"id": "A", "current_start": 0, "length": 0, "target_start": 1}
            ],
        }
    )
    assert r.status_code == 422


def test_422_when_negative_coordinate():
    r = post(
        {
            "capacity": 4,
            "segments": [
                {"id": "A", "current_start": -1, "length": 1, "target_start": 0}
            ],
        }
    )
    assert r.status_code == 422


def test_422_when_duplicate_ids():
    r = post(
        {
            "capacity": 6,
            "segments": [
                {"id": "A", "current_start": 0, "length": 1, "target_start": 2},
                {"id": "A", "current_start": 1, "length": 1, "target_start": 3},
            ],
        }
    )
    assert r.status_code == 422
    assert r.json()["detail"][0]["code"] == "DUPLICATE_SEGMENT_ID"


def test_422_on_unknown_field():
    r = post(
        {
            "capacity": 4,
            "bogus": 1,
            "segments": [
                {"id": "A", "current_start": 0, "length": 1, "target_start": 1}
            ],
        }
    )
    assert r.status_code == 422


def test_422_on_uint64_overflow_endpoint():
    r = post(
        {
            "capacity": 2**64,
            "segments": [
                {
                    "id": "A",
                    "current_start": 2**64 - 1,
                    "length": 2,  # 端点 2^64+1，超出 64 位
                    "target_start": 0,
                }
            ],
        }
    )
    assert r.status_code == 422
    codes = {item["code"] for item in r.json()["detail"]}
    assert "SEGMENT_OUT_OF_BOUNDS" in codes


def test_422_when_capacity_exceeds_uint64_coordinate_space():
    r = post(
        {
            "capacity": 2**64 + 5,
            "segments": [
                {"id": "A", "current_start": 0, "length": 1, "target_start": 1}
            ],
        }
    )
    assert r.status_code == 422


def test_rotate12_returns_replayable_13_steps():
    # 容量 13，12 个长度 1 区段轮转 (i+1)%12，位置 12 空闲：13 步借洞归位。
    body = {
        "capacity": 13,
        "segments": [
            {
                "id": str(i),
                "current_start": i,
                "length": 1,
                "target_start": (i + 1) % 12,
            }
            for i in range(12)
        ],
    }
    r = post(body)
    assert r.status_code == 200, r.text
    moves = r.json()["moves"]
    assert r.json()["feasible"] is True
    assert len(moves) == 13

    # 逐块重放：源目不重叠、目的整块空闲、复制后释放、最终到位。
    grid = [None] * 13
    for i in range(12):
        grid[i] = str(i)
    for m in moves:
        sid = m["segment_id"]
        dest = m["destination_start"]
        assert m["length"] == 1
        src = grid.index(sid)
        assert grid[dest] is None, (sid, dest, grid[dest])
        assert src != dest
        grid[dest] = sid
        grid[src] = None
    for i in range(12):
        assert grid[(i + 1) % 12] == str(i)


def test_503_when_search_limit_reached(monkeypatch):
    def boom(*_args, **_kwargs):
        raise SearchLimitReached

    monkeypatch.setattr("app.main.plan_moves", boom)
    r = post(
        {
            "capacity": 3,
            "segments": [
                {"id": "A", "current_start": 0, "length": 1, "target_start": 1}
            ],
        }
    )
    assert r.status_code == 503
    assert r.json()["detail"][0]["code"] == "SEARCH_LIMIT_REACHED"


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
