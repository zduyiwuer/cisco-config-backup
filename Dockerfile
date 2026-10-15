# ============================================================
#  Cisco 配置备份中心
#  多阶段构建：builder 装依赖，runtime 只带运行所需文件
# ============================================================

# ---------- 构建阶段 ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# paramiko 需要编译工具与底层库
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libc6-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# ---------- 运行阶段 ----------
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Cisco 配置备份中心" \
      org.opencontainers.image.description="Cisco 交换机配置备份与版本管理（SSH/Telnet + AD 认证）" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PATH="/opt/venv/bin:$PATH"

# openssh-client 用于连通性排查，curl 供健康检查
RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client curl tzdata ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 非root 运行
RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY run.py requirements.txt README.md ./

# 数据目录挂载为卷，避免容器重建丢备份
RUN mkdir -p /app/data && chown -R app:app /app

USER app
VOLUME ["/app/data"]
EXPOSE 8077

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8077/api/health || exit 1

ENTRYPOINT ["python", "run.py", "--no-browser", "--host", "0.0.0.0", "--port", "8077"]