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

#: 需要绕过代理的地址。0.0.0.0 也加上：有些服务绑定通配地址后
#: 客户端真的会用 0.0.0.0 去连（Linux 上等价于 localhost）
LOCAL_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1", "::1", "0.0.0.0")

#: 只关心这几种代理键。``no`` / ``no_proxy`` 是"排除列表"，不是代理本身 ——
#: 把它们当成代理汇报出去，会得出"检测到系统代理：{'no': ...}'" 这种废话诊断。
_PROXY_KEYS = ("all", "http", "https", "ftp")


def read_system_proxies() -> dict[str, str]:
    """读出**真实存在**的代理设置（不含 ``no`` 这一类排除项）。

    刻意同时读环境变量和注册表：

    * ``getproxies_registry()`` 读 Windows 注册表 —— 这是我们自己写不进去的地方，
      所以它反映的永远是用户的原始设置，不会被本模块的修改污染
    * ``getproxies_environment()`` 读环境变量 —— 用户可能显式配过
    """
    found: dict[str, str] = {}
    try:
        from urllib.request import getproxies_environment, getproxies_registry
    except ImportError:  # pragma: no cover - 极老的 Python
        return found

    for source in (getproxies_environment, getproxies_registry):
        try:
            found.update({k: v for k, v in source().items() if k in _PROXY_KEYS and v})
        except Exception:  # noqa: BLE001 - 平台不支持时忽略
            continue
    return found


def ensure_localhost_bypasses_proxy(extra: str | None = None) -> str:
    """让回环地址绕开系统代理，同时**保留**外网代理。返回最终的 ``NO_PROXY``。

    ## 为什么不能只写 NO_PROXY

    ``urllib.request.getproxies()`` 的实现是::

        return getproxies_environment() or getproxies_registry()

    注意那个 ``or``：**只要环境变量里存在任何代理相关的键，它就不再看注册表**。
    而 ``NO_PROXY`` 也算代理键。

    所以只写 ``NO_PROXY`` 会得到反效果：

    1. 我们写了 ``NO_PROXY=localhost,127.0.0.1,...``
    2. ``getproxies_environment()`` 返回 ``{'no': 'localhost,...'}``，非空
    3. ``getproxies()`` 直接返回它，**不再回落注册表**
    4. httpx 拿到的代理列表里一个可用代理都没有 → 连访问外部 API 也变成直连

    结果是：本地 502 修好了，但**用户的系统代理被悄悄关掉了**。
    如果他的网络环境需要代理才能访问外部 API，会表现成
    "配好了 Key 却一直超时"，而且极难联想到是本地绕过代理的改动引起的。

    ## 正确顺序

    先把注册表里的真实代理**显式搬进环境变量**，再追加 NO_PROXY。
    两边都保住：外网继续走代理，回环地址直连。

    幂等：已配置过的不重复追加，也不会覆盖用户自己写的主机名。
    """
    # ---- 1. 先把真实代理落到环境变量里 ----
    # 顺序很重要：必须在写 NO_PROXY **之前**做（理由见上）。
    for key, value in read_system_proxies().items():
        # setdefault：用户显式配过的环境变量优先，不要覆盖他
        os.environ.setdefault(f"{key.upper()}_PROXY", value)

    # ---- 2. 再追加回环地址 ----
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


def proxy_report() -> dict[str, object]:
    """给 ``/health`` 和自检脚本用的代理诊断。

    区分"上游代理"和"排除列表"两件事 —— 上一条命中的 bug 就是因为
    把后者当成了前者，输出了一句毫无意义的"检测到系统代理"。
    """
    upstream = read_system_proxies()
    return {
        "upstream": upstream,
        "no_proxy": os.environ.get("NO_PROXY", ""),
        "external_will_use_proxy": bool(upstream),
    }


def detected_system_proxies() -> dict[str, str]:
    """向后兼容的旧名字：只返回真实代理，不含 ``no``。"""
    return read_system_proxies()
    from urllib.request import getproxies

    return {k: v for k, v in getproxies().items() if v}
