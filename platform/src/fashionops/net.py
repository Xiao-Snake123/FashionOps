"""网络环境修正：让本地服务绕过系统代理。

## 这个坑是怎么来的

Windows 上 ``httpx`` / ``requests`` 在 ``trust_env=True``（默认值）时会去读
**系统代理设置**（注册表 ``Internet Settings``）。如果机器上装了 Clash /
公司 VPN / 抓包工具这类设置全局代理的软件，那么连
``http://127.0.0.1:6333`` 这种本机请求也会被丢给代理，代理不认识本地的
Qdrant，于是回一个 **502 Bad Gateway**。

迷惑之处在于：

* ``curl`` 不读系统代理，所以用 curl 测一切正常
* 服务日志里也看不出任何异常，因为请求根本没到 Qdrant
* 报错只有一句 "502 Bad Gateway"，很容易误判成 Qdrant 本身挂了

## 修法

把回环地址写进 ``NO_PROXY``。只影响本地服务；访问 ``api.deepseek.com``
这类外网地址仍然照常走代理，不会把需要代理才能上网的环境弄坏。
"""

from __future__ import annotations

import os

# 需要绕过代理的地址。0.0.0.0 也加上：有些服务绑定通配地址后
# 客户端真的会用 0.0.0.0 去连（Linux 上等价于 localhost）
LOCAL_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def ensure_localhost_bypasses_proxy(extra: str | None = None) -> str:
    """把回环地址并入 ``NO_PROXY``，返回最终值。

    幂等：已经配过就不会重复追加，也不会覆盖用户自己写的主机名。
    ``extra`` 用来追加额外需要绕过代理的地址（比如内网域名）。
    """
    current = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    parts = [p.strip() for p in current.split(",") if p.strip()]

    wanted = list(LOCAL_HOSTS)
    if extra:
        wanted.extend(p.strip() for p in extra.split(",") if p.strip())

    missing = [h for h in wanted if h not in parts]
    if missing:
        parts.extend(missing)
        value = ",".join(parts)
        # 大小写两种都写：不同库读的键名不一样
        os.environ["NO_PROXY"] = value
        os.environ["no_proxy"] = value

    return os.environ.get("NO_PROXY", "")


def detected_system_proxies() -> dict[str, str]:
    """读出系统代理设置，仅用于诊断输出（不包含任何凭据）。

    ``urllib`` 在 Windows 上会去读注册表，正是 httpx 的 ``trust_env`` 用的那份。
    ``/health`` 里带上它，"明明是本地服务却报 502" 这种问题一眼就能定位。
    """
    from urllib.request import getproxies

    return {k: v for k, v in getproxies().items() if v}
