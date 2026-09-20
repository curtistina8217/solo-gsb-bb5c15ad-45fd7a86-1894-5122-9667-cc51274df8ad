# 智能组卷 API

面向教研组的智能组卷服务：提交**题库快照**与组卷约束（总分、各题型数量、知识点覆盖、难度区间、互斥题组），由 OR-Tools CP-SAT 生成**分值精确、约束满足、结果可复现**的试卷；无解时返回经**逐项删减验证**的最小冲突约束集。

## 启动（一条命令）

```bash
docker compose up --build
```

启动时自动安装依赖并在数据目录建库（SQLite）。

- **必要配置**（均有默认值，通过环境变量覆盖）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PAPER_DB_PATH` | `/data/papers.db` | SQLite 数据库路径（目录不存在自动创建） |
| `PAPER_SEED` | `20260920` | CP-SAT 随机种子，固定以保证可复现 |
| `PAPER_SOLVER_TIME_LIMIT` | `10` | 单次求解时间上限（秒） |

- **API 地址与端口**：`http://localhost:8000`（容器内监听 `0.0.0.0:8000`）；交互文档 `http://localhost:8000/docs`。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/papers` | 创建组卷任务，返回 `task_id`（201） |
| GET | `/api/papers/{task_id}` | 查询状态 `pending/running/succeeded/failed` 与结果 |
| GET | `/healthz` | 健康检查 |

### 创建任务请求体

```json
{
  "questions": [
    {"id": "Q1", "score": 5, "type": "单选", "knowledge": ["函数", "集合"], "difficulty": 0.2},
    {"id": "Q2", "score": 5, "type": "单选", "knowledge": ["几何"], "difficulty": 0.6}
  ],
  "total_score": 10,
  "type_counts": {"单选": 2},
  "knowledge_coverage": ["函数", "几何"],
  "difficulty_range": [0.0, 0.8],
  "mutex_groups": [["Q1", "Q2"]]
}
```

- `questions`：题库快照；`id` 快照内唯一，`score` 为正整数，`difficulty ∈ [0,1]`
- `total_score`：试卷总分（入选题目分值之和必须**精确相等**）
- `type_counts`：各题型入选精确数量（之和即试卷题数）
- `knowledge_coverage`：必须覆盖的知识点（每点至少一道入选题）
- `difficulty_range`：入选题目平均难度的闭区间 `[下界, 上界]`
- `mutex_groups`：互斥题组，同组至多选一道

### 择优规则（多组可行解时词典序择优）

1. 难度均值相对区间中点（理想难度）的绝对偏差最小
2. 试卷内重复知识点数最少（`Σ 知识点 max(0, 出现题数 − 1)`）
3. 被选题号按字符串升序排列后的序列词典序最小（结果稳定、便于比对）

### 成功结果

```json
{
  "task_id": 1,
  "status": "succeeded",
  "result": {
    "feasible": true,
    "question_ids": ["Q1", "Q3"],
    "total_score": 10,
    "type_counts": {"单选": 2},
    "knowledge_points": ["函数", "几何"],
    "repeated_knowledge_points": [],
    "difficulty_avg": 0.35,
    "ideal_difficulty": 0.4,
    "difficulty_deviation": 0.05,
    "objective_trace": { "...": "各级最优值与求解器状态" },
    "reproducibility": {
      "random_seed": 20260920,
      "num_search_workers": 1,
      "request_sha256": "..."
    }
  }
}
```

### 无解结果

`result.feasible = false`，`minimal_conflict` 为最小冲突约束集：集合整体不可满足、删除其中任意一项即恢复可行；每项附 `description` 与 `witness`（删除该项后一份可行试卷的题号序列，作为逐项删减验证的证据）。

## 技术栈

Python 3.11 · FastAPI · SQLite（WAL，标准库 `sqlite3`）· OR-Tools CP-SAT（固定种子、单线程搜索保证可复现）。
