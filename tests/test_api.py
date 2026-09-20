"""HTTP 接口测试（任务创建 / 结果查询 / 校验 / 健康检查）。"""


def _payload(**over):
    p = {
        "questions": [
            {"id": "Q1", "score": 5, "type": "单选",
             "knowledge": ["A"], "difficulty": 0.3},
            {"id": "Q2", "score": 5, "type": "单选",
             "knowledge": ["B"], "difficulty": 0.5},
            {"id": "Q3", "score": 10, "type": "解答",
             "knowledge": ["C"], "difficulty": 0.7},
            {"id": "Q4", "score": 10, "type": "解答",
             "knowledge": ["C"], "difficulty": 0.6},
        ],
        "total_score": 20,
        "type_counts": {"单选": 2, "解答": 1},
        "knowledge_coverage": ["A", "B", "C"],
        "difficulty_range": [0.0, 1.0],
        "mutex_groups": [],
    }
    p.update(over)
    return p


def test_health(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_create_and_query_success(create_and_wait):
    body = create_and_wait(_payload())
    assert body["status"] == "succeeded"
    r = body["result"]
    assert r["feasible"] is True
    assert r["total_score"] == 20
    assert r["type_counts"] == {"单选": 2, "解答": 1}
    assert len(r["question_ids"]) == 3
    # 同请求再跑一次 -> 可复现
    again = create_and_wait(_payload())
    assert again["result"]["question_ids"] == r["question_ids"]


def test_task_not_found(client):
    r = client.get("/api/papers/999999")
    assert r.status_code == 404


def test_status_lifecycle(client):
    r = client.post("/api/papers", json=_payload())
    assert r.status_code == 201
    tid = r.json()["task_id"]
    assert r.json()["status"] == "pending"
    # 立即查询应是 pending/running/succeeded 之一
    g = client.get(f"/api/papers/{tid}").json()
    assert g["status"] in ("pending", "running", "succeeded")


def test_infeasible_returns_mus(create_and_wait):
    body = create_and_wait(_payload(total_score=9999))
    assert body["status"] == "succeeded"
    r = body["result"]
    assert r["feasible"] is False
    assert r["reason"]
    mus = r["minimal_conflict"]
    assert len(mus) >= 1
    codes = [m["code"] for m in mus]
    assert "total_score" in codes
    for m in mus:
        assert m["description"]
        assert m["witness"]


def test_validation_duplicate_id(client):
    p = _payload()
    p["questions"][1] = dict(p["questions"][0])  # 重复 Q1
    r = client.post("/api/papers", json=p)
    assert r.status_code == 422


def test_validation_bad_difficulty_range(client):
    r = client.post("/api/papers", json=_payload(difficulty_range=[0.9, 0.1]))
    assert r.status_code == 422


def test_validation_unknown_mutex_member(client):
    r = client.post("/api/papers", json=_payload(mutex_groups=[["Q1", "ZZZ"]]))
    assert r.status_code == 422


def test_validation_type_stock_shortage(client):
    r = client.post(
        "/api/papers", json=_payload(type_counts={"单选": 99, "解答": 1})
    )
    assert r.status_code == 422


def test_validation_coverage_kp_missing(client):
    r = client.post(
        "/api/papers", json=_payload(knowledge_coverage=["NOPE"])
    )
    assert r.status_code == 422
