FROM python:3.11-slim

# 容器启动时自动安装依赖所需
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
COPY app /app/app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 数据卷：SQLite 数据库存放处（默认 PAPER_DB_PATH=/data/papers.db）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
