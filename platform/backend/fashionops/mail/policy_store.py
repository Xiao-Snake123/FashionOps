"""邮件策略的**运行时覆盖层**。

## 为什么需要它

白名单 / 真发开关这些策略值，原来只能写进 ``.env`` 再重启进程。
但它们的真实使用场景是"运维临时放行一个地址，跑完再收回"——
为了改一个收件人域名去重启服务，等于逼人在便利和安全之间二选一，
而实际结果通常是把白名单写成 ``*`` 就再也不动了。

所以这里做一层**覆盖（override）**，不动 ``.env``：

* 覆盖值优先于 ``.env``，没被覆盖的键照旧读配置
* 只认 ``OVERRIDABLE`` 里的键 —— SMTP 密码这类**一律不可改**，
  否则"改策略"的接口就变成了一个绕过配置保护的口子
* 存 Redis（db1，和网关状态同库）；Redis 不可用时退回进程内存
* **丢失覆盖总是往安全的方向倒**：Redis 一清，策略回到 .env 的
  ``dry_run=true`` / 白名单收紧，不会意外变成"可以发给任何人"

## 为什么不去改 Settings 实例

``Settings`` 是 pydantic-settings 的全局单例，被 API 进程、MCP 进程、
任务 worker 共用。直接改它只有一个进程看得见 —— 行为会变成
"有时候生效"，那比明确不支持更难排查。覆盖层至少是显式的、读得回来的。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final

from fashionops.cache import DB_GATEWAY, get_redis
from fashionops.config import Settings, get_settings

logger = logging.getLogger("fashionops.mail.policy")

#: 键名。和业务缓存分开（它有自己的命名空间前缀），避免被版本号失效误伤
_OVERRIDE_KEY: Final = "mailpolicy:overrides"

#: **允许被运行时修改的键**。这份白名单是安全边界，不是便利开关：
#: 每加一个键都要回答"通过接口改它，能不能绕过某个策略"。
#: SMTP 主机/账号/密码、附件大小上限都不在里面 ——
#: 前者改了就能把信发到攻击者的服务器，后者改大就能把大文件带出去。
OVERRIDABLE: Final[frozenset[str]] = frozenset(
    {
        "mail_enabled",  # 总开关
        "mail_dry_run",  # 演练 → 真发
        "mail_send_allowlist",  # 收件人白名单
        "mail_send_denylist",  # 收件人黑名单
        "mail_max_recipients",  # 单封收件人上限
        "mail_rate_limit_per_minute",  # 每收件人每分钟上限
    }
)

#: Redis 挂了时的退路。**只保当前进程**，重启即失效（回到 .env 的安全值）
_memory: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def _redis_key() -> str:
    return f"{get_settings().redis_key_prefix}:{_OVERRIDE_KEY}"


def load_overrides() -> dict[str, Any]:
    """读出当前覆盖。Redis 读不到就用内存里的那份。"""
    try:
        raw = get_redis(DB_GATEWAY).get(_redis_key())
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {k: v for k, v in parsed.items() if k in OVERRIDABLE}
    except Exception as exc:  # noqa: BLE001
        # Redis 不可用不该让"看一眼策略"这个动作失败 —— 降级到内存副本
        logger.debug("邮件策略覆盖读失败，用内存副本：%s", exc)
    return dict(_memory)


def patch_overrides(patch: dict[str, Any]) -> dict[str, Any]:
    """合并写入覆盖，返回**合并后**的完整覆盖。

    ``patch`` 必须已经过 `validate_patch` —— 这里不再校验，
    校验和落盘分开是为了让"改了什么"能先算出来写进审计。
    """
    current = load_overrides()
    current.update(patch)
    # 传 None 表示"恢复 .env 的值"，落到存储里就是把这个键删掉
    merged = {k: v for k, v in current.items() if v is not None}

    _memory.clear()
    _memory.update(merged)
    try:
        get_redis(DB_GATEWAY).set(_redis_key(), json.dumps(merged, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("邮件策略覆盖没能写进 Redis，只在内存里生效（重启后回到 .env）：%s", exc)
    return merged


def clear_overrides() -> None:
    """清空全部覆盖，回到 ``.env``。"""
    _memory.clear()
    try:
        get_redis(DB_GATEWAY).delete(_redis_key())
    except Exception as exc:  # noqa: BLE001
        logger.debug("邮件策略覆盖清理失败（忽略）：%s", exc)


def validate_patch(patch: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """校验入参，返回 ``(合法部分, 错误说明)``。

    **不抛异常**：调用方要把"哪几项没生效"原样回给界面，
    抛异常只会变成一句 422，用户不知道自己哪一项白填了。
    """
    accepted: dict[str, Any] = {}
    errors: list[str] = []

    for key, value in patch.items():
        if key not in OVERRIDABLE:
            errors.append(f"{key} 不允许运行时修改（只能通过 .env 配置）")
            continue
        if value is None:
            accepted[key] = None
            continue
        if key in ("mail_enabled", "mail_dry_run"):
            if isinstance(value, bool):
                accepted[key] = value
            else:
                errors.append(f"{key} 必须是 true/false")
        elif key.endswith("list"):
            if isinstance(value, str):
                accepted[key] = value.strip()
            else:
                errors.append(f"{key} 必须是逗号分隔的字符串")
        else:
            try:
                number = int(value)
            except (TypeError, ValueError):
                errors.append(f"{key} 必须是整数")
                continue
            if key == "mail_max_recipients" and not 1 <= number <= 100:
                errors.append(f"{key} 必须在 1~100 之间")
            elif key == "mail_rate_limit_per_minute" and not 1 <= number <= 120:
                errors.append(f"{key} 必须在 1~120 之间")
            else:
                accepted[key] = number

    return accepted, errors


# ---------------------------------------------------------------------------
# 生效策略
# ---------------------------------------------------------------------------
def _split_csv(raw: str) -> tuple[str, ...]:
    """逗号分隔 → 元组。和配置里那份同义，这里不import它是因为它在 config 里是私有的。"""
    return tuple(p.strip() for p in raw.split(",") if p.strip())


class EffectivePolicy:
    """``.env`` 配置 + 运行时覆盖 = 生效策略。

    为什么要单独一个类而不是改 Settings：闸门拿到的是 **policy 对象**，
    它只要求"有这些属性"。用代理包一层，覆盖就只影响读到的值，
    ``Settings`` 本身保持不可变 —— 别人拿到的还是配置原值。
    """

    def __init__(self, settings: Settings, overrides: dict[str, Any] | None = None) -> None:
        self._settings = settings
        self._overrides = dict(overrides or {})

    def __getattr__(self, name: str) -> Any:
        # 只对**没有在本类里显式定义**的属性走代理（否则会和 property 抢）
        return getattr(self._settings, name)

    @property
    def overrides(self) -> dict[str, Any]:
        return dict(self._overrides)

    def _get(self, key: str) -> Any:
        return self._overrides.get(key, getattr(self._settings, key))

    # ---- 被覆盖的键 ----
    @property
    def mail_enabled(self) -> bool:
        return bool(self._get("mail_enabled"))

    @property
    def mail_dry_run(self) -> bool:
        return bool(self._get("mail_dry_run"))

    @property
    def mail_max_recipients(self) -> int:
        return int(self._get("mail_max_recipients"))

    @property
    def mail_rate_limit_per_minute(self) -> int:
        return int(self._get("mail_rate_limit_per_minute"))

    # ---- 派生值（必须在这一层算，不能代理到 Settings）----
    #
    # `Settings.mail_allowlist` 读的是 `self.mail_send_allowlist`，
    # 而那个 self 是 Settings 实例 —— 代理过去会读到**未覆盖**的值，
    # 表现为"界面上改了白名单但闸门还在用旧的"。派生属性必须在这里重写。
    @property
    def mail_allowlist(self) -> tuple[str, ...]:
        raw = self._overrides.get("mail_send_allowlist")
        return _split_csv(raw) if isinstance(raw, str) else self._settings.mail_allowlist

    @property
    def mail_denylist(self) -> tuple[str, ...]:
        raw = self._overrides.get("mail_send_denylist")
        return _split_csv(raw) if isinstance(raw, str) else self._settings.mail_denylist

    @property
    def mail_can_really_send(self) -> bool:
        return self.mail_enabled and not self.mail_dry_run and bool(self._settings.smtp_host)

    def mail_policy_summary(self) -> dict[str, object]:
        """给界面和模型看的摘要 —— 不含任何密码。

        多返回两个键是刻意：`overrides` 说明"哪些值不是 .env 来的"，
        `override_source` 说明这份覆盖存在哪。否则运维看到的白名单
        和 .env 里写的不一致，会以为是同步出了 bug。
        """
        summary = self._settings.mail_policy_summary()
        summary.update(
            {
                "enabled": self.mail_enabled,
                "dry_run": self.mail_dry_run,
                "allowlist": list(self.mail_allowlist),
                "denylist": list(self.mail_denylist),
                "max_recipients": self.mail_max_recipients,
                "rate_limit_per_minute": self.mail_rate_limit_per_minute,
                "smtp_configured": self.mail_can_really_send,
                "overrides": dict(self._overrides),
                "override_source": "redis" if _redis_reachable() else "memory",
            }
        )
        return summary


def _redis_reachable() -> bool:
    try:
        get_redis(DB_GATEWAY).ping()
    except Exception:  # noqa: BLE001
        return False
    return True


def current_policy(settings: Any = None) -> EffectivePolicy:
    """取当前生效策略（每次都重新读覆盖 —— 覆盖随时可能被别人改）。

    ``settings`` 由调用方**显式传进来**，而不是在这里 `get_settings()`：
    调用方手里的那份可能已经被替换过（测试注入临时配置、
    多租户按请求取配置）。这里自己去取会绕过它，
    表现为"明明改了配置，策略还是旧的"。
    """
    return EffectivePolicy(settings if settings is not None else get_settings(), load_overrides())


__all__ = [
    "OVERRIDABLE",
    "EffectivePolicy",
    "clear_overrides",
    "current_policy",
    "load_overrides",
    "patch_overrides",
    "validate_patch",
]
