# 破晓 Dawn · 科研全流程智能体
# 零第三方依赖：后端纯 Python 标准库，无需 pip install
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Dawn Research Agent"
LABEL org.opencontainers.image.description="破晓 Dawn 科研全流程智能体（论文全流程 Web 助手）"

WORKDIR /app

# 只复制自包含的应用目录（data 运行时通过卷持久化）
COPY skills/research-agent/app/ /app/

# 数据目录（首次启动自动创建管理员 admin / Aa123456）
RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/', timeout=4)" || exit 1

CMD ["python", "web_app.py", "--host", "0.0.0.0", "--port", "8787", "--no-browser"]
