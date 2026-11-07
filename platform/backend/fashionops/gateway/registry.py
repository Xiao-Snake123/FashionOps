"""工具注册表 —— 网关调用的工具有哪些、长什么样、要什么权限。

## 命名与 LLM 的坑

内部工具名用点号分层：``erp.query_inventory``、``ad.update_budget``。
但 **OpenAI 的 function name 只允许 `[a-zA-Z0-9_-]`，点号会被直接拒绝**。
所以对外暴露给模型时要把 ``.`` 换成 ``__``（``erp__query_inventory``），
收到模型返回的名称再换回来。

这个坑很隐蔽：本地用 mock 模型时不会报错，真接上 OpenAI 兼容的模型
才发现整批工具都注册不上。所以映射逻辑放在注册表里统一做。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

#: 点号 ↔ 双下划线的互转。写在一处，避免两边不一致。
LLM_NAME_SEPARATOR = "__"


def to_llm_name(name: str) -> str:
    """``erp.query_inventory`` → ``erp__query_inventory``"""
    return name.replace(".", LLM_NAME_SEPARATOR)


def from_llm_name(llm_name: str) -> str:
    """``erp__query_inventory`` → ``erp.query_inventory``"""
    return llm_name.replace(LLM_NAME_SEPARATOR, ".")


class Scope:
    """权限作用域。

    故意用朴素的字符串常量而不是 Enum：Agent 的权限配置最终来自配置文件
    或数据库，字符串更好写也更好合并。
    """

    ERP_READ = "erp:read"
    ERP_WRITE = "erp:write"
    ECOM_READ = "ecom:read"
    ECOM_WRITE = "ecom:write"
    AD_READ = "ad:read"
    AD_WRITE = "ad:write"
    KB_READ = "kb:read"
    ASSET_WRITE = "asset:write"
    MAIL_SEND = "mail:send"

    #: 外部办公能力（由 mcp-servers 项目提供，经网关接入）。
    #: 只读动作给 office:read，会改变外部世界的（发信 / 建日程 / 写文件）给 office:write。
    #: 这样「谁能发邮件、谁能改日程」由平台 RBAC 统一管，而不是每个办公服务器各搞一套。
    OFFICE_READ = "office:read"
    OFFICE_WRITE = "office:write"

    ALL = frozenset(
        {
            ERP_READ,
            ERP_WRITE,
            ECOM_READ,
            ECOM_WRITE,
            AD_READ,
            AD_WRITE,
            KB_READ,
            ASSET_WRITE,
            MAIL_SEND,
            OFFICE_READ,
            OFFICE_WRITE,
        }
    )


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整描述。"""

    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[..., Any]

    #: 调用这个工具需要哪些作用域。**满足任意一个即可**（OR 语义）。
    #: 空集合表示不需要权限，但要显式写成空集合，不能省略 ——
    #: 省略会让人以为"忘了写"，显式空集让人确认"确实不需要"。
    scopes: frozenset[str] = frozenset()

    #: 只读工具可以 fail-open（限流组件挂了也放行），写工具必须 fail-closed。
    read_only: bool = False

    #: 幂等工具带 idempotency_key 时，重复调用返回首次结果而不是重做一遍。
    #: 商品上架必须是幂等的 —— 否则重试会刷出两个商品。
    idempotent: bool = False

    timeout_s: float = 10.0
    source: str = "mock"
    tags: tuple[str, ...] = field(default_factory=tuple)

    #: 单工具限流覆盖。``None`` 表示用全局默认值。
    #: 报表导出这类重接口要收紧，查库存这类轻接口可以放宽。
    rate_limit_per_minute: int | None = None

    @property
    def llm_name(self) -> str:
        return to_llm_name(self.name)

    @property
    def is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.handler)

    def openai_schema(self) -> dict[str, Any]:
        """转成 OpenAI function calling 的工具描述。

        ``strict`` 打开后模型必须严格按 schema 输出，省掉一大类参数解析失败。
        代价是 schema 里的可选字段要写全 ``required``，所以只在参数简单时开。
        """
        return {
            "type": "function",
            "function": {
                "name": self.llm_name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }


class ToolNotFound(KeyError):
    """工具没注册。网关会把它转成 ``TOOL_NOT_FOUND`` 的结构化错误。"""


class ToolRegistry:
    """工具注册表。

    进程内单例用法见文件末尾的 ``registry``。单测里可以自己 new 一个，
    避免测试之间互相污染。
    """

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    # ---- 注册 ----
    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"工具重名：{spec.name}（已注册，检查是不是重复导入）")
        if spec.timeout_s <= 0:
            raise ValueError(f"工具 {spec.name} 的 timeout_s 必须为正数")
        self._specs[spec.name] = spec

    def tool(
        self,
        *,
        name: str,
        description: str,
        args_model: type[BaseModel],
        scopes: Iterable[str] = (),
        read_only: bool = False,
        idempotent: bool = False,
        timeout_s: float = 10.0,
        source: str = "mock",
        tags: Iterable[str] = (),
        rate_limit_per_minute: int | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """装饰器写法：``@registry.tool(name="erp.query_inventory", ...)``"""

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.register(
                ToolSpec(
                    name=name,
                    description=description,
                    args_model=args_model,
                    handler=func,
                    scopes=frozenset(scopes),
                    read_only=read_only,
                    idempotent=idempotent,
                    timeout_s=timeout_s,
                    source=source,
                    tags=tuple(tags),
                    rate_limit_per_minute=rate_limit_per_minute,
                )
            )
            return func

        return decorator

    # ---- 查询 ----
    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise ToolNotFound(name) from exc

    def find_by_llm_name(self, llm_name: str) -> ToolSpec:
        return self.get(from_llm_name(llm_name))

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __len__(self) -> int:
        return len(self._specs)

    def specs(self) -> list[ToolSpec]:
        return sorted(self._specs.values(), key=lambda s: s.name)

    def names(self) -> list[str]:
        return [s.name for s in self.specs()]

    # ---- 对 LLM 暴露 ----
    def openai_schemas(self, granted_scopes: frozenset[str] | None = None) -> list[dict[str, Any]]:
        """生成工具清单。

        传了 ``granted_scopes`` 就**只暴露调用方有权限的工具**。
        在网关层过滤掉，比让模型看见再被拒绝要好：模型看到工具就会去调，
        调了被拒就浪费一整轮 token，还容易陷入重试循环。
        """
        specs = self.specs()
        if granted_scopes is not None:
            specs = [s for s in specs if not s.scopes or (s.scopes & granted_scopes)]
        return [s.openai_schema() for s in specs]

    def describe(self) -> list[dict[str, Any]]:
        """给 ``/api/v1/tools`` 用的可读清单。"""
        return [
            {
                "name": s.name,
                "llm_name": s.llm_name,
                "description": s.description,
                "scopes": sorted(s.scopes),
                "read_only": s.read_only,
                "idempotent": s.idempotent,
                "timeout_s": s.timeout_s,
                "source": s.source,
                "tags": list(s.tags),
                "rate_limit_per_minute": s.rate_limit_per_minute,
                "is_async": s.is_async,
            }
            for s in self.specs()
        ]


#: 进程内默认注册表。业务工具通过 ``register_tools()`` 往这里注册。
registry = ToolRegistry()
