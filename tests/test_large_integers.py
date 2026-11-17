"""大整数精度、旧库迁移与失败回滚测试。

覆盖：
- 坐标在 SQLite 64 位整数范围内、但面积/交叠面积超出 64 位时的精确存取；
- 最大余数法与户数守恒在超大整数权重下不变；
- 旧库（INTEGER 亲和面积列 + 小面积数据）升级、重启后历史版本与已发布结果仍可读；
- 新任务写入失败时不留半份明细、不改变当前发布；
- 越界坐标得到 400 而不是 500。
"""
from __future__ import annotations

import sqlite3

from app import db
from app.allocation import NewDistrict, OldCell, allocate, overlap_area, rect_area
from tests.conftest import poll_generation

N = 3100000001
HUGE_AREA = N * N  # 9610000006200000001，超出有符号 64 位整数
INT64_MAX = 2**63 - 1

OLD_V1 = {
    "items": [
        {"id": "g1", "rect": [0, 0, 10, 10], "households": 7},
        {"id": "g2", "rect": [0, 10, 10, 20], "households": 11},
    ]
}
NEW_V1 = {
    "items": [
        {"id": "d1", "rect": [0, 0, 5, 20]},
        {"id": "d2", "rect": [5, 0, 10, 20]},
    ]
}


# ---------- 纯函数层：超大整数仍精确 ----------

def test_rect_area_exact_beyond_int64():
    assert HUGE_AREA == 9610000006200000001
    assert HUGE_AREA > INT64_MAX
    assert rect_area((0, 0, N, N)) == HUGE_AREA


def test_overlap_area_exact_beyond_int64():
    # 条带切分：宽 N 高 1 的条带面积 N；其余部分 N*(N-1) 也超 64 位
    assert overlap_area((0, 0, N, N), (0, 0, N, 1)) == N
    rest = overlap_area((0, 0, N, N), (0, 1, N, N))
    assert rest == N * (N - 1)
    assert rest > INT64_MAX
    assert isinstance(rest, int)


def test_allocate_huge_weights_conservation_and_exact_areas():
    old = [OldCell("g1", (0, 0, N, N), 7)]
    districts = [
        NewDistrict("d1", (0, 0, N, 1)),       # 面积 N
        NewDistrict("d2", (0, 1, N, N)),       # 面积 N*(N-1)
    ]
    results = allocate(old, districts)
    # 完整覆盖：交叠之和恰为旧格面积（精确整数）
    assert sum(a.overlap_area for a in results) == HUGE_AREA
    # 户数守恒
    assert sum(a.households for a in results) == 7
    by = {a.district_id: a for a in results}
    assert by["d1"].overlap_area == N
    assert by["d2"].overlap_area == N * (N - 1)
    # d2 权重几乎为全部面积：7 户应全部分给 d2，d1 为 0
    assert by["d1"].households == 0
    assert by["d2"].households == 7
    for a in results:
        assert isinstance(a.overlap_area, int)


def test_largest_remainder_tie_still_resolved_by_id_with_huge_weights():
    # 两个等权（同为超大面积）片区分 3 户：余数并列，编号小的 d1 多得 1
    side = 5_000_000_000  # 偶数，可等分；坐标仍在 int64 内
    half = side // 2
    assert half * side > INT64_MAX  # 每个权重本身就超 64 位
    old = [OldCell("g1", (0, 0, side, side), 3)]
    districts = [
        NewDistrict("d2", (0, 0, half, side)),
        NewDistrict("d1", (half, 0, side, side)),
    ]
    results = allocate(old, districts)
    assert results[0].overlap_area == half * side
    assert results[1].overlap_area == half * side
    shares = {a.district_id: a.households for a in results}
    assert shares == {"d1": 2, "d2": 1}


# ---------- API 层：大面积版本入库与查询 ----------

def test_huge_area_submission_returns_exact_integer(client):
    payload = {
        "items": [
            {"id": "big", "rect": [0, 0, N, N], "households": 5},
        ]
    }
    r = client.post("/old-grids", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    item = body["items"][0]
    # 数值字段保持 JSON 整数（不是浮点、不是字符串），且精度无损
    assert isinstance(item["area"], int)
    assert item["area"] == HUGE_AREA

    # 原始响应体里必须是完整十进制数字，不能出现浮点形式
    raw = r.text
    assert "9610000006200000001" in raw
    assert "9.61" not in raw

    # 版本查询同样精确
    got = client.get(f"/versions/{body['version']}").json()
    assert got["items"][0]["area"] == HUGE_AREA
    assert got["items"][0]["x2"] == N and got["items"][0]["y2"] == N


def test_huge_area_new_districts_and_generation_publish(client):
    client.post(
        "/old-grids",
        json={"items": [{"id": "g1", "rect": [0, 0, N, N], "households": 7}]},
    )
    client.post(
        "/new-districts",
        json={
            "items": [
                {"id": "d1", "rect": [0, 0, N, 1]},
                {"id": "d2", "rect": [0, 1, N, N]},
            ]
        },
    )
    gen = poll_generation(client, client.post("/generations").json()["id"])
    assert gen["status"] == "succeeded", gen

    result = client.get("/result").json()
    assert result["total_households"] == 7
    assert sum(d["allocated_households"] for d in result["districts"]) == 7

    # 输入版本快照中的面积精确
    assert result["old_version"]["items"][0]["area"] == HUGE_AREA

    detail = {
        x["district_id"]: x
        for d in result["districts"]
        for x in d["detail"]
    }
    assert isinstance(detail["d1"]["overlap_area"], int)
    assert detail["d1"]["overlap_area"] == N
    assert isinstance(detail["d2"]["overlap_area"], int)
    assert detail["d2"]["overlap_area"] == N * (N - 1)
    assert detail["d1"]["households"] == 0
    assert detail["d2"]["households"] == 7

    # 原始 JSON 中交叠面积为完整整数
    raw = client.get("/result").text
    assert str(N * (N - 1)) in raw


def test_coordinate_outside_int64_rejected_as_400(client):
    # 坐标超出 SQLite 整数范围：业务校验拒绝（400），而不是底层 500
    bad = {
        "items": [
            {"id": "g1", "rect": [0, 0, INT64_MAX + 1, 1], "households": 1},
        ]
    }
    r = client.post("/old-grids", json=bad)
    assert r.status_code == 400, r.text
    assert "64 位整数" in r.json()["detail"]
    # 没有留下任何版本
    assert client.get("/versions").json() == []


def test_failed_validation_writes_nothing(client):
    # 编号重复触发 400：版本表不应出现半份写入
    before = client.get("/versions").json()
    dup = {
        "items": [
            {"id": "g1", "rect": [0, 0, 1, 1], "households": 1},
            {"id": "g1", "rect": [2, 0, 3, 1], "households": 1},
        ]
    }
    assert client.post("/old-grids", json=dup).status_code == 400
    assert client.get("/versions").json() == before


# ---------- 旧库迁移 ----------

# 与升级前应用完全一致的旧结构
LEGACY_SCHEMA = """
CREATE TABLE versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK (kind IN ('old', 'new')),
    created_at  TEXT NOT NULL,
    item_count  INTEGER NOT NULL
);
CREATE TABLE cells (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id   INTEGER NOT NULL REFERENCES versions(id),
    seq          INTEGER NOT NULL,
    cell_id      TEXT NOT NULL,
    x1           INTEGER NOT NULL,
    y1           INTEGER NOT NULL,
    x2           INTEGER NOT NULL,
    y2           INTEGER NOT NULL,
    area         INTEGER NOT NULL,
    households   INTEGER
);
CREATE INDEX idx_cells_version ON cells(version_id);
CREATE TABLE generations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    old_version   INTEGER REFERENCES versions(id),
    new_version   INTEGER REFERENCES versions(id),
    error         TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);
CREATE TABLE result_parts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id  INTEGER NOT NULL REFERENCES generations(id),
    district_id    TEXT NOT NULL,
    old_id         TEXT NOT NULL,
    overlap_area   INTEGER NOT NULL,
    households     INTEGER NOT NULL
);
CREATE INDEX idx_parts_generation ON result_parts(generation_id);
CREATE TABLE current_result (
    only_row       INTEGER PRIMARY KEY CHECK (only_row = 1),
    generation_id  INTEGER NOT NULL REFERENCES generations(id),
    published_at   TEXT NOT NULL,
    total_households INTEGER NOT NULL
);
"""


def _build_legacy_db(path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO versions(id, kind, created_at, item_count)"
            " VALUES (1, 'old', '2026-01-01T00:00:00+00:00', 2)"
        )
        conn.execute(
            "INSERT INTO versions(id, kind, created_at, item_count)"
            " VALUES (2, 'new', '2026-01-01T00:00:00+00:00', 2)"
        )
        cells = [
            (1, 0, "g1", 0, 0, 10, 10, 100, 7),
            (1, 1, "g2", 0, 10, 10, 20, 100, 11),
            (2, 0, "d1", 0, 0, 5, 20, 100, None),
            (2, 1, "d2", 5, 0, 10, 20, 100, None),
        ]
        conn.executemany(
            "INSERT INTO cells(version_id, seq, cell_id, x1, y1, x2, y2, area, households)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            cells,
        )
        conn.execute(
            "INSERT INTO generations(id, status, old_version, new_version, created_at,"
            " started_at, finished_at) VALUES (1, 'succeeded', 1, 2,"
            " '2026-01-01T00:00:01+00:00', '2026-01-01T00:00:01+00:00',"
            " '2026-01-01T00:00:02+00:00')"
        )
        parts = [
            (1, "d1", "g1", 50, 4),
            (1, "d2", "g1", 50, 3),
            (1, "d1", "g2", 50, 6),
            (1, "d2", "g2", 50, 5),
        ]
        conn.executemany(
            "INSERT INTO result_parts(generation_id, district_id, old_id,"
            " overlap_area, households) VALUES (?, ?, ?, ?, ?)",
            parts,
        )
        conn.execute(
            "INSERT INTO current_result(only_row, generation_id, published_at,"
            " total_households) VALUES (1, 1, '2026-01-01T00:00:02+00:00', 18)"
        )
        conn.commit()
    finally:
        conn.close()


def test_legacy_db_migrates_and_remains_readable_after_restart(tmp_path):
    db_file = tmp_path / "legacy.db"
    _build_legacy_db(db_file)

    # 第一次打开：触发升级
    conn = db.get_conn(db_file)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        # 面积列已变为 BLOB（无）亲和，旧小面积以精确十进制文本保存
        assert db._column_type(conn, "cells", "area") == "BLOB"
        assert db._column_type(conn, "result_parts", "overlap_area") == "BLOB"
        assert db._column_type(conn, "current_result", "total_households") == "BLOB"
        stored = conn.execute(
            "SELECT area, typeof(area) FROM cells WHERE cell_id='g1'"
        ).fetchone()
        assert stored[0] == "100" and stored[1] == "text"

        v1 = db.get_version(1, conn)
        assert [c["id"] for c in v1["items"]] == ["g1", "g2"]
        assert v1["items"][0]["area"] == 100  # 读回仍是精确整数
        assert v1["items"][0]["households"] == 7

        result = db.get_current_result(conn)
        assert result["generation_id"] == 1
        assert result["total_households"] == 18
        flat = {
            (x["district_id"], x["old_id"]): x
            for d in result["districts"]
            for x in d["detail"]
        }
        assert flat[("d1", "g1")]["overlap_area"] == 50
        assert flat[("d1", "g1")]["households"] == 4
        assert sum(d["allocated_households"] for d in result["districts"]) == 18
    finally:
        conn.close()

    # 模拟重启：再次打开不应重复迁移、数据依旧可读
    conn2 = db.get_conn(db_file)
    try:
        assert conn2.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert db.get_version(1, conn2)["items"][1]["area"] == 100
        assert db.get_current_result(conn2)["total_households"] == 18

        # 升级后的库支持超大面积新写入
        vid = db.insert_version(
            "old",
            [{"id": "big", "rect": (0, 0, N, N), "households": 3}],
            conn2,
        )
        conn2.commit()
        assert db.get_version(vid, conn2)["items"][0]["area"] == HUGE_AREA
        # 旧发布结果未被新写入改动
        assert db.get_current_result(conn2)["generation_id"] == 1
    finally:
        conn2.close()

    # 再重启一次：新老数据都在
    conn3 = db.get_conn(db_file)
    try:
        assert db.get_version(1, conn3)["items"][0]["area"] == 100
        assert db.get_version(3, conn3)["items"][0]["area"] == HUGE_AREA
        assert db.get_current_result(conn3)["generation_id"] == 1
        # 新写入的超大面积在 BLOB 亲和列里以 TEXT 存储，读回仍是精确整数
        kind = conn3.execute(
            "SELECT typeof(area) FROM cells WHERE cell_id='big'"
        ).fetchone()[0]
        assert kind == "text"
    finally:
        conn3.close()


def test_small_areas_stay_native_integer_huge_areas_become_text(tmp_path):
    """BLOB 亲和列：64 位以内的面积保持原生 INTEGER，超界才存为文本。"""
    conn = db.get_conn(tmp_path / "boundary.db")
    int64_max = INT64_MAX
    db.insert_version(
        "old",
        [{"id": "atmax", "rect": (0, 0, 1, int64_max), "households": 1}],
        conn,
    )
    db.insert_version(
        "old",
        [{"id": "over", "rect": (0, 0, 2, int64_max), "households": 1}],
        conn,
    )
    conn.commit()
    rows = conn.execute(
        "SELECT cell_id, area, typeof(area) FROM cells ORDER BY id"
    ).fetchall()
    assert rows[0][1] == int64_max and rows[0][2] == "integer"
    assert rows[1][1] == str(2 * int64_max) and rows[1][2] == "text"
    # 两种存储类读回都是精确 Python int
    v1 = db.list_versions("old", conn)
    assert v1[0]["items"][0]["area"] == int64_max
    assert v1[1]["items"][0]["area"] == 2 * int64_max
    conn.close()


def test_migration_is_idempotent(tmp_path):
    db_file = tmp_path / "legacy2.db"
    _build_legacy_db(db_file)
    for _ in range(3):
        conn = db.get_conn(db_file)
        conn.close()
    conn = db.get_conn(db_file)
    try:
        assert conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM result_parts").fetchone()[0] == 4
    finally:
        conn.close()


# ---------- 发布事务失败：回滚干净、旧发布不变 ----------

def test_publish_failure_rolls_back_half_details_and_keeps_publish(client, monkeypatch):
    # 先发布一份成功结果
    client.post("/old-grids", json=OLD_V1)
    client.post("/new-districts", json=NEW_V1)
    gen1 = poll_generation(client, client.post("/generations").json()["id"])
    assert gen1["status"] == "succeeded"
    good = client.get("/result").json()

    # 再生成一次，并在发布事务写入 current_result 时注入故障
    real_bind_int = db.bind_int

    def flaky_bind_int(value):
        # 总户数 18 只在 current_result 绑定时出现；明细面积为 50/100
        if value == 18:
            raise RuntimeError("injected publish failure")
        return real_bind_int(value)

    monkeypatch.setattr(db, "bind_int", flaky_bind_int)
    gen2 = poll_generation(client, client.post("/generations").json()["id"])
    monkeypatch.undo()
    assert gen2["status"] == "failed"
    assert "injected publish failure" in gen2["error"]

    # 失败任务没有留下任何明细（事务整体回滚）
    conn = db.get_conn()
    try:
        left = conn.execute(
            "SELECT COUNT(*) FROM result_parts WHERE generation_id=?",
            (gen2["id"],),
        ).fetchone()[0]
        assert left == 0
        # 任务状态已落终态，不会卡在 running
        row = conn.execute(
            "SELECT status FROM generations WHERE id=?", (gen2["id"],)
        ).fetchone()
        assert row[0] == "failed"
    finally:
        conn.close()

    # 当前发布仍是上一份结果，内容不变
    still = client.get("/result").json()
    assert still["generation_id"] == good["generation_id"]
    assert still["districts"] == good["districts"]
    assert still["total_households"] == good["total_households"]

    # 故障排除后新任务可正常发布并切换
    gen3 = poll_generation(client, client.post("/generations").json()["id"])
    assert gen3["status"] == "succeeded"
    assert client.get("/result").json()["generation_id"] == gen3["id"]
