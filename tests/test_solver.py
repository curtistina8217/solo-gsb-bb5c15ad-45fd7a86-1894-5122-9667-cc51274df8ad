"""求解器层单元测试。"""
from app.solver import prepare, solve

DIFF_EPS = 1e-4


def q(qid, score, qtype, kps, diff):
    return {
        "id": qid, "score": score, "type": qtype,
        "knowledge": list(kps), "difficulty": diff,
    }


def base_payload(**over):
    p = {
        "questions": [
            q("Q1", 5, "单选", ["A"], 0.20),
            q("Q2", 5, "单选", ["A"], 0.40),
            q("Q3", 5, "单选", ["B"], 0.40),
            q("Q4", 5, "单选", ["B"], 0.40),
            q("Q5", 10, "解答", ["C"], 0.60),
            q("Q6", 10, "解答", ["C"], 0.50),
        ],
        "total_score": 20,
        "type_counts": {"单选": 2, "解答": 1},
        "knowledge_coverage": ["A", "B", "C"],
        "difficulty_range": [0.0, 1.0],
        "mutex_groups": [],
    }
    p.update(over)
    return p


def test_feasible_exact_score_and_constraints():
    r = solve(base_payload())
    assert r["feasible"] is True
    assert r["total_score"] == 20
    assert r["type_counts"] == {"单选": 2, "解答": 1}
    assert len(r["question_ids"]) == 3
    assert set(["A", "B", "C"]).issubset(set(r["knowledge_points"]))
    lo, hi = 0.0, 1.0
    assert lo - DIFF_EPS <= r["difficulty_avg"] <= hi + DIFF_EPS
    # 理想难度 0.5；暴力枚举确认最优 Q2(0.4)+Q3(0.4)+Q5(0.6)=1.4，
    # 平均 0.4667，偏差 0.0333；并列时序列词典序最小。
    assert abs(r["difficulty_deviation"] - 0.033333) < 1e-3
    assert r["question_ids"] == ["Q2", "Q3", "Q5"]


def test_reproducible_same_snapshot_same_result():
    r1 = solve(base_payload())
    r2 = solve(base_payload())
    assert r1["question_ids"] == r2["question_ids"]
    assert r1["difficulty_avg"] == r2["difficulty_avg"]


def test_tiebreak_difficulty_first():
    # 第一优先级：平均难度贴近区间中点 0.5 的一组严格胜出
    questions = [
        q("A1", 5, "单选", ["X"], 0.0),
        q("A2", 5, "单选", ["X"], 0.0),
        q("B1", 5, "单选", ["Y"], 0.45),
        q("B2", 5, "单选", ["Y"], 0.55),
    ]
    p = {
        "questions": questions,
        "total_score": 10,
        "type_counts": {"单选": 2},
        "knowledge_coverage": [],
        "difficulty_range": [0.0, 1.0],
        "mutex_groups": [],
    }
    r = solve(p)
    # A 组均值 0.0（偏差 0.5），B 组均值 0.5（偏差 0）-> B 组胜，无视题号
    assert r["question_ids"] == ["B1", "B2"]
    assert abs(r["difficulty_deviation"]) < 1e-6


def test_tiebreak_repetition_then_sequence():
    # 难度都相同 -> 偏差并列；选 2 道：同知识点会产生重复
    questions = [
        q("T1", 5, "单选", ["K1"], 0.5),
        q("T2", 5, "单选", ["K2"], 0.5),
        q("T3", 5, "单选", ["K2"], 0.5),
        q("T4", 5, "单选", ["K3"], 0.5),
    ]
    p = {
        "questions": questions,
        "total_score": 10,
        "type_counts": {"单选": 2},
        "knowledge_coverage": [],
        "difficulty_range": [0.0, 1.0],
        "mutex_groups": [],
    }
    r = solve(p)
    # 最优可做到 0 重复；序列最小为 [T1, T2]
    assert r["repeated_knowledge_points"] == []
    assert r["question_ids"] == ["T1", "T2"]


def test_mutex_groups_enforced():
    p = base_payload(mutex_groups=[["Q1", "Q3", "Q6"]])
    r = solve(p)
    assert r["feasible"] is True
    chosen = set(r["question_ids"])
    assert len(chosen & {"Q1", "Q3", "Q6"}) <= 1


def test_infeasible_total_score_single_mus():
    # 题池总分上限 40，但要求 100 分 -> 仅总分约束冲突
    p = base_payload(total_score=100)
    r = solve(p)
    assert r["feasible"] is False
    codes = [c["code"] for c in r["minimal_conflict"]]
    assert codes == ["total_score"]
    item = r["minimal_conflict"][0]
    assert item["witness"] is not None
    # witness 是删除总分约束后的一份可行试卷
    assert len(item["witness"]) == 3


def test_infeasible_complex_mus_is_minimal():
    # 构造多约束联合冲突：
    # 仅 2 道单选题库存，却要求单选 2 道 + 总分 100
    # + 覆盖一个不存在于单选题的知识点（在解答题上）
    p = base_payload(
        total_score=100,
        knowledge_coverage=["A", "B", "C", "GHOST"],
    )
    p["questions"] = [
        q("Q1", 5, "单选", ["A"], 0.2),
        q("Q2", 5, "单选", ["A"], 0.4),
        q("Q5", 10, "解答", ["C"], 0.6),
        q("Q6", 10, "解答", ["C"], 0.5),
        q("QG", 5, "单选", ["GHOST"], 0.5),
    ]
    # 单选库存 3（Q1,Q2,QG），要求 2；加上 GHOST 覆盖会迫使 QG 入选
    p["type_counts"] = {"单选": 2, "解答": 1}
    # 总分 100 永远不可达（全选 35 分）；其余约束彼此可同时满足
    # -> MUS 应只包含 total_score（其它约束合取可行）
    r = solve(p)
    assert r["feasible"] is False
    codes = [c["code"] for c in r["minimal_conflict"]]
    assert codes == ["total_score"]


def test_infeasible_mutex_vs_coverage_mus():
    # 互斥组成员是某知识点的仅来源 -> 覆盖与互斥联合冲突
    p = base_payload()
    p["questions"] = [
        q("Q1", 5, "单选", ["RARE"], 0.5),
        q("Q2", 5, "单选", ["RARE"], 0.5),
        q("Q3", 5, "单选", ["B"], 0.5),
        q("Q4", 5, "单选", ["B"], 0.5),
        q("Q5", 10, "解答", ["C"], 0.5),
        q("Q6", 10, "解答", ["C"], 0.5),
    ]
    p["knowledge_coverage"] = ["RARE", "B", "C"]
    p["mutex_groups"] = [["Q1", "Q2"]]
    # 需选 2 单选 1 解答；单选须覆盖 RARE 和 B，
    # Q1,Q2 互斥仍可只选其中一个 + 一道 B 题 -> 可行
    r = solve(p)
    assert r["feasible"] is True
    chosen = set(r["question_ids"])
    assert len(chosen & {"Q1", "Q2"}) == 1

    # 升级冲突：仅需 1 道单选，但要求同时覆盖 RARE 和 B
    p["type_counts"] = {"单选": 1, "解答": 1}
    p["total_score"] = 15
    r = solve(p)
    assert r["feasible"] is False
    codes = {c["code"] for c in r["minimal_conflict"]}
    # 单选数量为 1 与 RARE/B 两个覆盖约束存在冲突，MUS 必包含其中一组
    assert any(c.startswith("coverage:") for c in codes)
    # 每项必须有见证（删它即可行）
    for item in r["minimal_conflict"]:
        assert item["witness"] is not None
        assert len(item["witness"]) == 2  # 共选 2 道


def test_mus_every_item_witnessed():
    p = base_payload(total_score=9999)
    r = solve(p)
    assert r["feasible"] is False
    assert len(r["minimal_conflict"]) >= 1
    for item in r["minimal_conflict"]:
        assert item["required"] is True
        assert item["removable"] is True
        assert item["witness"], f"{item['code']} 缺少删减见证"


def test_prepare_deterministic_order():
    norm = prepare(base_payload())
    ids_sorted = [norm.questions[i].id for i in norm.order]
    assert ids_sorted == sorted(ids_sorted)
