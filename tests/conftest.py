import os
import shutil

# 必须在导入 app.* 之前设置：测试用独立 SQLite 文件
_TEST_DB = "/tmp/test_papers_api.db"
shutil.rmtree(_TEST_DB, ignore_errors=True)
for suffix in ("-wal", "-shm"):
    shutil.rmtree(_TEST_DB + suffix, ignore_errors=True)
os.environ["PAPER_DB_PATH"] = _TEST_DB
os.environ["PAPER_SEED"] = "20260920"
os.environ["PAPER_SOLVER_TIME_LIMIT"] = "5"
os.environ["PAPER_SOLVER_NUM_WORKERS"] = "1"

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


def poll(client, task_id, timeout=30.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/papers/{task_id}")
        assert r.status_code == 200
        body = r.json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError("任务超时未完成")


@pytest.fixture
def create_and_wait(client):
    def _f(payload):
        r = client.post("/api/papers", json=payload)
        assert r.status_code == 201, r.text
        task_id = r.json()["task_id"]
        return poll(client, task_id)

    return _f
