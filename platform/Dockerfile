# =============================================================================
# FashionOps API 镜像
# -----------------------------------------------------------------------------
# 状态：**已实测**（Docker Desktop 4.91.0，WSL2 后端，引擎 29.8.0）。
#       构建通过，容器内 `fashionops-db init` 建出 23 张表，`/health` 三项依赖
#       （MySQL / Redis / Qdrant）全通，未带 token 访问业务接口返回 401。
#       但没有在别的机器 / CI 上验证过，换环境请先看 docker-compose.yml 顶部的
#       「第一次跑之前检查这几项」。
#
# 构建时最容易踩的两个坑（都真的踩过）：
#   1. `README.md` 必须 COPY（见下面 COPY 那行的注释）。
#   2. 网络受限（DNS 污染）时要传构建参数 HTTP_PROXY / HTTPS_PROXY，
#      且值必须是 `host.docker.internal:7897` 而**不是** `127.0.0.1:7897`
#      —— 构建容器里的 127.0.0.1 是它自己。见 docker-compose.yml 的 api.build.args。
#
# 用 uv 而不是 pip：
#   uv 的 lock 文件能保证镜像里装的依赖和开发机完全一致，
#   而 `pip install -e .` 会重新解析依赖树 —— 结果是"本地好的、镜像里坏的"。
# =============================================================================
FROM python:3.12-slim AS base

# uv 的官方镜像拷贝方式：比 `pip install uv` 快且不引入 pip 依赖
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 中文日志要能正常输出，且日志文件必须是 UTF-8
#   PYTHONUTF8 让 Python 的默认 IO 编码变成 UTF-8
#   PYTHONUNBUFFERED 让日志实时出来（否则容器里看不到实时输出，只有等进程退出）
ENV PYTHONUTF8=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# ---- 依赖层（单独一层，改代码时不用重装依赖）----
# 先只拷 pyproject/lock：只要这两份没变，这一层命中缓存
#
# **README.md 必须一起拷**：`pyproject.toml` 里有 `readme = "README.md"`，
# 而 hatchling 在构建元数据时会去读这个文件 —— 读不到就
# `OSError: Readme file does not exist: README.md`。
# 这个错误**只有真正构建镜像时才会出现**（本地 `uv sync` 不受影响，
# 因为文件就在那儿），所以它是典型的"第一次跑 Docker 才炸"的一类问题。
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --all-extras --no-install-project --no-dev

# ---- 代码层 ----
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY scripts/ ./scripts/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --all-extras --no-dev

# 数据目录（SQLite 降级用、对象存储本地后端用、日志）
RUN mkdir -p /app/data/logs /app/data/storage

# 不要用 root 跑服务。
#
# 理由不是"洁癖"：容器里的 root 一旦通过某个漏洞拿到写入能力，
# 影响的是宿主机的挂载卷。而且很多基础镜像的 root 有 CAP_NET_RAW 之类的能力，
# 一个 RCE 就能变成一次完整的容器逃逸尝试。
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

ENV PATH="/app/.venv/bin:$PATH" \
    FASHIONOPS_DATA_DIR=/app/data

EXPOSE 8000

# 探活用 /health，不用 `/`：
#   `/health` 会真的去探 MySQL/Redis/Qdrant 三项依赖，
#   而 `/` 只是返回一个静态响应 —— 用它做探针等于没探。
# start-period 给 20 秒：应用启动时要同步权限码 + 检查表结构，
#   这段时间内的失败不该被计入 unhealthy。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

# 不用 --reload：那是开发专用的。生产要的是稳定的 worker 数。
# 也不用多个 worker —— 任务执行器目前是**进程内**的（见 docs/11 §5 P0-1），
# 多 worker 会让同一个任务被重复执行。等任务队列落库之后再调大。
CMD ["uvicorn", "fashionops.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
