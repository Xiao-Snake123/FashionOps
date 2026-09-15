"""日志配置的测试。

这组用例针对的是一个**真实存在过的缺陷**：项目里 10 个模块都写了
``logging.getLogger(...)`` 和 ``logger.info(...)``，但全项目没有一处配置过 logging。

后果是 ``logger.info`` **一条都没输出**（root logger 默认 WARNING），
而那些精心写的中文日志、关键告警，谁都没看见过。
代码看起来完全正常，review 也不会发现 —— "写了日志"和"日志能被看到"是两件事。

所以这里测的是"日志真的能被看到"，以及几个容易出错的细节：
不要重复 handler、上下文不要跨请求泄漏、编码不要退化成 GBK。
"""

from __future__ import annotations

import json
import logging

import pytest

from fashionops.logging_setup import (
    bind_request_context,
    get_logger,
    new_request_id,
    request_context,
    reset_logging,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _clean_logging():
    """每个用例前后都恢复原状。

    ``setup_logging`` 改的是**进程级**的 root logger，不清理会污染其他用例
    （比如 pytest 自己的日志捕获）。
    """
    reset_logging()
    yield
    reset_logging()


class TestSetupIsVisible:
    def test_info_logs_actually_appear(self, capfd):
        """最核心的一条：``logger.info`` 必须真的输出。

        这正是之前坏掉的地方 —— 没有这条用例，
        "日志配置缺失"可以一直存在而没人发现。
        """
        setup_logging(level="INFO", json_output=False)

        logging.getLogger("fashionops.test").info("这是一条 INFO 日志")
        out, err = capfd.readouterr()

        assert "这是一条 INFO 日志" in (out + err)

    def test_stdlib_logger_goes_through_same_pipeline(self, capfd):
        """已有代码用的是标准库 logging，它们必须走同一套渲染。

        不打通这层的话，就会出现"两套日志格式混在一起"：
        structlog 的带时间戳，标准库的只有一行裸文本。
        项目里绝大多数调用都是标准库风格，所以这条比 structlog 那条更重要。
        """
        setup_logging(level="INFO", json_output=True)

        logging.getLogger("fashionops.gateway").warning("网关熔断打开了")
        _, err = capfd.readouterr()

        payload = json.loads(err.strip().splitlines()[-1])
        assert payload["event"] == "网关熔断打开了"
        assert payload["level"] == "warning"
        # logger 名要带上，否则多模块日志混在一起分不清是谁打的
        assert "fashionops.gateway" in payload.get("logger", "")

    def test_level_filtering_works(self, capfd):
        setup_logging(level="WARNING", json_output=False)
        log = get_logger("fashionops.test")

        log.info("不该出现")
        log.warning("该出现")

        out, err = capfd.readouterr()
        text = out + err
        assert "不该出现" not in text
        assert "该出现" in text

    def test_debug_level_shows_debug(self, capfd):
        setup_logging(level="DEBUG", json_output=False)
        logging.getLogger("fashionops.test").debug("调试细节")

        out, err = capfd.readouterr()
        assert "调试细节" in (out + err)


class TestIdempotency:
    def test_repeated_setup_does_not_duplicate_handlers(self, capfd):
        """重复配置不能叠加 handler。

        API 进程、pytest、各 CLI 都会调 ``setup_logging``。
        不幂等的话同一条日志会打 N 遍 —— 而且 N 随着调用次数增长，
        日志量会莫名其妙地膨胀。
        """
        setup_logging(level="INFO", json_output=False)
        setup_logging(level="INFO", json_output=False)
        setup_logging(level="INFO", json_output=False)

        logging.getLogger("fashionops.test").info("只应出现一次")
        out, err = capfd.readouterr()

        assert (out + err).count("只应出现一次") == 1

    def test_root_has_exactly_one_handler(self):
        setup_logging(level="INFO")
        setup_logging(level="INFO")
        assert len(logging.getLogger().handlers) == 1


class TestRequestContext:
    def test_request_id_is_attached_to_every_log(self, capfd):
        """绑定之后，**同一请求里任何地方**打的日志都自动带上 request_id。

        这是不需要把 request_id 当参数一路透传的原因。
        """
        setup_logging(level="INFO", json_output=True)

        with request_context("req-abc123", path="/api/v1/tasks"):
            logging.getLogger("fashionops.api").info("第一层")
            logging.getLogger("fashionops.gateway").info("第二层")

        _, err = capfd.readouterr()
        lines = [json.loads(ln) for ln in err.strip().splitlines()]
        assert all(ln["request_id"] == "req-abc123" for ln in lines)
        assert all(ln["path"] == "/api/v1/tasks" for ln in lines)

    def test_context_is_cleared_after_request(self, capfd):
        """退出后必须**清空**上下文，不能只解绑本次绑的键。

        框架的 worker 会复用线程。不清的话下一个请求会继承上一个的 request_id，
        日志就变成"两个请求用同一个 ID" —— 那比完全没有 ID 更难查，
        因为你会以为两份日志属于同一次请求。
        """
        setup_logging(level="INFO", json_output=True)

        with request_context("req-1"):
            logging.getLogger("fashionops.test").info("请求内")
        logging.getLogger("fashionops.test").info("请求外")

        _, err = capfd.readouterr()
        lines = [json.loads(ln) for ln in err.strip().splitlines()]
        inside = next(ln for ln in lines if ln["event"] == "请求内")
        outside = next(ln for ln in lines if ln["event"] == "请求外")

        assert inside["request_id"] == "req-1"
        assert "request_id" not in outside, "上下文泄漏到请求外了"

    def test_nested_requests_do_not_leak(self, capfd):
        """连续两次请求不能互相污染 —— 串行复用线程是最常见的情形。"""
        setup_logging(level="INFO", json_output=True)

        for rid in ("req-A", "req-B"):
            with request_context(rid):
                logging.getLogger("fashionops.test").info("处理中")

        _, err = capfd.readouterr()
        lines = [json.loads(ln) for ln in err.strip().splitlines()]
        assert [ln["request_id"] for ln in lines] == ["req-A", "req-B"]

    def test_exception_inside_request_still_clears_context(self, capfd):
        """请求里抛异常时上下文也必须清掉。

        否则一次失败请求会污染之后所有请求的日志 —— 而且恰恰是
        出问题之后最需要干净日志的时候。
        """
        setup_logging(level="INFO", json_output=True)

        with pytest.raises(RuntimeError), request_context("req-boom"):
            raise RuntimeError("炸了")

        logging.getLogger("fashionops.test").info("之后")
        _, err = capfd.readouterr()
        line = json.loads(err.strip().splitlines()[-1])
        assert "request_id" not in line

    def test_bind_request_context_without_scope(self, capfd):
        setup_logging(level="INFO", json_output=True)
        bind_request_context(request_id="manual-1")
        logging.getLogger("fashionops.test").info("手动绑定")

        _, err = capfd.readouterr()
        assert json.loads(err.strip().splitlines()[-1])["request_id"] == "manual-1"

    def test_request_ids_are_unique_and_short(self):
        ids = {new_request_id() for _ in range(200)}
        assert len(ids) == 200, "生成了重复的 request_id"
        assert all(len(i) == 16 for i in ids)


class TestEncoding:
    def test_chinese_is_written_utf8(self, capfd):
        """日志里的中文必须以 UTF-8 落盘。

        Windows 控制台默认 GBK：重定向到文件后日志文件就是 GBK 的，
        拿到 Linux 或被 ELK 采集会全是乱码。
        这个项目里中文日志到处都是，所以这条很实际。
        """
        setup_logging(level="INFO", json_output=False)
        logging.getLogger("fashionops.test").info("启动 服装电商中台")

        out, err = capfd.readouterr()
        text = out + err
        assert "启动 服装电商中台" in text

    def test_unencodable_character_does_not_lose_the_log(self, capfd):
        """编不了的字符不能让整条日志消失。

        logging 遇到 UnicodeEncodeError 会吞掉并打 "--- Logging error ---"，
        也就是**那条日志本身没了** —— 而丢掉的往往正好是报错的那条。
        所以 handler 用了 ``errors="replace"``。
        """
        setup_logging(level="INFO", json_output=False)
        logging.getLogger("fashionops.test").info("含生僻字 %s 的日志", "\U0001f9ec")

        out, err = capfd.readouterr()
        text = out + err
        assert "含生僻字" in text
        assert "Logging error" not in text


class TestNoisyLibraries:
    def test_noisy_loggers_are_quieted(self):
        """第三方库压到 WARNING，但**不能**靠调高 root 级别来实现 ——
        那样会把自己代码的 INFO 也一起丢掉。"""
        setup_logging(level="INFO")

        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING
        # 我们自己的 logger 不受影响（它继承 root 的 INFO）
        assert logging.getLogger("fashionops").getEffectiveLevel() <= logging.INFO

    def test_uvicorn_logs_are_merged_not_duplicated(self, capfd):
        """uvicorn 自带的 handler 要清掉并让它 propagate。

        不清的话 uvicorn 的启动日志会打两遍（一遍它自己的格式、一遍我们的）。
        """
        setup_logging(level="INFO", json_output=True)

        logging.getLogger("uvicorn.error").info("Started server process")
        _, err = capfd.readouterr()

        assert err.count("Started server process") == 1
        # 而且要是我们的 JSON 格式
        assert json.loads(err.strip().splitlines()[-1])["event"] == "Started server process"

    def test_uvicorn_access_log_is_disabled(self):
        """access log 关掉 —— 我们用中间件自己记，带 request_id 和耗时。"""
        setup_logging(level="INFO")
        assert logging.getLogger("uvicorn.access").disabled


def test_setup_logging_is_safe_without_explicit_args():
    """``setup_logging`` 不传参时要能从 ``Settings`` 取到级别和格式，且不抛异常。

    API 的 ``create_app`` 在**模块顶层**调它，那时如果抛异常整个应用起不来 ——
    而"日志没配好"远不该是致命错误。
    """
    setup_logging()
    root = logging.getLogger()
    assert root.handlers
    assert root.level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR)
