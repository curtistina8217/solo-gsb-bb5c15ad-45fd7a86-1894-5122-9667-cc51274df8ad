"""智能组卷 API（FastAPI）。

接口：
  POST /api/papers         创建组卷任务（提交题库快照与约束）
  GET  /api/papers/{id}    查询任务状态与结果
  GET  /healthz            健康检查

任务在后台守护线程执行；求解器同进程调用，SQLite 记录全生命周期。
"""
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException

from . import db
from .schemas import PaperRequest
from .solver import SolverError, solve


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # 进程重启后，将上次未完成的任务重新入队
    for tid in db.pending_task_ids():
        _spawn(tid)
    yield


app = FastAPI(
    title="智能组卷 API",
    description="面向教研组：提交题库快照与约束，CP-SAT 生成分值精确、"
    "约束满足、结果可复现的试卷；无解时返回最小冲突约束集。",
    version="1.0.0",
    lifespan=lifespan,
)


def _process(task_id: int) -> None:
    task = db.get_task(task_id)
    if task is None:
        return
    db.mark_running(task_id)
    try:
        result = solve(task["request"])
        db.finish_task(task_id, result)
    except SolverError as e:
        db.fail_task(task_id, str(e))
    except Exception as e:  # pragma: no cover - 防御性
        db.fail_task(task_id, f"求解失败: {e!r}")


def _spawn(task_id: int) -> None:
    import threading

    t = threading.Thread(target=_process, args=(task_id,), daemon=True)
    t.start()


@app.post("/api/papers", status_code=201)
def create_paper(req: PaperRequest, bg: BackgroundTasks):
    payload = req.model_dump(mode="json")
    task_id = db.create_task(payload)
    bg.add_task(_spawn, task_id)
    return {"task_id": task_id, "status": "pending"}


@app.get("/api/papers/{task_id}")
def get_paper(task_id: int):
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    body = {
        "task_id": task["id"],
        "status": task["status"],  # pending / running / succeeded / failed
        "result": task["result"],
        "error": task["error"],
    }
    if task["status"] == "succeeded":
        r = task["result"]
        if r.get("feasible"):
            body["result"] = r
        else:
            body["result"] = r  # 含 minimal_conflict 的无解报告
    return body


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
