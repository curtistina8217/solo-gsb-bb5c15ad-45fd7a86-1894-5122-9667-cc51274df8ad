"""API 数据模型（Pydantic v2）。

请求级语义校验在此完成（题号唯一、分值为正、题型数量自洽、难度区间合法、
知识点存在性、互斥题组成员存在），不合法请求返回 422。
"""
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class QuestionIn(BaseModel):
    id: str = Field(..., min_length=1, description="题目编号（题库快照内唯一）")
    score: int = Field(..., gt=0, description="题目分值（正整数）")
    type: str = Field(..., min_length=1, description="题型，如 单选/多选/填空/解答")
    knowledge: list[str] = Field(default_factory=list, description="该题覆盖的知识点")
    difficulty: float = Field(..., ge=0.0, le=1.0, description="难度系数，范围 [0,1]")

    @field_validator("knowledge")
    @classmethod
    def _dedup_knowledge(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))  # 去重但保序


class PaperRequest(BaseModel):
    questions: list[QuestionIn] = Field(..., min_length=1, description="题库快照")
    total_score: int = Field(..., gt=0, description="试卷总分（被选题目分值之和）")
    type_counts: dict[str, int] = Field(
        default_factory=dict,
        description='各题型精确数量，如 {"单选": 10, "解答": 3}',
    )
    knowledge_coverage: list[str] = Field(
        default_factory=list,
        description="试卷必须覆盖的知识点（每点至少出现一道被选题）",
    )
    difficulty_range: tuple[float, float] = Field(
        ..., description="被选题难度均值允许的闭区间 [下界, 上界]，范围 [0,1]"
    )
    mutex_groups: list[list[str]] = Field(
        default_factory=list,
        description="互斥题组：同组题目至多入选一道",
    )

    @model_validator(mode="after")
    def _check_semantics(self) -> "PaperRequest":
        qs = self.questions

        # 题号唯一
        ids = [q.id for q in qs]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"题库快照中题号重复: {sorted(dup)}")

        # 题型数量必须非负
        for t, c in self.type_counts.items():
            if c < 0:
                raise ValueError(f"题型 {t} 的数量不能为负: {c}")

        # 难度区间合法
        lo, hi = self.difficulty_range
        if lo > hi:
            raise ValueError(f"难度区间下界 {lo} 不能大于上界 {hi}")

        # 总题数
        m = sum(self.type_counts.values())
        if m > len(qs):
            raise ValueError(f"题型数量之和 {m} 超过题库题数 {len(qs)}")

        id_set = set(ids)

        # 每题型库存足够
        stock: dict[str, int] = {}
        for q in qs:
            stock[q.type] = stock.get(q.type, 0) + 1
        for t, c in self.type_counts.items():
            if stock.get(t, 0) < c:
                raise ValueError(f"题型 {t} 需要 {c} 道，题库仅有 {stock.get(t, 0)} 道")

        # 覆盖知识点必须在题库中出现
        known: set[str] = set()
        for q in qs:
            known.update(q.knowledge)
        missing = [k for k in self.knowledge_coverage if k not in known]
        if missing:
            raise ValueError(f"知识点在题库中不存在: {missing}")

        # 互斥题组校验
        for gi, g in enumerate(self.mutex_groups):
            if len(g) < 2:
                raise ValueError(f"第 {gi} 组互斥题至少需包含 2 道题")
            absent = [i for i in g if i not in id_set]
            if absent:
                raise ValueError(f"第 {gi} 组互斥题包含不存在的题号: {absent}")

        return self


class TaskCreateResponse(BaseModel):
    task_id: int
    status: str


class Optionality(BaseModel):
    code: str
    description: str
    required: bool
    removable: bool
    witness: Optional[list[str]] = Field(
        default=None,
        description="删除该约束后可行的一组题号（证明它确属最小冲突的组成部分）",
    )


class NoSolutionResult(BaseModel):
    feasible: bool = False
    reason: str
    minimal_conflict: list[Optionality] = Field(
        ..., description="逐项删减验证的最小冲突约束集（MUS）"
    )


class PaperResult(BaseModel):
    feasible: bool = True
    question_ids: list[str]
    total_score: int
    type_counts: dict[str, int]
    knowledge_points: list[str]
    repeated_knowledge_points: list[str]
    difficulty_avg: float
    ideal_difficulty: float
    difficulty_deviation: float
    objective_trace: dict
    reproducibility: dict
    solving_time_ms: int


class TaskStatusResponse(BaseModel):
    task_id: int
    status: str
    error: Optional[str] = None
    result: Optional[dict] = None
