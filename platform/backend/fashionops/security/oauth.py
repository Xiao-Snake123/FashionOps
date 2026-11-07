"""钉钉 / 企业微信扫码登录 —— **适配位，未实现真实调用**。

## 这份代码现在能做什么、不能做什么

| | 状态 |
| --- | --- |
| 数据模型（`external_identities` 表） | ✅ 已建，能绑定"同一个人的两种登录方式" |
| 配置项（corpId / agentId / secret） | ✅ 已加，`fashionops-config check` 会校验组合是否完整 |
| OAuth 流程抽象（取 code → 换 userid → 取资料 → 绑定） | ✅ 接口和本地实现都在，能跑通测试 |
| **真实 HTTP 调用第三方** | ❌ **没有**。调用会抛 `NotImplementedError` 并说明要改哪里 |

## 为什么留"能配但不用"而不是直接删掉

因为删掉之后，真接入时要动的地方**不止一处**：数据模型要加表、配置要加字段、
前端要加按钮和回调页、登录接口要加一个新分支。留成现在这样，
真接入时只需要：

1. 在 `DingTalkProvider.exchange_code()` 里填 3 个 HTTP 调用（代码里有注释说明端点）
2. 在 `.env` 里填 `FASHIONOPS_DINGTALK_CLIENT_ID` / `_SECRET` / `_CORP_ID`
3. 前端把"扫码登录"按钮接上 `GET /api/v1/auth/oauth/dingtalk/authorize`

—— 数据模型、绑定逻辑、会话签发、权限加载**全都不用改**，
因为它们走的是和密码登录**完全同一条**后半程。

## 设计上唯一值得说的点：两条登录路径必须汇合到同一个 User

扫码用户和密码用户是**同一个人**，不能各建一个账号 ——
那样权限要维护两份，离职时漏删一个就是一条后门。

所以流程是：
```
第三方扫码 → 拿到 (provider, external_id)
  ├─ external_identities 里有 → 直接找到 User
  └─ 没有 → 按邮箱/手机号匹配现有 User（企业目录里的邮箱通常和系统一致）
             ├─ 匹配到 → 插入绑定关系
             └─ 没匹配到 → 按配置决定「自动建号」还是「拒绝并提示先由管理员建号」
```
默认**拒绝自动建号**（`auto_create=False`）：开放的自动注册意味着
任何能访问企业目录的人都能进系统，而权限是靠角色给的 —— 新号没有角色，
进去了也是个空壳，反而会让管理员为了"能用"而随手给个高权限角色。
**先建号、再授权**是更安全的顺序。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

logger = logging.getLogger("fashionops.security.oauth")


class OAuthError(Exception):
    """扫码登录流程失败。`message` 给用户，`reason` 给日志。"""

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class OAuthProfile:
    """从第三方拿到的用户资料。**归一化之后的形态**。

    各家的字段名都不一样（钉钉叫 `unionid`，企业微信叫 `userid`，
    OIDC 叫 `sub`），归一化在这里做掉，绑定逻辑就不用写 if-else 分支。
    """

    provider: str
    external_id: str
    union_id: str | None = None
    display_name: str = ""
    email: str | None = None
    phone: str | None = None
    #: 第三方返回的原始 JSON。**存下来**是为了排查"为什么这个人没对上" ——
    #: 每次重新拉取既慢，而且第三方可能随时改字段。
    raw: dict[str, Any] | None = None


class OAuthProvider(Protocol):
    """扫码登录提供方。加一家新平台 = 实现这一个协议。"""

    name: str

    @property
    def configured(self) -> bool:
        """配置是否完整。不完整时前端不显示这个登录方式。"""
        ...

    def authorize_url(self, *, state: str) -> str:
        """拼出跳转到第三方的授权页地址。"""
        ...

    def exchange_code(self, code: str) -> OAuthProfile:
        """用回调里的 code 换用户资料。"""
        ...


# ---------------------------------------------------------------------------
# 钉钉
# ---------------------------------------------------------------------------
class DingTalkProvider:
    """钉钉扫码登录。

    ## 真接入时要填的三个调用（都在 `exchange_code` 里）

    1. `POST https://api.dingtalk.com/v1.0/oauth2/userAccessToken`
       body: `{clientId, clientSecret, code, grantType: "authorization_code"}`
       → 拿到 `accessToken`
    2. `GET https://api.dingtalk.com/v1.0/contact/users/me`
       header: `x-acs-dingtalk-access-token: <accessToken>`
       → 拿到 `unionId` / `openId` / `nick` / `email` / `mobile`
    3. （可选，拿企业内 userid）
       `POST https://oapi.dingtalk.com/topapi/v2/user/getbyunionid`
       → `result.userid`

    `external_id` 建议用 **unionid**：同一个企业里不同应用之间它是一致的，
    用 openid 的话换应用会得到不同的值，用户就"对不上号"了。

    ## 为什么要 `state`

    回调地址是公开的，任何人都能构造一个 `?code=xxx` 打过来。
    `state` 是我们自己生成的随机串，存在前端（或服务端会话）里，
    回调时比对 —— 不比对的话就是 **CSRF**：攻击者可以用自己的钉钉账号
    完成授权，然后把回调地址发给受害者，受害者的浏览器一访问，
    就把攻击者的身份绑到了受害者当前登录的账号上。
    """

    name = "dingtalk"

    def __init__(
        self,
        *,
        client_id: str = "",
        client_secret: str | None = None,
        corp_id: str = "",
        redirect_uri: str = "",
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._corp_id = corp_id
        self._redirect_uri = redirect_uri

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret and self._corp_id)

    def authorize_url(self, *, state: str) -> str:
        if not self.configured:
            raise OAuthError(
                "钉钉登录未配置（缺 client_id / client_secret / corp_id）",
                reason="not_configured",
            )
        # 服务端直跳的写法。前端 JS SDK（DTFrameLogin）能免去 corpId 暴露，
        # 但需要引入钉钉的 JS 文件；这里给的是不依赖前端 SDK 的版本。
        query = urlencode(
            {
                "redirect_uri": self._redirect_uri,
                "response_type": "code",
                "client_id": self._client_id,
                "scope": "openid",
                "state": state,
                "prompt": "consent",
            }
        )
        return f"https://login.dingtalk.com/oauth2/auth?{query}"

    def exchange_code(self, code: str) -> OAuthProfile:
        raise NotImplementedError(
            "钉钉登录未接入真实调用（目前只有适配位）。\n"
            "要接入：在 security/oauth.py 的 DingTalkProvider.exchange_code() 里，\n"
            "按类 docstring 里的 3 个端点实现（用 fashionops.net 里配好代理绕过的 httpx 客户端）。\n"
            "数据模型、绑定逻辑、令牌签发都不用改 —— 见模块 docstring。"
        )


# ---------------------------------------------------------------------------
# 企业微信
# ---------------------------------------------------------------------------
class WeComProvider:
    """企业微信扫码登录。同 DingTalkProvider，未实现真实调用。

    真接入的端点：
    1. `GET https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=&corpsecret=`
       → `access_token`（**注意它有 2 小时有效期，且拿太频繁会被限流，
          需要缓存** —— 这是企业微信最容易踩的坑）
    2. 前端用 `wework_admin.qrcode` 或 `open.work.weixin.qq.com/wwopen/sso/qrConnect`
       拿 code
    3. `GET https://qyapi.weixin.qq.com/cgi-bin/auth/getuserinfo?access_token=&code=`
       → `userid`
    4. `GET https://qyapi.weixin.qq.com/cgi-bin/user/get?access_token=&userid=`
       → 完整资料

    `external_id` 用 `userid`。企业微信没有 unionid 概念的对应物，
    所以 `union_id` 留空。
    """

    name = "wecom"

    def __init__(
        self,
        *,
        corp_id: str = "",
        agent_id: str = "",
        secret: str | None = None,
        redirect_uri: str = "",
    ) -> None:
        self._corp_id = corp_id
        self._agent_id = agent_id
        self._secret = secret
        self._redirect_uri = redirect_uri

    @property
    def configured(self) -> bool:
        return bool(self._corp_id and self._agent_id and self._secret)

    def authorize_url(self, *, state: str) -> str:
        if not self.configured:
            raise OAuthError(
                "企业微信登录未配置（缺 corp_id / agent_id / secret）",
                reason="not_configured",
            )
        query = urlencode(
            {
                "appid": self._corp_id,
                "agentid": self._agent_id,
                "redirect_uri": self._redirect_uri,
                "state": state,
            }
        )
        return f"https://open.work.weixin.qq.com/wwopen/sso/qrConnect?{query}"

    def exchange_code(self, code: str) -> OAuthProfile:
        raise NotImplementedError(
            "企业微信登录未接入真实调用（目前只有适配位）。\n"
            "要接入：实现 WeComProvider.exchange_code()，注意 access_token 要缓存"
            "（2 小时有效期 + 调用频率限制）。"
        )


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def get_provider(name: str, settings: Any) -> OAuthProvider:
    """按名字取 provider。名字非法时抛 `OAuthError` 而不是返回 `None` ——

    返回 None 会让调用方多写一个 `if provider is None`，
    而那个分支迟早有人忘了写，于是变成 AttributeError 500。
    """
    redirect = getattr(settings, "oauth_redirect_base", "") or ""
    key = (name or "").strip().lower()

    if key == "dingtalk":
        return DingTalkProvider(
            client_id=getattr(settings, "dingtalk_client_id", ""),
            client_secret=getattr(settings, "dingtalk_client_secret", None),
            corp_id=getattr(settings, "dingtalk_corp_id", ""),
            redirect_uri=f"{redirect}?provider=dingtalk",
        )
    if key == "wecom":
        return WeComProvider(
            corp_id=getattr(settings, "wecom_corp_id", ""),
            agent_id=getattr(settings, "wecom_agent_id", ""),
            secret=getattr(settings, "wecom_secret", None),
            redirect_uri=f"{redirect}?provider=wecom",
        )
    raise OAuthError(f"不支持的登录方式：{name}", reason="unknown_provider")


def available_providers(settings: Any) -> list[dict[str, Any]]:
    """列给前端：哪些登录方式**配置完整**了。

    只列配置完整的 —— 列出一个点了会报"未配置"的按钮，
    比不显示这个按钮体验更差。

    `implemented=False` 是刻意暴露的：前端可以据此把按钮置灰并提示
    "已规划但未接入"，而不是让用户点了才发现。
    """
    out: list[dict[str, Any]] = []
    for name, implemented in (("dingtalk", False), ("wecom", False)):
        try:
            provider = get_provider(name, settings)
        except OAuthError:
            continue
        out.append(
            {
                "provider": name,
                "configured": provider.configured,
                "implemented": implemented,
                "hint": (
                    "已留好适配位：数据模型与配置项就绪，真实 HTTP 调用待接入（见 security/oauth.py）"
                    if not implemented
                    else ""
                ),
            }
        )
    return out


__all__ = [
    "DingTalkProvider",
    "OAuthError",
    "OAuthProfile",
    "OAuthProvider",
    "WeComProvider",
    "available_providers",
    "get_provider",
]
