"""向量库的**契约测试**。

## 为什么这样组织

同一个测试类跑两个后端（内存 / 真 Qdrant），而不是给每个实现各写一份。

理由：内存实现存在的**唯一目的**是"在单测里代替 Qdrant"。
如果两者行为不一致，那么"单测全绿"就毫无意义 ——
测试验的是一套逻辑，线上跑的是另一套。这比没有测试更糟，
因为它给出虚假的安全感。

契约测试强制两者回答同一组问题：

* 距离度量的方向（越大越相似，还是越小？）
* 过滤条件的语义（`must` / `any_of` / `must_not` 是 AND 还是 OR？）
* 维度不一致时的行为（报错？静默？）
* upsert 是否幂等
* 排序是否确定（同分时怎么办）

其中**排序确定性**这条特别值得测：浮点误差 + 哈希表遍历顺序会让
"同分记录"的返回顺序在两次运行间不同，表现为测试偶发失败。
这种 flaky 测试最后会被人加 `pytest.mark.skip` 关掉。
"""

from __future__ import annotations

import contextlib
import math

import pytest

from fashionops.vectors import (
    Filter,
    MemoryVectorStore,
    VectorRecord,
)
from fashionops.vectors.base import PAYLOAD_TEXT

DIM = 8
COLLECTION = "pytest_vectors_contract"


def _vec(*values: float) -> list[float]:
    """补零到固定维度。让测试用例写起来短一点。"""
    assert len(values) <= DIM
    return list(values) + [0.0] * (DIM - len(values))


def _unit(dim: int, index: int) -> list[float]:
    """第 index 维为 1 的单位向量。两两正交，余弦相似度为 0。"""
    vec = [0.0] * dim
    vec[index] = 1.0
    return vec


@pytest.fixture(scope="session", params=["memory", "qdrant"])
def _backend(request):  # noqa: ANN001, ANN201
    """按后端类型建**一个**实例，整个测试会话复用。

    为什么是 session 级：真 Qdrant 里建立 collection 要为几个字段建 payload 索引，
    每次都是带 ``wait=True`` 的往返请求。改成每个用例重建一次的话，
    19 个用例 × 4 个索引 = 76 次阻塞往返，测试从几秒变成三分钟 ——
    而**慢测试的下场是没人跑它**。

    所以：collection 建一次，每个用例只清空里面的点。
    """
    if request.param == "memory":
        store = MemoryVectorStore()
    else:
        from fashionops.config import get_settings
        from fashionops.vectors import QdrantVectorStore

        settings = get_settings()
        store = QdrantVectorStore(url=settings.qdrant_url, timeout=settings.qdrant_timeout)
        ok, detail = store.healthy()
        if not ok:
            pytest.skip(f"Qdrant 连不上，跳过真组件测试：{detail}")

    store.ensure_collection(COLLECTION, dimension=DIM)
    store.delete(COLLECTION)
    yield store

    if request.param == "qdrant":
        with contextlib.suppress(Exception):
            store.drop_collection(COLLECTION)
        store.close()


@pytest.fixture
def store(_backend):  # noqa: ANN001, ANN201
    """每个用例拿到干净的 collection，但复用同一个连接。

    这里也调一次 ``ensure_collection``：有人（比如 ``test_drop_collection``）
    可能把 collection 整个删掉，下一个用例得能自愈。
    已存在时它只是查一下元信息，很便宜。
    """
    _backend.ensure_collection(COLLECTION, dimension=DIM)
    _backend.delete(COLLECTION)
    return _backend


class TestContract:
    def test_upsert_returns_logical_id_not_storage_id(self, store):  # noqa: ANN001
        """**契约测试发现的真 bug**：两个后端返回的 id 曾经不一样。

        Qdrant 的 point id 只接受整数或 UUID，所以适配器会把 ``kb-0001#3``
        转成确定的 UUID。第一版直接把存储层 id 返回，于是：

        * 内存实现返回 ``"near"``
        * Qdrant 返回 ``"6fa7f901-802c-58e9-..."``

        调用方拿到的 id 既无法反推来源，两个后端还互不一致 ——
        而**只跑一个后端时完全看不出来**。

        修法：写入时把逻辑 id 存进 payload，检索时原样还回去。
        存储层怎么编码 id 是实现细节，不该泄到接口上。
        """
        store.upsert(
            COLLECTION,
            [
                VectorRecord(id="kb-0001#0", vector=_unit(DIM, 0), payload={PAYLOAD_TEXT: "A"}),
                VectorRecord(id="kb-0002#3", vector=_unit(DIM, 1), payload={PAYLOAD_TEXT: "B"}),
            ],
        )
        hits = store.search(COLLECTION, _unit(DIM, 0), limit=1)
        assert hits[0].id == "kb-0001#0"

        # 内部约定字段不该露给调用方
        assert "_id" not in hits[0].payload

    def test_upsert_and_count(self, store):  # noqa: ANN001
        assert store.count(COLLECTION) == 0
        store.upsert(
            COLLECTION,
            [
                VectorRecord(id="a", vector=_unit(DIM, 0), payload={PAYLOAD_TEXT: "AAA"}),
                VectorRecord(id="b", vector=_unit(DIM, 1), payload={PAYLOAD_TEXT: "BBB"}),
            ],
        )
        assert store.count(COLLECTION) == 2

    def test_upsert_is_idempotent_on_same_id(self, store):  # noqa: ANN001
        """同 id 重复写入必须是覆盖而不是新增。

        建索引天然要支持重跑。不幂等的话重跑一次库里就多一份，
        检索时同一段内容被召回三次，top-k 里全是重复。
        """
        record = VectorRecord(id="a", vector=_unit(DIM, 0), payload={PAYLOAD_TEXT: "第一版"})
        store.upsert(COLLECTION, [record])
        store.upsert(COLLECTION, [record])
        assert store.count(COLLECTION) == 1

        # 而且内容应该是新的那份
        hits = store.search(COLLECTION, _unit(DIM, 0), limit=1)
        assert hits[0].payload[PAYLOAD_TEXT] == "第一版"

        store.upsert(
            COLLECTION,
            [VectorRecord(id="a", vector=_unit(DIM, 0), payload={PAYLOAD_TEXT: "第二版"})],
        )
        hits = store.search(COLLECTION, _unit(DIM, 0), limit=1)
        assert hits[0].payload[PAYLOAD_TEXT] == "第二版", "upsert 应该是覆盖"

    def test_search_orders_by_similarity_desc(self, store):  # noqa: ANN001
        """**核心契约**：score 越大越相似。

        Qdrant 对 cosine/dot 返回相似度、对 euclid/manhattan 返回距离 ——
        方向相反。适配器必须归一化，否则用欧氏距离时"取第一条"拿到的是
        最不相似的结果，而且不报错，只是结果莫名其妙。
        """
        store.upsert(
            COLLECTION,
            [
                VectorRecord(id="near", vector=_vec(1.0, 0.1)),
                VectorRecord(id="far", vector=_vec(0.1, 1.0)),
            ],
        )
        hits = store.search(COLLECTION, _vec(1.0, 0.0), limit=2)
        assert [h.id for h in hits] == ["near", "far"]
        assert hits[0].score > hits[1].score

    def test_search_respects_limit(self, store):  # noqa: ANN001
        store.upsert(
            COLLECTION,
            [VectorRecord(id=f"v{i}", vector=_unit(DIM, i)) for i in range(DIM)],
        )
        assert len(store.search(COLLECTION, _unit(DIM, 0), limit=3)) == 3

    def test_search_on_empty_collection_returns_empty(self, store):  # noqa: ANN001
        assert store.search(COLLECTION, _unit(DIM, 0), limit=5) == []

    def test_filter_must(self, store):  # noqa: ANN001
        store.upsert(
            COLLECTION,
            [
                VectorRecord(id="1", vector=_unit(DIM, 0), payload={"cat": "退货", PAYLOAD_TEXT: "x"}),
                VectorRecord(id="2", vector=_unit(DIM, 0), payload={"cat": "物流", PAYLOAD_TEXT: "y"}),
            ],
        )
        hits = store.search(COLLECTION, _unit(DIM, 0), limit=5, filter=Filter(must={"cat": "退货"}))
        assert [h.id for h in hits] == ["1"]

    def test_filter_any_of(self, store):  # noqa: ANN001
        store.upsert(
            COLLECTION,
            [
                VectorRecord(id="1", vector=_unit(DIM, 0), payload={"cat": "退货"}),
                VectorRecord(id="2", vector=_unit(DIM, 0), payload={"cat": "物流"}),
                VectorRecord(id="3", vector=_unit(DIM, 0), payload={"cat": "尺码"}),
            ],
        )
        hits = store.search(
            COLLECTION, _unit(DIM, 0), limit=5, filter=Filter(any_of={"cat": ["退货", "物流"]})
        )
        assert sorted(h.id for h in hits) == ["1", "2"]

    def test_filter_must_not(self, store):  # noqa: ANN001
        store.upsert(
            COLLECTION,
            [
                VectorRecord(id="1", vector=_unit(DIM, 0), payload={"cat": "退货"}),
                VectorRecord(id="2", vector=_unit(DIM, 0), payload={"cat": "物流"}),
            ],
        )
        hits = store.search(COLLECTION, _unit(DIM, 0), limit=5, filter=Filter(must_not={"cat": "退货"}))
        assert [h.id for h in hits] == ["2"]

    def test_empty_filter_matches_everything(self, store):  # noqa: ANN001
        """空 filter 必须等价于"不过滤"。

        有些后端在拿到空条件时会生成一个恒假的条件，结果一条都召不回 ——
        而且不会报错，只是检索"什么都没有"。
        """
        store.upsert(
            COLLECTION,
            [VectorRecord(id="1", vector=_unit(DIM, 0)), VectorRecord(id="2", vector=_unit(DIM, 0))],
        )
        assert len(store.search(COLLECTION, _unit(DIM, 0), limit=5, filter=Filter())) == 2

    def test_with_payload_false(self, store):  # noqa: ANN001
        store.upsert(COLLECTION, [VectorRecord(id="1", vector=_unit(DIM, 0), payload={"k": "v"})])
        hits = store.search(COLLECTION, _unit(DIM, 0), limit=1, with_payload=False)
        assert hits[0].payload == {}

    def test_delete_by_ids(self, store):  # noqa: ANN001
        store.upsert(
            COLLECTION,
            [VectorRecord(id="1", vector=_unit(DIM, 0)), VectorRecord(id="2", vector=_unit(DIM, 1))],
        )
        store.delete(COLLECTION, ids=["1"])
        assert store.count(COLLECTION) == 1

    def test_delete_all(self, store):  # noqa: ANN001
        store.upsert(
            COLLECTION,
            [VectorRecord(id=f"v{i}", vector=_unit(DIM, i)) for i in range(3)],
        )
        store.delete(COLLECTION)
        assert store.count(COLLECTION) == 0

    def test_delete_all_keeps_collection_queryable(self, store):  # noqa: ANN001
        """清空之后 collection 还得能继续用（重建索引就靠这个）。"""
        store.upsert(COLLECTION, [VectorRecord(id="1", vector=_unit(DIM, 0))])
        store.delete(COLLECTION)
        store.upsert(COLLECTION, [VectorRecord(id="2", vector=_unit(DIM, 0))])
        assert store.count(COLLECTION) == 1

    def test_drop_collection_removes_structure(self, store):  # noqa: ANN001
        """``drop_collection`` 是拆结构（连维度/索引一起扔），和清空不是一回事。

        为什么必须分开：换 embedding 模型时维度变了，旧 collection 的
        ``size`` 已经固定，改不了，只能删掉重建。如果 ``delete()`` 隐含删结构，
        那重建索引（``fashionops-kb index --rebuild``）就会顺手把索引配置也扔了，
        每次都白建一遍 payload 索引 —— 又慢又没必要。
        """
        store.upsert(COLLECTION, [VectorRecord(id="1", vector=_unit(DIM, 0))])
        assert store.drop_collection(COLLECTION) is True
        # 删完之后：不存在，且 count/info 退化返回而不是抛异常
        assert store.info(COLLECTION).exists is False
        assert store.count(COLLECTION) == 0
        # 再删一次返回 False（幂等，方便脚本反复调）
        assert store.drop_collection(COLLECTION) is False

    def test_info_reports_dimension_and_count(self, store):  # noqa: ANN001
        store.upsert(COLLECTION, [VectorRecord(id="1", vector=_unit(DIM, 0))])
        info = store.info(COLLECTION)
        assert info.exists
        assert info.dimension == DIM
        assert info.vector_count == 1

    def test_info_on_missing_collection(self, store):  # noqa: ANN001
        info = store.info("definitely_not_here")
        assert not info.exists
        assert info.vector_count == 0

    def test_healthy(self, store):  # noqa: ANN001
        ok, detail = store.healthy()
        assert ok
        # detail 必须带上有用的信息，不能只说 "OK"
        assert detail

    def test_dimension_mismatch_on_upsert_is_loud(self, store):  # noqa: ANN001
        """维度不对必须**抛错**，不能静默截断或补零。

        静默处理的话，向量会以一种"看起来能跑"的方式损坏：
        检索永远返回垃圾结果，但不报任何错。
        """
        with pytest.raises((ValueError, Exception)):  # noqa: B017
            store.upsert(COLLECTION, [VectorRecord(id="x", vector=[1.0, 2.0, 3.0])])

    def test_ensure_collection_dimension_conflict_raises(self, store):  # noqa: ANN001
        """已存在的 collection 维度不一致时要报错，**不能静默重建**。

        重建会丢掉全部已有向量，而调用方（比如一次普通的服务启动）
        根本没想到会触发数据丢失。
        """
        with pytest.raises(ValueError, match="维度"):
            store.ensure_collection(COLLECTION, dimension=DIM * 2)

    def test_ensure_collection_is_idempotent(self, store):  # noqa: ANN001
        assert store.ensure_collection(COLLECTION, dimension=DIM) is False, (
            "已存在的 collection 不该报告为'新建了'"
        )

    def test_ties_are_deterministic(self, store):  # noqa: ANN001
        """同分记录的返回顺序必须**稳定**。

        不稳定的话，同一份数据两次运行会给出不同排序，
        测试变成偶发失败，最后被人加 skip 关掉 —— 而真正的问题还在。
        """
        records = [
            VectorRecord(id=f"tie-{i}", vector=_unit(DIM, 0), payload={PAYLOAD_TEXT: f"t{i}"})
            for i in range(5)
        ]
        store.upsert(COLLECTION, records)

        first = [h.id for h in store.search(COLLECTION, _unit(DIM, 0), limit=5)]
        second = [h.id for h in store.search(COLLECTION, _unit(DIM, 0), limit=5)]
        assert first == second


class TestMemoryOnly:
    """内存实现特有的行为 —— 它不对 Qdrant 做同样要求。"""

    def test_search_on_missing_collection_raises_with_hint(self):
        store = MemoryVectorStore()
        with pytest.raises(KeyError, match="ensure_collection"):
            store.search("nope", _unit(DIM, 0))

    def test_cosine_similarity_is_actually_computed(self):
        """验证内存实现算的是**真的余弦相似度**，不是别的东西。"""
        store = MemoryVectorStore()
        store.ensure_collection("c", dimension=2)
        store.upsert("c", [VectorRecord(id="a", vector=[1.0, 0.0])])

        # 与 [1,0] 完全同向 → 相似度 1
        assert math.isclose(store.search("c", [2.0, 0.0], limit=1)[0].score, 1.0)
        # 正交 → 0
        assert math.isclose(store.search("c", [0.0, 5.0], limit=1)[0].score, 0.0)
        # 反向 → -1
        assert math.isclose(store.search("c", [-1.0, 0.0], limit=1)[0].score, -1.0)

    def test_zero_vector_is_handled(self):
        """零向量的余弦距离是未定义的，不能崩。

        空文本 / 纯标点在真实数据里很常见（比如一个只有表情符号的标题）。
        """
        store = MemoryVectorStore()
        store.ensure_collection("c", dimension=2)
        store.upsert("c", [VectorRecord(id="a", vector=[0.0, 0.0])])
        assert store.search("c", [1.0, 0.0], limit=1)[0].score == 0.0

    def test_euclid_distance_direction_is_normalized(self):
        """欧氏距离是"越小越好"，实现必须转成"越大越相似"。"""
        store = MemoryVectorStore()
        store.ensure_collection("c", dimension=2, distance="euclid")
        store.upsert(
            "c",
            [
                VectorRecord(id="near", vector=[0.0, 0.1]),
                VectorRecord(id="far", vector=[9.0, 9.0]),
            ],
        )
        hits = store.search("c", [0.0, 0.0], limit=2)
        assert hits[0].id == "near"
        assert hits[0].score > hits[1].score
