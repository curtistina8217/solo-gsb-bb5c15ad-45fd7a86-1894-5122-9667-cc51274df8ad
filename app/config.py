"""运行配置：全部可通过环境变量覆盖。"""
import os

# SQLite 数据库文件路径（容器内默认 /data/papers.db，启动时自动建库）
DB_PATH = os.environ.get("PAPER_DB_PATH", "/data/papers.db")

# CP-SAT 固定随机种子，保证结果可复现
SEED = int(os.environ.get("PAPER_SEED", "20260920"))

# CP-SAT 每次求解的时间上限（秒）。未触顶时结果与机器无关、完全可复现
SOLVER_TIME_LIMIT = float(os.environ.get("PAPER_SOLVER_TIME_LIMIT", "10"))

# 工作线程数：固定单线程，消除并行搜索带来的不确定性
SOLVER_NUM_WORKERS = int(os.environ.get("PAPER_SOLVER_NUM_WORKERS", "1"))
