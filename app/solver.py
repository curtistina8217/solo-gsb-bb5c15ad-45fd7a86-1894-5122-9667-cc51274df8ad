"""智能组卷求解核心（OR-Tools CP-SAT）。

词典序三级择优（逐级固定上级最优值后求下级）：
  1. 难度均值与理想难度（区间中点）的绝对偏差最小；
  2. 试卷内重复知识点数最小（sum_k max(0, 入选题中含 k 的题数 - 1)）；
  3. 被选题号序列的词典序最小（按题号字符串排序后的最小 m 元组）。

无可行解时，用 deletion-based 方法提取最小冲突约束集（MUS）：
逐项尝试删减，删后仍不可行则该约束被删去；删后可行则保留并记录一份
可行试卷作为"该项确属冲突成因"的见证（witness）。最终核集中每一项都
经过逐项删减验证，且任何单项被删去后都存在可行解（最小性）。

固定种子 + 单线程搜索；在求解时间未触顶时，同一份快照必然得到同一结果。
"""
import hashlib
import json
import time
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from . import config

SCALE = 1000  # 难度浮点 -> 整数的放大倍数（0.001 精度，兼顾求解速度）


class SolverError(RuntimeError):
    """求解器异常（如时间预算耗尽仍无法得出结论）。"""


@dataclass
class Q:
    idx: int           # 在快照中的原始下标
    order: int         # 按题号字符串排序后的位置
    id: str
    score: int
    type: str
    knowledge: frozenset[str]
    difficulty: int    # 放大后的整数难度


@dataclass
class Item:
    """一条可删减的硬性约束，用于 MUS 提取。"""
    code: str
    description: str
    fn: object  # (model, x, norm) -> None


@dataclass
class Norm:
    questions: list[Q]
    order: list[int]               # 题号升序位置 -> 快照下标
    n: int
    m: int
    total_score: int
    type_counts: dict[str, int]
    coverage: list[str]
    mutex_groups: list[list[int]]
    lo: int
    hi: int
    ideal: int                     # 放大后的理想难度（区间中点）
    items: list[Item] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------

def prepare(payload: dict) -> Norm:
    """把已通过 schema 校验的请求 dict 规范化为求解器内部结构。"""
    raw = payload["questions"]
    # 题号字符串排序；极端重名（schema 已禁止）下以快照下标兜底，保证全序
    order = sorted(range(len(raw)), key=lambda i: (raw[i]["id"], i))
    pos = {idx: p for p, idx in enumerate(order)}

    qs: list[Q] = []
    for i, q in enumerate(raw):
        qs.append(
            Q(
                idx=i,
                order=pos[i],
                id=q["id"],
                score=int(q["score"]),
                type=q["type"],
                knowledge=frozenset(q.get("knowledge", [])),
                difficulty=int(round(float(q["difficulty"]) * SCALE)),
            )
        )

    lo, hi = payload["difficulty_range"]
    type_counts = {t: int(c) for t, c in payload["type_counts"].items()}
    m = sum(type_counts.values())
    ideal = int(round(((float(lo) + float(hi)) / 2.0) * SCALE))

    id_to_idx = {q.id: q.idx for q in qs}
    mutex_groups = [
        [id_to_idx[qid] for qid in grp] for grp in payload.get("mutex_groups", [])
    ]

    norm = Norm(
        questions=qs,
        order=order,
        n=len(qs),
        m=m,
        total_score=int(payload["total_score"]),
        type_counts=type_counts,
        coverage=list(payload.get("knowledge_coverage", [])),
        mutex_groups=mutex_groups,
        lo=int(round(float(lo) * SCALE)),
        hi=int(round(float(hi) * SCALE)),
        ideal=ideal,
    )
    norm.items = _build_items(norm)
    return norm


def _build_items(norm: Norm) -> list[Item]:
    """可删减约束清单。删减从此顺序首项开始（先尝试最"软"的约束）。"""
    items: list[Item] = []

    # 1) 知识点覆盖
    def make_cover(kp):
        def add(model, x, nm):
            model.Add(sum(x[q.idx] for q in nm.questions if kp in q.knowledge) >= 1)
        return add

    for kp in norm.coverage:
        items.append(
            Item(
                code=f"coverage:{kp}",
                description=f"知识点必须覆盖：{kp}",
                fn=make_cover(kp),
            )
        )

    # 2) 互斥题组
    for gi, grp in enumerate(norm.mutex_groups):
        ids = [norm.questions[i].id for i in grp]

        def make_mutex(members):
            def add(model, x, nm):
                model.Add(sum(x[i] for i in members) <= 1)
            return add

        items.append(
            Item(
                code=f"mutex:{gi}",
                description=f"互斥题组（同组至多选 1 道）：{' / '.join(ids)}",
                fn=make_mutex(grp),
            )
        )

    # 3) 难度区间（被选题难度均值 ∈ [lo, hi]）
    # dsum = Σ d_i，d_i 已是 SCALE 量纲；平均 = dsum/m，
    # 故 lo*m ≤ dsum ≤ hi*m（同为 SCALE 量纲，无需除法或额外放大）。
    def add_difficulty(model, x, nm):
        if nm.m == 0:
            return
        dsum = model.NewIntVar(0, nm.m * SCALE, "dsum_diff")
        model.Add(dsum == sum(q.difficulty * x[q.idx] for q in nm.questions))
        model.Add(dsum >= nm.lo * nm.m)
        model.Add(dsum <= nm.hi * nm.m)

    items.append(
        Item(
            code="difficulty_range",
            description=(
                f"被选题平均难度须落在 [{_fmt(norm.lo)}, {_fmt(norm.hi)}] 闭区间"
            ),
            fn=add_difficulty,
        )
    )

    # 4) 各题型精确数量
    def make_type(t, c):
        def add(model, x, nm):
            model.Add(sum(x[q.idx] for q in nm.questions if q.type == t) == c)
        return add

    for t, c in norm.type_counts.items():
        items.append(
            Item(
                code=f"type_count:{t}",
                description=f"题型【{t}】入选数量 = {c}",
                fn=make_type(t, c),
            )
        )

    # 5) 总分精确
    def add_total(model, x, nm):
        model.Add(sum(q.score * x[q.idx] for q in nm.questions) == nm.total_score)

    items.append(
        Item(
            code="total_score",
            description=f"试卷总分 = {norm.total_score} 分（分值精确）",
            fn=add_total,
        )
    )
    return items


def _fmt(v: int) -> str:
    return f"{v / SCALE:.4f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# 模型构建
# ---------------------------------------------------------------------------

def _build_base(norm: Norm, items: list[Item]):
    """基础 0/1 选题模型。结构约束 sum(x)=m 始终保留、不可删减。"""
    model = cp_model.CpModel()
    x = [model.NewBoolVar(f"x_{i}") for i in range(norm.n)]
    model.Add(sum(x) == norm.m)
    for it in items:
        it.fn(model, x, norm)
    return model, x


def _all_knowledge_points(norm: Norm) -> list[str]:
    return sorted({k for q in norm.questions for k in q.knowledge})


def _add_difficulty_and_repetition(model, x, norm: Norm):
    """在已有 x 的模型上追加难度偏差变量 e 与知识点重复变量 r。"""
    dsum = model.NewIntVar(0, norm.n * SCALE, "dsum")
    model.Add(dsum == sum(q.difficulty * x[q.idx] for q in norm.questions))
    target = norm.ideal * norm.m
    # dev = |dsum - target|，两者同为 SCALE 量纲；m 为常数，最小化它
    # 等价于最小化平均难度与理想难度的绝对偏差。
    dev = model.NewIntVar(0, norm.m * SCALE, "dev")
    model.Add(dsum - target <= dev)
    model.Add(target - dsum <= dev)

    terms = []
    for kp in _all_knowledge_points(norm):
        c = model.NewIntVar(0, norm.m, f"c_{kp}")
        model.Add(c == sum(x[q.idx] for q in norm.questions if kp in q.knowledge))
        # max(c-1, 0)：先物化为变量，避免裸表达式与常量混合取 max 的语义问题
        cm1 = model.NewIntVar(-1, norm.m - 1, f"cm1_{kp}")
        model.Add(cm1 == c - 1)
        excess = model.NewIntVar(0, norm.m, f"e_{kp}")
        model.AddMaxEquality(excess, [cm1, 0])
        terms.append(excess)
    # 重复知识点数：每个被覆盖知识点贡献 (入选题数 - 1)。
    # 一题可含多个知识点，紧上界为 入选题知识点出现总次数 - m
    # （所有入选题知识点集合完全相同的极端情况）。
    kp_occ = sum(len(q.knowledge) for q in norm.questions)
    rep_upper = max(0, kp_occ - norm.m)
    rep = model.NewIntVar(0, rep_upper, "rep")
    model.Add(rep == sum(terms))
    return dev, rep


def _new_solver():
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = config.SOLVER_TIME_LIMIT
    solver.parameters.num_search_workers = config.SOLVER_NUM_WORKERS
    solver.parameters.random_seed = config.SEED
    solver.parameters.log_search_progress = False
    return solver


def _add_hints(model, x, chosen: set[int]):
    for i, var in enumerate(x):
        model.AddHint(var, 1 if i in chosen else 0)


def _chosen_set(solver, x) -> set[int]:
    return {i for i, var in enumerate(x) if solver.Value(var) == 1}


def _run(model, what: str, *, need_optimal: bool):
    """返回 (solver 或 None, 是否最优)；INFEASIBLE 返回 (None, False)。"""
    solver = _new_solver()
    status = solver.Solve(model)
    if status == cp_model.OPTIMAL:
        return solver, True
    if status == cp_model.FEASIBLE and not need_optimal:
        return solver, False
    if status == cp_model.INFEASIBLE:
        return None, False
    raise SolverError(
        f"{what}阶段求解器在 {config.SOLVER_TIME_LIMIT}s 内未能得出结论"
        f"（status={solver.StatusName(status)}），请调大 PAPER_SOLVER_TIME_LIMIT"
    )


def _feasible(norm: Norm, items: list[Item], hint: set[int] | None = None):
    """返回 (是否可行, 入选快照下标集合)。"""
    model, x = _build_base(norm, items)
    if hint is not None:
        _add_hints(model, x, hint)
    solver, _ = _run(model, "可行性判定", need_optimal=False)
    if solver is None:
        return False, None
    return True, _chosen_set(solver, x)


# ---------------------------------------------------------------------------
# 最小冲突约束集（deletion-based MUS）
# ---------------------------------------------------------------------------

def _minimal_conflict(norm: Norm) -> list[dict]:
    """逐项删减验证。

    对每条约束尝试将其从当前核集中删去：删后仍不可行 -> 它不是必要成因，
    永久移除；删后可行 -> 保留，并把这份可行试卷记为该约束的见证。
    """
    core = list(norm.items)
    witnesses: dict[str, list[str]] = {}
    hint: set[int] | None = None

    for item in norm.items:
        trial = [it for it in core if it.code != item.code]
        ok, chosen = _feasible(norm, trial, hint)
        if not ok:
            core = trial
        else:
            hint = chosen  # 用已有可行解提示求解器，加速后续判定
            witnesses[item.code] = [
                norm.questions[i].id for i in norm.order if i in chosen
            ]

    return [
        {
            "code": it.code,
            "description": it.description,
            "required": True,
            "removable": True,
            "witness": witnesses.get(it.code),
        }
        for it in core
    ]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def solve(payload: dict) -> dict:
    started = time.perf_counter()
    norm = prepare(payload)
    statuses: dict[str, str] = {}
    timed_out_phases: list[str] = []

    # 整模型可行性
    ok, hint_set = _feasible(norm, norm.items)
    if not ok:
        mus = _minimal_conflict(norm)
        return {
            "feasible": False,
            "reason": "不存在满足全部硬性约束的试卷",
            "minimal_conflict": mus,
            "solving_time_ms": int((time.perf_counter() - started) * 1000),
        }

    # --- 阶段 1：最小化难度偏差 ---
    # 严格词典序：每一级单独求最优并固定，避免大权重合并目标带来的
    # CP-SAT 数值松弛问题。
    model, x = _build_base(norm, norm.items)
    dev, rep = _add_difficulty_and_repetition(model, x, norm)
    model.Minimize(dev)
    _add_hints(model, x, hint_set)
    solver, optimal = _run(model, "难度偏差优化", need_optimal=False)
    statuses["phase1_difficulty_deviation"] = solver.StatusName()
    if not optimal:
        timed_out_phases.append("phase1")
    best_dev = solver.Value(dev)
    hint_set = _chosen_set(solver, x)

    # --- 阶段 2：固定难度偏差最优值，最小化重复知识点数 ---
    model, x = _build_base(norm, norm.items)
    dev, rep = _add_difficulty_and_repetition(model, x, norm)
    model.Add(dev <= best_dev)
    model.Minimize(rep)
    _add_hints(model, x, hint_set)
    solver, optimal = _run(model, "知识点重复优化", need_optimal=False)
    statuses["phase2_repeated_knowledge"] = solver.StatusName()
    if not optimal:
        timed_out_phases.append("phase2")
    best_rep = solver.Value(rep)
    hint_set = _chosen_set(solver, x)

    # --- 阶段 3：固定前两级最优，求题号序列词典序最小 ---
    # 三种规模路径，见下方分支注释。
    model, x = _build_base(norm, norm.items)
    d, r = _add_difficulty_and_repetition(model, x, norm)
    model.Add(d <= best_dev)
    model.Add(r <= best_rep)

    n, m = norm.n, norm.m
    skips = n - m  # 未入选题数
    # 精确词典序需要把序列编码为单个整数（系数须在 int64 <= 2^62 内）：
    #  A) b_k 段编码（入选位置）：m 段、每段 B=ceil(log2(n-m+1)) 位，
    #     适合选题稀疏（m 小）；skip = Σ b_k·2^{(m-1-k)B}，最小化。
    #  B) 跳过位置段编码：skips 段、每段 ceil(log2(n+1)) 位，
    #     适合选题密集（n-m 小）；最大化跳过位置序列即最小化入选序列。
    #  C) 两者系数都超 int64（题池与试卷都很大）时退而采用阶段 1/2 的
    #     解（前两级严格最优、硬约束全满足），第三级如实标注 SATISFICING。
    seg_bits_b = max(1, (n - m + 1).bit_length())
    use_b_encoding = m * seg_bits_b <= 62
    use_skip_encoding = (not use_b_encoding) and skips <= 62 and skips > 0

    if use_b_encoding:
        y_prefix = []
        for j in range(n):
            yj = model.NewIntVar(0, m, f"yp_{j}")
            if j == 0:
                model.Add(yj == x[norm.order[0]])
            else:
                model.Add(yj == y_prefix[j - 1] + x[norm.order[j]])
            y_prefix.append(yj)

        seg_vars = []
        for k in range(m):
            indicators = []
            for j in range(n):
                le = model.NewBoolVar(f"le_{k}_{j}")
                model.Add(y_prefix[j] <= k).OnlyEnforceIf(le)
                model.Add(y_prefix[j] > k).OnlyEnforceIf(le.Not())
                indicators.append(le)
            s_k = model.NewIntVar(0, n - 1, f"sk_{k}")
            model.Add(s_k == sum(indicators))
            b_k = model.NewIntVar(0, n - m, f"bk_{k}")
            model.Add(b_k == s_k - k)
            seg_vars.append(b_k)

        base = 1 << seg_bits_b
        skip = model.NewIntVar(0, base**m - 1, "skip")
        model.Add(
            skip
            == sum(
                seg_vars[k] * (1 << ((m - 1 - k) * seg_bits_b))
                for k in range(m)
            )
        )
        model.Minimize(skip)
        _add_hints(model, x, hint_set)
        solver, optimal = _run(model, "题号序列优化", need_optimal=False)
        statuses["phase3_id_sequence"] = solver.StatusName()
        if not optimal:
            timed_out_phases.append("phase3")
        fixed_in = _chosen_set(solver, x)

    elif use_skip_encoding:
        # 稀疏跳过：设第 t 个被跳过位置为 u_t，最大化被选等价于最小化
        # 跳过位置序列 (u_0,...,u_{S-1}) 的词典序（越早跳过越"小"，
        # 但我们希望被选尽量靠前 = 跳过尽量靠后 = 跳过序列词典序最大）。
        # 用前缀计数 z_j = 位置 j 及之前跳过数；第 t 个跳过位置
        # u_t = #{j : z_j <= t}。编码为 S 段、每段 ceil(log2(n+1)) 位。
        seg_bits = max(1, n.bit_length())
        assert skips * seg_bits <= 62
        z_prefix = []
        for j in range(n):
            zj = model.NewIntVar(0, skips, f"zp_{j}")
            skipped = 1 - x[norm.order[j]]
            if j == 0:
                model.Add(zj == skipped)
            else:
                model.Add(zj == z_prefix[j - 1] + skipped)
            z_prefix.append(zj)
        seg = []
        for t in range(skips):
            inds = []
            for j in range(n):
                le = model.NewBoolVar(f"zl_{t}_{j}")
                model.Add(z_prefix[j] <= t).OnlyEnforceIf(le)
                model.Add(z_prefix[j] > t).OnlyEnforceIf(le.Not())
                inds.append(le)
            u_t = model.NewIntVar(0, n - 1, f"u_{t}")
            model.Add(u_t == sum(inds))
            seg.append(u_t)
        base = 1 << seg_bits
        target = model.NewIntVar(0, base**skips - 1, "skipseq")
        model.Add(
            target
            == sum(
                seg[t] * (1 << ((skips - 1 - t) * seg_bits))
                for t in range(skips)
            )
        )
        model.Maximize(target)  # 跳过位置尽量靠后 <=> 被选尽量靠前
        _add_hints(model, x, hint_set)
        solver, optimal = _run(model, "题号序列优化(稀疏跳过)", need_optimal=False)
        statuses["phase3_id_sequence"] = solver.StatusName()
        if not optimal:
            timed_out_phases.append("phase3")
        fixed_in = _chosen_set(solver, x)

    else:
        # 超大题池（精确 lex 编码系数超 int64）：逐位贪心的不可行性证明
        # 在此规模代价过高，故直接采用阶段 1/2 已求得的解——它严格满足
        # 全部硬约束且前两级（难度偏差、知识点重复）最优，仅第三级"题号
        # 序列"不保证词典序最小。如实标注，调用方可据此知晓权衡。
        timed_out_phases.append("phase3_skipped_oversized_bank")
        statuses["phase3_id_sequence"] = "SATISFICING"
        fixed_in = set(hint_set)

    chosen = sorted(fixed_in, key=lambda i: norm.questions[i].order)
    return _format_result(
        norm, chosen, best_dev, best_rep, statuses, timed_out_phases,
        payload, started,
    )


def _format_result(norm, chosen, best_dev, best_rep, statuses,
                   timed_out_phases, payload, started) -> dict:
    sel = [norm.questions[i] for i in chosen]
    chosen_ids = [q.id for q in sel]

    type_counts_out: dict[str, int] = {}
    for q in sel:
        type_counts_out[q.type] = type_counts_out.get(q.type, 0) + 1

    kp_count: dict[str, int] = {}
    for q in sel:
        for k in q.knowledge:
            kp_count[k] = kp_count.get(k, 0) + 1
    kps = sorted(kp_count)
    repeated = sorted(k for k, c in kp_count.items() if c >= 2)

    dsum_val = sum(q.difficulty for q in sel)
    avg_scaled = dsum_val / norm.m if norm.m else 0.0
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    return {
        "feasible": True,
        "question_ids": chosen_ids,
        "total_score": sum(q.score for q in sel),
        "type_counts": type_counts_out,
        "knowledge_points": kps,
        "repeated_knowledge_points": repeated,
        "difficulty_avg": round(avg_scaled / SCALE, 6),
        "ideal_difficulty": round(norm.ideal / SCALE, 6),
        "difficulty_deviation": round(abs(avg_scaled - norm.ideal) / SCALE, 6),
        "objective_trace": {
            "phase1_min_difficulty_deviation": round(
                best_dev / (SCALE * norm.m), 6
            ) if norm.m else 0.0,
            "phase2_min_repeated_knowledge_count": best_rep,
            "phase3_min_id_sequence": chosen_ids,
            "tie_break_order": [
                "difficulty_deviation",
                "repeated_knowledge_count",
                "question_id_sequence",
            ],
            "solver_status": statuses,
            "time_limit_hit": bool(timed_out_phases),
        },
        "reproducibility": {
            "solver": "OR-Tools CP-SAT",
            "solver_version": _ortools_version(),
            "random_seed": config.SEED,
            "num_search_workers": config.SOLVER_NUM_WORKERS,
            "time_limit_seconds": config.SOLVER_TIME_LIMIT,
            "request_sha256": fingerprint,
            "note": "同一份请求（含题库快照）在时间未触顶时结果完全可复现",
        },
        "snapshot_size": norm.n,
        "solving_time_ms": int((time.perf_counter() - started) * 1000),
    }


def _ortools_version() -> str:
    try:
        from ortools import __version__ as v
        return v
    except Exception:
        return "unknown"
