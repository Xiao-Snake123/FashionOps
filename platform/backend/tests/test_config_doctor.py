"""配置体检的测试。

这组用例存在的理由：**配置写错是没有任何症状的一类 bug**。

``Settings`` 用 ``extra="ignore"``，未知的环境变量会被静默丢弃 ——
应用照常启动、照常跑，只是用的是默认值。真实案例：

```ini
# 字段叫 smtp_host，所以正确名字是 FASHIONOPS_SMTP_HOST
FASHIONOPS_MAIL_SMTP_HOST=smtp.qq.com
# 结果：smtp_host="" → mail_can_really_send 永远 False → 邮件永远走 dry_run
```

排查时会往邮件代码里找，而问题在一个字母上。所以体检工具必须可靠，
下面每一条"能发现问题"的用例都配了对应的"不误报"用例。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fashionops.config import Settings
from fashionops.config_doctor import check_config, known_env_keys


def _settings_from_env(tmp_path: Path, env_text: str, monkeypatch) -> Settings:
    """把一段 .env 内容写成真文件，构造指向它的 Settings。

    **不走 ``get_settings()``** —— 那是 ``lru_cache`` 单例，
    测试之间会互相污染。直接构造实例，并且屏蔽掉真实 .env。
    """
    env_file = tmp_path / ".env"
    env_file.write_text(env_text, encoding="utf-8")

    # 断掉进程环境变量的干扰（CI 上可能注入过 FASHIONOPS_*）
    for key in list(known_env_keys()):
        monkeypatch.delenv(key, raising=False)

    cfg = Settings.model_config.copy()
    cfg["env_file"] = (env_file,)
    isolated = type("IsolatedSettings", (Settings,), {"model_config": cfg})
    return isolated(_env_file=str(env_file))


class TestKnownKeys:
    def test_derived_from_model_not_handwritten(self):
        """合法键必须从模型推导。

        手写清单一定会过时：加了字段忘了加清单，体检工具就会**误报**
        一个其实合法的配置项 —— 而误报会让人不再信任这个工具。
        """
        keys = known_env_keys()
        assert "FASHIONOPS_SMTP_HOST" in keys
        assert "FASHIONOPS_MAIL_ENABLED" in keys
        assert keys["FASHIONOPS_SMTP_HOST"] == "smtp_host"
        # 每个字段一个键，不多不少
        assert len(keys) == len(Settings.model_fields)

    def test_the_actually_wrong_name_is_not_known(self):
        """真实踩到的那个错名字，不能被当成合法键。"""
        assert "FASHIONOPS_MAIL_SMTP_HOST" not in known_env_keys()
        assert "FASHIONOPS_MAIL_SMTP_PASSWORD" not in known_env_keys()


class TestDetectsUnknownKeys:
    def test_reports_unknown_key_with_suggestion(self, tmp_path, monkeypatch):
        settings = _settings_from_env(
            tmp_path,
            "FASHIONOPS_MAIL_SMTP_HOST=smtp.qq.com\n",
            monkeypatch,
        )
        report = check_config(settings)

        assert not report.ok
        keys = [i.key for i in report.issues]
        assert "FASHIONOPS_MAIL_SMTP_HOST" in keys

        issue = next(i for i in report.issues if i.key == "FASHIONOPS_MAIL_SMTP_HOST")
        assert issue.level == "error"
        # 关键：要给出**正确的名字**，不能只说"错了"
        assert "FASHIONOPS_SMTP_HOST" in issue.suggestion

    def test_ignores_unrelated_env_prefix(self, tmp_path, monkeypatch):
        """不是我们命名空间的变量不该报 —— 否则一屋子噪音，没人会看。"""
        settings = _settings_from_env(tmp_path, "PATH=/usr/bin\nHOME=/root\n", monkeypatch)
        assert check_config(settings).ok

    def test_valid_env_passes_cleanly(self, tmp_path, monkeypatch):
        """写对了就必须一条问题都没有。**误报会让工具失去信任。**"""
        settings = _settings_from_env(
            tmp_path,
            (
                "FASHIONOPS_SMTP_HOST=smtp.qq.com\n"
                "FASHIONOPS_SMTP_PORT=465\n"
                "FASHIONOPS_SMTP_USER=ops@qq.com\n"
                "FASHIONOPS_MAIL_FROM=ops@qq.com\n"
                "FASHIONOPS_MAIL_ENABLED=true\n"
                "FASHIONOPS_MAIL_DRY_RUN=false\n"
                "FASHIONOPS_MAIL_SEND_ALLOWLIST=ops@qq.com,customer@example.com\n"
            ),
            monkeypatch,
        )
        report = check_config(settings)
        assert report.ok, [f"{i.key}: {i.message}" for i in report.issues]


class TestSemanticChecks:
    """值的常识检查 —— 名字对但值填错了，同样发现不了。"""

    def test_smtp_host_that_is_an_email_address(self, tmp_path, monkeypatch):
        """真实踩到的那次：把邮箱地址填进了 SMTP_HOST。

        名字（如果写对）和值看起来都"像那么回事"，只有语义检查能发现。
        """
        settings = _settings_from_env(tmp_path, "FASHIONOPS_SMTP_HOST=3272109547@qq.com\n", monkeypatch)
        report = check_config(settings)

        issue = next(i for i in report.issues if i.key == "FASHIONOPS_SMTP_HOST")
        assert issue.level == "error"
        assert "邮箱地址" in issue.message
        # 要顺手告诉对方正确答案，否则用户还是得去搜
        assert "smtp.qq.com" in issue.suggestion

    def test_smtp_host_with_scheme(self, tmp_path, monkeypatch):
        settings = _settings_from_env(tmp_path, "FASHIONOPS_SMTP_HOST=smtp://smtp.qq.com\n", monkeypatch)
        report = check_config(settings)
        assert any("协议头" in i.message for i in report.issues)

    def test_bad_port_is_caught_by_pydantic_not_the_doctor(self, tmp_path, monkeypatch):
        """类型错误由 pydantic 拦住，体检工具根本走不到。

        这条用例的价值在于**说清职责边界**：体检工具只负责
        "名字拼错"和"类型对但语义错"，类型错误归 pydantic。
        不然以后有人看到 doctor 里有 int() 就想删掉那层防御。
        """
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc:
            _settings_from_env(tmp_path, "FASHIONOPS_SMTP_PORT=abc\n", monkeypatch)
        assert "smtp_port" in str(exc.value)

    def test_unusual_port_is_warning_not_error(self, tmp_path, monkeypatch):
        """不常见但合法（内网中继可能用任意端口）→ 只警告，不报错。

        报错会让 CI 红，而 CI 红了人会习惯性地"改配置让它过"，
        于是真正的警告也被一起忽略了。
        """
        settings = _settings_from_env(tmp_path, "FASHIONOPS_SMTP_PORT=10025\n", monkeypatch)
        report = check_config(settings)
        assert report.warnings
        assert not report.errors

    def test_allowlist_entries_that_are_not_emails(self, tmp_path, monkeypatch):
        settings = _settings_from_env(tmp_path, "FASHIONOPS_MAIL_SEND_ALLOWLIST=ops@qq.com,not-an-email\n", monkeypatch)
        report = check_config(settings)
        assert any("不像邮箱" in i.message for i in report.issues)


class TestCrossFieldConsistency:
    """单看每一项都对，组合起来注定不工作 —— 这类问题最费时间。"""

    def test_enabled_without_smtp_host(self, tmp_path, monkeypatch):
        settings = _settings_from_env(tmp_path, "FASHIONOPS_MAIL_ENABLED=true\n", monkeypatch)
        report = check_config(settings)

        issue = next(i for i in report.issues if i.key == "FASHIONOPS_MAIL_ENABLED")
        assert issue.level == "error"
        assert "SMTP_HOST" in issue.message

    def test_real_send_without_allowlist(self, tmp_path, monkeypatch):
        """关掉 dry_run 但白名单为空 —— 这是最容易误判的一种。

        用户会以为"我开了开关也该发出去了"，实际白名单为空是 fail-closed 禁发。
        不提示的话，表现和"SMTP 配错了"一模一样。
        """
        settings = _settings_from_env(
            tmp_path,
            (
                "FASHIONOPS_MAIL_ENABLED=true\n"
                "FASHIONOPS_MAIL_DRY_RUN=false\n"
                "FASHIONOPS_SMTP_HOST=smtp.qq.com\n"
                "FASHIONOPS_SMTP_USER=ops@qq.com\n"
            ),
            monkeypatch,
        )
        report = check_config(settings)

        issue = next(i for i in report.issues if i.key == "FASHIONOPS_MAIL_SEND_ALLOWLIST")
        assert issue.level == "error"
        assert "fail-closed" in issue.message

    def test_enabled_without_smtp_user(self, tmp_path, monkeypatch):
        settings = _settings_from_env(
            tmp_path,
            (
                "FASHIONOPS_MAIL_ENABLED=true\n"
                "FASHIONOPS_MAIL_DRY_RUN=false\n"
                "FASHIONOPS_SMTP_HOST=smtp.qq.com\n"
                "FASHIONOPS_MAIL_SEND_ALLOWLIST=ops@qq.com\n"
            ),
            monkeypatch,
        )
        report = check_config(settings)
        assert any(i.key == "FASHIONOPS_SMTP_USER" for i in report.issues)


class TestTheDoctorItself:
    def test_missing_env_file_is_reported_not_crashed(self, tmp_path, monkeypatch):
        """一个 .env 都没有时要给提示，而不是抛异常。

        体检工具崩掉比不体检更糟：部署脚本会在"检查配置"这一步死掉，
        而真正的配置问题还没被看到。
        """
        for key in list(known_env_keys()):
            monkeypatch.delenv(key, raising=False)
        cfg = Settings.model_config.copy()
        cfg["env_file"] = (tmp_path / "nope.env",)
        isolated = type("NoEnvSettings", (Settings,), {"model_config": cfg})

        report = check_config(isolated(_env_file=str(tmp_path / "nope.env")))
        assert any("没有找到任何 .env" in i.message for i in report.issues)

    def test_ok_property_matches_issues(self, tmp_path, monkeypatch):
        clean = check_config(_settings_from_env(tmp_path, "", monkeypatch))
        assert clean.ok or clean.errors == []  # 空 .env 最多是 warning

        dirty = check_config(_settings_from_env(tmp_path, "FASHIONOPS_TYPO_HERE=1\n", monkeypatch))
        assert not dirty.ok
        assert len(dirty.errors) == 1


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("FASHIONOPS_SMTP_HOST=smtp.qq.com", "FASHIONOPS_SMTP_HOST"),
        ("  FASHIONOPS_SMTP_PORT=465  ", "FASHIONOPS_SMTP_PORT"),
        # 用 TIMEZONE 而不是 MAIL_ENABLED：后者单独出现会（正确地）
        # 触发"开了总开关却没配 SMTP_HOST"的跨字段错误，
        # 那会让这条用例测不出它真正想测的东西
        ("export FASHIONOPS_TIMEZONE=Asia/Shanghai", "FASHIONOPS_TIMEZONE"),
        ('FASHIONOPS_MAIL_FROM="ops@qq.com"', "FASHIONOPS_MAIL_FROM"),
    ],
)
def test_env_parsing_handles_common_shapes(tmp_path, monkeypatch, line, expected):
    """.env` 的写法五花八门：带空格、带 export、带引号。

    解析漏掉一种，用户就会得到"我明明配了但它说没配"的困惑。
    """
    settings = _settings_from_env(tmp_path, f"{line}\n", monkeypatch)
    report = check_config(settings)
    unknown = [i for i in report.issues if "不存在" in i.message]
    assert not any(i.key == expected for i in unknown), f"{line!r} 被误判为未知键 —— 解析器没处理好这种写法"
