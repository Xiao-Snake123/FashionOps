"""Agent 层的依赖上下文。

这个文件原来有 6 组用例（状态、预算、上下文、checkpointer、HITL 图、
结构化对象），其中 5 组测的是**随旧执行架构一起删掉的东西**
（`state.py` 的 TaskState/预算/StepRecord、`checkpoint.py` 的图持久化）。
留着它们等于给已经不存在的行为写断言，所以删了。

留下的只有 `RunContext` —— 它**还在用**（客服 Agent 走它拿网关和模型），
所以它的契约要继续锁住。
"""

from __future__ import annotations

from fashionops.agents.context import RunContext


class TestRunContext:
    def test_carries_dependencies_without_being_state(self):
        """上下文是个**依赖集合**，不是状态。

        新架构里任务执行不再用它（loop 自己在函数里建网关和模型），
        但它仍然是客服 Agent 的入口参数 —— 这个形状不能随手改。
        """
        gateway = object()
        model = object()
        ctx = RunContext(
            gateway=gateway,
            model=model,
            actor="agent:service",
            scopes=frozenset({"erp:read"}),
            task_id=7,
        )

        assert ctx.gateway is gateway
        assert ctx.model is model
        assert ctx.actor == "agent:service"
        assert ctx.scopes == frozenset({"erp:read"})
        assert ctx.task_id == 7

    def test_is_not_part_of_any_serialized_state(self):
        """**不可序列化的东西不许进状态** —— 这条原则没变。

        旧架构用 checkpoint 落状态，塞进去会写挂；新架构虽然没有
        checkpoint 了，但"依赖与数据分开"的判据仍然成立：
        状态要能落库、能重放，网关和模型客户端不能。
        """
        ctx = RunContext(gateway=object(), model=object(), actor="test")
        assert not hasattr(ctx, "to_dict")

    def test_repr_is_readable(self):
        ctx = RunContext(
            gateway=None, model=None, actor="agent:service", scopes=frozenset({"erp:read"})
        )
        assert "agent:service" in repr(ctx)
        assert "erp:read" in repr(ctx)
