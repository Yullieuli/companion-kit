# companion-kit 容器镜像
#
# 容器里能做什么：core 全套（决策/记忆/心情/事件/成本/MCP stdio）、只读 HTTP 事件流、CLI。
# 容器里不能做什么：桌面控制（没有显示）、TTS 语音（没有音频栈）、手机 adb（要映射 USB）、
# 浏览器 CDP（要连宿主 Chrome）。这四条在 README 的平台矩阵里也这么写。
#
# 三个阶段：
#   builder  构建 wheel（保证装的是构建产物，不是源码目录）
#   runtime  运行镜像（默认目标）
#   test     在容器里跑全量单测（构建时即验证，不靠"我本地跑过"）
#
# 用法：
#   docker build -t companion-kit .
#   docker build --target test -t companion-kit:test .    # 构建时跑 379 项单测
#   docker run --rm -v companion-data:/data companion-kit companion doctor
#   docker run --rm -p 127.0.0.1:7788:7788 -v companion-data:/data \
#              companion-kit serve --host 0.0.0.0 --port 7788
#
# 换源（国内网络）：--build-arg BASE=m.daocloud.io/docker.io/library/python:3.12-slim
ARG BASE=python:3.12-slim

# ── builder：打 wheel ────────────────────────────────────────
FROM ${BASE} AS builder
WORKDIR /src
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# hermes 适配层通过 [tool.setuptools.data-files] 打进 wheel；
# 不 COPY 这一行，构建阶段就会因为找不到文件而失败。
COPY hermes ./hermes
RUN python -m build --wheel --outdir /wheels

# ── runtime：只装 wheel，不带源码 ─────────────────────────────
FROM ${BASE} AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    COMPANION_HOME=/data \
    TZ=Asia/Shanghai
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels \
    && useradd --create-home --uid 10001 companion \
    && mkdir -p /data \
    && chown -R companion:companion /data
# store 必须挂卷：容器重建即失忆，等于换一个人
VOLUME ["/data"]
USER companion
WORKDIR /data
EXPOSE 7788
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import companion" || exit 1
ENTRYPOINT ["companion"]
CMD ["doctor"]

# ── test：容器内跑全量单测 ────────────────────────────────────
FROM runtime AS test
USER root
WORKDIR /src
COPY tests ./tests
COPY pyproject.toml install.sh install.ps1 ./
COPY hermes ./hermes
RUN chown -R companion:companion /src
USER companion
# 用 RUN 而不是只靠 CMD：构建这一层时就把全量单测跑一遍，
# 「docker build --target test 通过」本身就是绿证据，而不是"回头再跑"。
RUN python -m unittest discover -s tests
CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
