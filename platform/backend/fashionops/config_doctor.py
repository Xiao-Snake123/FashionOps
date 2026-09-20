"""配置体检：找出 ``.env`` 里**拼错所以被静默忽略**的配置项。

## 为什么需要这个

``Settings`` 用的是 ``extra="ignore"``：未知的环境变量**不报错，直接丢掉**。
于是下面这种错误完全没有症状：

```ini
# 字段其实叫 smtp_host，所以正确名字是 FASHIONOPS_SMTP_HOST
FASHIONOPS_MAIL_SMTP_HOST=smtp.qq.com
```

应用照常启动、照常跑，只是**用的是默认值**（``smtp_host=""``，
于是 ``mail_can_really_send`` 永远是 False，邮件永远走 dry_run）。
排查起来会往邮件代码里找，而问题在一个字母上。

这类"配置写错但没有任何反馈"的问题，在企业环境下尤其贵：
换环境（dev → staging → prod）时最容易出现，而那时往往没人盯着日志。

所以这里做三件事：

1. **未知键 + "你是不是想写 X"**（用 ``difflib`` 猜最接近的合法名）
2. **值明显不合理**的常识检查（如 ``SMTP_HOST`` 填成了邮箱地址）
3. 把结果同时暴露给 **启动日志** 和 **CLI**，两边都能被发现

## 为什么不直接改成 ``extra="forbid"``

那样一个多余的变量就会让应用**起不来**。生产上"起不来"比"配置没生效"严重得多 ——
而且 base 镜像、K8s 注入、云厂商 SDK 都可能往环境里塞 ``FASHIONOPS_`` 开头的变量。
**告警而不是拒绝**是正确的取舍。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from fashionops.config import ENV_PREFIX, Settings, get_settings


@dataclass
class ConfigIssue:
    """一条配置问题。"""

    #: error = 一定会导致行为不符预期；warning = 很可能不对
    level: str
    key: str
    message: str
    suggestion: str = ""


@dataclass
class DoctorReport:
    issues: list[ConfigIssue] = field(default_factory=list)
    #: 实际读到了哪些 .env 文件（多个时按优先级排列）
    env_files: list[Path] = field(default_factory=list)
    #: 认识多少个配置项
    known_count: int = 0

    @property
    def errors(self) -> list[ConfigIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[ConfigIssue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.issues


def known_env_keys() -> dict[str, str]:
    """合法的环境变量名 → 字段名。

    推导规则就是 pydantic-settings 的规则：``ENV_PREFIX`` + 字段名大写。
    **从模型推导而不是手写清单**，这样加了字段这里自动跟上，
    不会出现"体检工具自己过时了"的情况。
    """
    return {f"{ENV_PREFIX}{name.upper()}": name for name in Settings.model_fields}


def _read_env_keys(path: Path) -> dict[str, str]:
    """读 .env 里的 KEY=VALUE（只做最朴素的解析，够用就行）。"""
    result: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return result

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # 允许 `export FOO=bar` 这种写法
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        if key:
            result[key] = value.strip().strip('"').strip("'")
    return result


def _semantic_checks(key: str, value: str) -> list[tuple[str, str]]:
    """值本身的常识检查（不看名字，看内容对不对得上）。

    这些规则都来自真实踩过的坑，不是臆想的。

    注意：**类型错误不归这里管**。比如 ``FASHIONOPS_SMTP_PORT=abc``，
    pydantic 在构造 Settings 时就会直接报 ValidationError ——
    也就是说这类问题根本走不到体检这一步。这里只做"类型对但语义错"的检查，
    比如把邮箱地址填进 SMTP_HOST。
    """
    out: list[tuple[str, str]] = []

    if key == "FASHIONOPS_SMTP_HOST" and value:
        if "@" in value:
            out.append(
                (
                    "error",
                    f"看起来是个邮箱地址（{value}），但 SMTP 主机应该填**服务器域名**。",
                )
            )
            out.append(("hint", "QQ 邮箱是 smtp.qq.com；163 是 smtp.163.com；企业微信是 smtp.exmail.qq.com"))
        if "://" in value:
            out.append(("warning", "SMTP 主机不该带协议头（smtp://），只填域名"))

    if key == "FASHIONOPS_SMTP_PORT" and value:
        # 端口本身不是数字的话，pydantic 早就报错了，走不到这里；
        # 保留这层是防御：将来若把该字段改成 str（比如允许 "465/ssl" 这种写法），
        # 这里能接住。
        try:
            port = int(value)
        except ValueError:
            out.append(("error", f"端口必须是数字，当前是 {value!r}"))
        else:
            if port not in (25, 465, 587, 994, 2525):
                out.append(("warning", f"{port} 不是常见的 SMTP 端口（25/465/587/994）"))

    if key == "FASHIONOPS_SMTP_PASSWORD" and len(value) > 32:
        # QQ/163 的授权码是 16 位小写字母。填成 32 位以上的多半是别的东西。
        out.append(("warning", f"长度 {len(value)}，比常见的授权码长；确认没把别的密钥填进来"))

    if key in ("FASHIONOPS_MAIL_FROM", "FASHIONOPS_SMTP_USER") and value and "@" not in value:
        out.append(("warning", f"{value!r} 不像邮箱地址"))

    if key == "FASHIONOPS_MAIL_SEND_ALLOWLIST" and value:
        bad = [a for a in value.replace(";", ",").split(",") if a.strip() and "@" not in a]
        if bad:
            out.append(("warning", f"这些白名单项不像邮箱：{bad}"))

    if key.endswith("_URL") and value and not (value.startswith(("http://", "https://", "redis://", "mysql"))):
        out.append(("warning", f"URL 应该带 scheme，当前是 {value!r}"))

    if (
        key in ("FASHIONOPS_MAIL_ENABLED", "FASHIONOPS_MAIL_DRY_RUN")
        and value
        and value.lower() not in ("true", "false", "1", "0", "yes", "no", "on", "off")
    ):
        out.append(("error", f"布尔值只认 true/false/1/0，当前是 {value!r}"))

    return out


def check_config(settings: Settings | None = None) -> DoctorReport:
    """跑一遍体检。**不抛异常**，把所有问题收集完再返回。"""
    settings = settings or get_settings()
    report = DoctorReport(known_count=len(known_env_keys()))
    known = known_env_keys()

    # 注意用 ``type(settings).model_config`` 而不是 ``Settings.model_config``：
    # 读基类的配置会忽略调用方实际使用的 env_file —— 测试里换成临时文件也看不见，
    # 而且"报告说读的是哪个文件"和"真的用了哪个文件"会对不上。
    model_config = type(settings).model_config
    paths = [Path(p) for p in (model_config.get("env_file") or ())]
    report.env_files = [p for p in paths if p.exists()]

    seen: dict[str, str] = {}
    for path in report.env_files:
        seen.update(_read_env_keys(path))

    if not report.env_files:
        report.issues.append(
            ConfigIssue(
                level="warning",
                key="(env file)",
                message="没有找到任何 .env 文件，全部配置都在用默认值。",
                suggestion="复制 platform/.env.example 为 platform/.env 再改",
            )
        )

    for key, value in sorted(seen.items()):
        if not key.startswith(ENV_PREFIX):
            continue  # 不是我们的命名空间，不管

        if key not in known:
            close = difflib.get_close_matches(key, known.keys(), n=1, cutoff=0.6)
            report.issues.append(
                ConfigIssue(
                    level="error",
                    key=key,
                    message="这个配置项不存在，**会被静默忽略**（用的是默认值）",
                    suggestion=f"改成 {close[0]}" if close else "检查拼写，或从 .env.example 对照",
                )
            )
            continue

        for level, message in _semantic_checks(key, value):
            if level == "hint":
                # hint 合并到上一条 issue 的 suggestion 里
                if report.issues and report.issues[-1].key == key:
                    report.issues[-1].suggestion = (report.issues[-1].suggestion + " " + message).strip()
                continue
            report.issues.append(ConfigIssue(level=level, key=key, message=message))

    # ---- 跨字段的一致性检查 ----
    if settings.mail_enabled and not settings.smtp_host:
        report.issues.append(
            ConfigIssue(
                level="error",
                key="FASHIONOPS_MAIL_ENABLED",
                message="开了发信总开关，但 SMTP_HOST 是空的 → 仍然只会走 dry_run，邮件发不出去",
                suggestion="填 FASHIONOPS_SMTP_HOST（如 smtp.qq.com）",
            )
        )

    if settings.mail_enabled and not settings.mail_dry_run and not settings.mail_allowlist:
        report.issues.append(
            ConfigIssue(
                level="error",
                key="FASHIONOPS_MAIL_SEND_ALLOWLIST",
                message=(
                    "关掉了 dry_run、也没配白名单。白名单为空是 **fail-closed 禁发**，"
                    "所以邮件还是发不出去（这是刻意的安全默认值）"
                ),
                suggestion="至少把测试收件人写进白名单",
            )
        )

    if settings.mail_enabled and settings.smtp_host and not settings.smtp_user:
        report.issues.append(
            ConfigIssue(
                level="warning",
                key="FASHIONOPS_SMTP_USER",
                message="没配 SMTP 登录名。多数公有云 SMTP（QQ/163/企业微信）不认证会直接拒收",
            )
        )

    return report


__all__ = ["ConfigIssue", "DoctorReport", "check_config", "known_env_keys"]
