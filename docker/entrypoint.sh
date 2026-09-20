#!/bin/sh
# 容器启动钩子：
#   1) 依赖未安装（或不完整）时自动 pip 安装 requirements.txt
#   2) 自动建库建表（应用 startup 中执行，此处先确保 /data 可写）
set -e

DATA_DIR="$(dirname "${PAPER_DB_PATH:-/data/papers.db}")"
mkdir -p "$DATA_DIR"

# 快速探活：任一关键依赖不可导入则执行安装
if ! python -c "import fastapi, uvicorn, ortools, pydantic" >/dev/null 2>&1; then
    echo "[entrypoint] 正在安装依赖..."
    pip install --no-cache-dir -r /app/requirements.txt
else
    echo "[entrypoint] 依赖已就绪"
fi

echo "[entrypoint] 启动服务（首次启动将自动建库）..."
exec "$@"
