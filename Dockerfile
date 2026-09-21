FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/data \
    GATEWAY_PORT=8000

WORKDIR /app

# 依赖单独一层，改代码不必重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# 数据库 / 密钥 / 临时素材都落在 /data（务必挂卷持久化）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 用 python 探活，避免额外安装 curl
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('GATEWAY_PORT','8000')+'/health', timeout=4).status==200 else 1)"

# 单进程 uvicorn：worker 是进程内后台任务，不要用多 worker
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${GATEWAY_PORT} --no-access-log"]
