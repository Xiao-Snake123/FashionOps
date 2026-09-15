"""日志配置：一处配置，全项目生效。

## 之前是什么状态（值得记下来）

项目里 10 个模块都写了 ``logger = logging.getLogger("fashionops.xxx")``，
也都有 ``logger.info`` / ``logger.warning`` 调用 —— 但**没有任何地方配置过 logging**。

后果：

* ``logger.info(...)`` **完全消失**。Python 的 root logger 默认级别是 WARNING，
  没有 handler 时 WARNING 以上会走 ``logging.lastResort`` 输出到 stderr，
  INFO/DEBUG 则被直接丢弃。
* 于是那些"精心写的中文日志"一条都没人看见过。
* 更糟的是 ``logger.warning("记录失败 %s", exc)`` 这类**关键告警**也只是一行
  没有时间戳、没有模块名的裸文本，混在 uvicorn 的输出里根本认不出来。

这类问题的隐蔽之处在于：代码看起来完全正常，review 时也不会有人发现问题 ——
"写了日志"和"日志能被看到"是两件事。

## 设计取舍

**用 structlog 而不是自己拼 JSON。** 理由：

1. 项目里已有大量**标准库 logging** 调用。structlog 的 ``ProcessorFormatter``
   能让它们和 structlog 调用走同一套渲染，不用把已有代码全改一遍。
2. ``contextvars`` 原生的请求上下文绑定：``bind_contextvars(request_id=...)``
   之后，**同一次请求里任何地方打的日志都会自动带上它** ——
   不需要把 request_id 当参数一路透传到网关、Agent、工具层。

**开发用带颜色的 console，生产用 JSON。** JSON 是给机器读的（ELK/Loki），
人读 JSON 很痛苦；反过来，开发时人读的多，彩色 console 效率高得多。
默认按 ``env`` 自动选，也可以显式指定。

**幂等。** 重复调用只生效一次 —— 因为 API 进程、pytest、各 CLI 都会调它，
不幂等会出现重复 handler，同一条日志打两遍（甚至 N 遍）。
"""

from __future__ import annotations

import contextlib
import logging
import sys
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

if TYPE_CHECKING:
    from collections.abc import Iterator

#: 这些第三方库在 INFO 级别非常吵，会把有用的日志淹掉。
#: 单独压到 WARNING —— 注意是**按需压制**，不是一刀切把 root 级别调高，
#: 那样会把我们自己的 INFO 也一起丢掉。
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "urllib3",
    "qdrant_client",
    "langchain",
    "langchain_core",
    "openai",
    "sqlalchemy.engine",
    "asyncio",
)

_configured = False


def _resolve_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(str(level).upper())
    # getLevelName 对非法名字返回字符串 "Level XXX"，这里兜回 INFO
    return resolved if isinstance(resolved, int) else logging.INFO


def _build_renderer(use_json: bool) -> Any:  # noqa: ANN401
    """选渲染器。JSON 给机器，console 给人。"""
    if use_json:
        return structlog.processors.JSONRenderer(ensure_ascii=False)
    return structlog.dev.ConsoleRenderer(
        # 关掉"把异常单独一块打印"，让它跟在日志行里，
        # 这样按 request_id 检索时异常不会和它的上下文分家
        exception_formatter=structlog.dev.plain_traceback,
        colors=sys.stderr.isatty(),
    )


def _utf8_stream() -> Any:  # noqa: ANN401
    """拿一个确保是 UTF-8 的 stderr。

    **为什么不能直接用 ``sys.stderr``**：Windows 中文控制台的默认编码是 GBK。
    日志里有中文（这个项目里到处都是）时会有两个后果：

    1. 重定向到文件后，日志文件是 **GBK 编码**的 —— 拿到 Linux 上、
       或者被 ELK/Loki 采集时全是乱码。
    2. 遇到 GBK 编不了的字符（比如 emoji、某些生僻字）时，
       logging 内部会抛 ``UnicodeEncodeError``。它虽然会被 logging 自己吞掉
       并打印 "--- Logging error ---"，但**那条日志本身就没了** ——
       而丢掉的往往正好是报错的那条。

    ``errors="replace"`` 是兜底：宁可把个别字符打成 ``?``，
    也绝不让"打日志"这件事把日志本身弄丢。
    """
    stream = sys.stderr
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        # 已经被包装过 / 不是 TextIOWrapper 时会失败，忽略即可
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")
    return stream


def setup_logging(
    *,
    level: str | int | None = None,
    json_output: bool | None = None,
) -> None:
    """配置全项目日志。可重复调用，只有第一次生效。

    ``level`` / ``json_output`` 留空时从 ``Settings`` 取。
    显式传参主要是给 CLI 和测试用。
    """
    global _configured
    if _configured:
        return

    # 延迟 import：config 里会 import net，net 里可能打日志 ——
    # 在模块顶层 import 有循环风险
    from fashionops.config import get_settings

    settings = get_settings()

    if level is None:
        level = settings.log_level
    if json_output is None:
        # 生产环境默认 JSON（要接日志采集），开发默认彩色 console
        json_output = settings.log_format == "json" if settings.log_format else settings.env == "prod"

    log_level = _resolve_level(level)

    # ---------------- structlog 侧 ----------------
    shared_processors: list[Any] = [
        # 把请求上下文（request_id / user 等）并进每条日志。
        # 放在最前面，这样后面所有 processor 和最终输出都带着它。
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            # stdlib 的日志对象要经过 ProcessorFormatter 才能用这套 processor，
            # 而它自己会再跑一次 processors —— 这个标记避免重复处理
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # ---------------- stdlib 侧 ----------------
    # 关键：让已有的 ``logging.getLogger("fashionops.xxx")`` 调用
    # 也走 structlog 的渲染。不改这层的话，两套日志格式会混在一起。
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _build_renderer(json_output),
        ],
    )

    handler = logging.StreamHandler(_utf8_stream())
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # 清掉已有 handler（uvicorn 可能会先装一个），避免一条日志打两遍
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(log_level)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # 让 uvicorn 自己的日志也并进来（它的 access log 我们已经有中间件替代）
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True

    _configured = True


def reset_logging() -> None:
    """撤销配置。**只给测试用** —— 测试之间要能改成不同的级别/格式。"""
    global _configured
    _configured = False
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    clear_contextvars()


def get_logger(name: str = "fashionops") -> Any:  # noqa: ANN401
    """取一个 logger。

    返回的是 structlog 的 BoundLogger，但**标准库的 ``logging.getLogger``
    同样可用**（已经通过 ProcessorFormatter 接到同一套渲染上了）。
    已有代码不需要改。
    """
    return structlog.get_logger(name)


# ---------------------------------------------------------------------------
# 请求上下文
# ---------------------------------------------------------------------------
def new_request_id() -> str:
    """生成请求 ID。用 hex 而不是 uuid4 的带横线形式 —— 日志里短一点更好读。"""
    return uuid.uuid4().hex[:16]


def bind_request_context(**kwargs: Any) -> None:  # noqa: ANN401
    """把上下文绑到当前 async 任务 / 线程。

    ``contextvars`` 是按任务隔离的，所以并发请求之间不会串。
    """
    bind_contextvars(**kwargs)


@contextmanager
def request_context(request_id: str, **extra: Any) -> Iterator[str]:  # noqa: ANN401
    """请求级上下文。用法::

        with request_context(new_request_id(), path="/api/v1/tasks") as rid:
            logger.info("开始处理")     # 自动带上 request_id 和 path

    退出时**清空**而不是只解绑本次绑定的键 —— 因为框架的 worker 会复用线程，
    不清的话下一个请求会继承上一个请求的 request_id，
    日志就成了"两个请求用同一个 ID"，比没有 ID 更难查。
    """
    clear_contextvars()
    bind_contextvars(request_id=request_id, **extra)
    try:
        yield request_id
    finally:
        clear_contextvars()


__all__ = [
    "bind_request_context",
    "get_logger",
    "new_request_id",
    "request_context",
    "reset_logging",
    "setup_logging",
]
