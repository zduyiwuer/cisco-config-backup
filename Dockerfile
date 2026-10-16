# ============================================================
#  多厂商网络设备配置备份中心
#  多阶段构建：builder 装依赖，runtime 只带运行所需文件
# ============================================================

# ---------- 构建阶段 ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# 用 BuildKit 缓存挂载放pip 下载缓存：不进镜像层，
# 重建时还能复用，磁盘占用远小于把缓存写进层里
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /opt/venv

WORKDIR /build
# 只拷依赖清单，源码变更不会让依赖层缓存失效
COPY requirements.txt .

# 本项目的依赖在主流平台都有预编译 wheel（cryptography / cffi / bcrypt /
# pynacl / pyyaml 均提供 manylinux wheel），因此**不装 gcc 等编译工具链**。
# 这样构建阶段能省下约 300MB 磁盘占用。
# 若在罕见的平台上遇到 "Failed building wheel"，改用下面这行装工具链：
#   RUN apt-get update && apt-get install -y --no-install-recommends \
#           gcc libc6-dev libssl-dev && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/pip \
    /opt/venv/bin/pip install -r requirements.txt

# ---------- 运行阶段 ----------
FROM python:3.12-slim

LABEL org.opencontainers.image.title="多厂商网络设备配置备份中心" \
      org.opencontainers.image.description="网络设备配置备份与版本管理，支持 Cisco/H3C/华为/锐捷" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PATH="/opt/venv/bin:$PATH"

# curl 供健康检查；ca-certificates 供AD 的 LDAPS 连接
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl tzdata ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# 非 root 运行
RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY run.py ./

# 数据目录挂载为卷，避免容器重建丢备份
RUN mkdir -p /app/data && chown -R app:app /app

USER app
VOLUME ["/app/data"]
EXPOSE 8077

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8077/api/health || exit 1

ENTRYPOINT ["python", "run.py", "--no-browser", "--host", "0.0.0.0", "--port", "8077"]