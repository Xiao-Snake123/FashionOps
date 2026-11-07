"""Embedding（文本向量化）客户端。

## 为什么单独一个模块，不塞进 ``llm/client.py``

看起来都是"调模型"，但两件事的差异比相似多：

* **协议不同**：embedding 是 ``POST /embeddings``，请求体是 ``{"input": [...]}``，
  响应是 ``{"data": [{"embedding": [...]}]}``。和 chat 的 messages/tools 完全是两套。
* **成本模型不同**：按 token 计费但没有输出 token，价格表要单独维护。
* **失败语义不同**：chat 的失败可以重试或降级成自然语言回答；
  embedding 失败则**整条检索链路没有输入**，必须让调用方明确知道。
* **可批量**：embedding 天然支持一次传一批文本（有上限），
  逐条调用会让建索引慢十倍。混在一起容易写成逐条。

## 默认维度 1024

对应智谱 ``embedding-2``。**维度必须和向量库 collection 的配置一致** ——
不一致的话 upsert 会直接报维度不匹配，而那个报错信息通常很晦涩
（Qdrant 只说 "Wrong input: Vector dimension error"，不告诉你期望多少）。

所以 ``VectorStore.ensure_collection`` 会把维度记下来，
后续 upsert 前能给出"你给的向量是 1536 维、collection 是 1024 维"这种能看的错误。
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Protocol

from fashionops.config import Settings, get_settings

logger = logging.getLogger("fashionops.llm.embeddings")

#: 一次请求最多发多少条文本。各家限制不同（智谱 64、OpenAI 2048），
#: 取一个保守值，超出的自动分批 —— 由客户端内部处理，调用方不用关心。
DEFAULT_BATCH_SIZE = 32


class EmbeddingError(Exception):
    """向量化失败。

    ``retryable`` 用来区分"再试一次可能好"（限流/超时/5xx）
    和"再试也没用"（维度非法/鉴权失败）。
    """

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class EmbeddingClient(Protocol):
    """向量化客户端协议。mock 和真实实现都满足它。"""

    #: 向量维度。**必须**和向量库 collection 一致。
    dimension: int

    #: 模型名，写进向量库的 payload 便于事后追溯"这批向量是什么模型生成的"
    model_name: str

    def embed(self, texts: list[str], *, batch_size: int | None = None) -> list[list[float]]:
        """把一批文本转成向量，顺序与输入一一对应。"""
        ...

    def embed_one(self, text: str) -> list[float]: ...


# ---------------------------------------------------------------------------
# 真实实现：OpenAI 兼容协议
# ---------------------------------------------------------------------------
class OpenAICompatEmbeddingClient:
    """走 OpenAI 兼容的 ``/embeddings`` 接口。

    智谱 / DeepSeek / 通义 / OpenAI 都遵循这个协议，差别只在 base_url 和模型名，
    所以一个实现能覆盖全部 —— 这也是 chat 层用同一套做法的原因。

    **用 ``openai`` SDK 而不是自己拼 httpx**：和 chat 层保持一致，
    鉴权头、错误结构、超时行为都不用我们自己维护一份。
    """

    provider = "openai_compat"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dimension: int,
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not api_key:
            raise EmbeddingError("缺少 embedding api_key", retryable=False)

        from openai import OpenAI

        self.model_name = model
        self.dimension = dimension
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=timeout,
            # 和 chat 层同样的理由：关掉 SDK 自带重试。
            # 它和上层重试策略会叠加，一次调用实际发好几次请求，
            # 成本和延迟都翻倍，而且日志里看不出来。
            max_retries=0,
        )

    def embed(self, texts: list[str], *, batch_size: int | None = None) -> list[list[float]]:
        if not texts:
            return []

        size = batch_size or DEFAULT_BATCH_SIZE
        out: list[list[float]] = []

        for start in range(0, len(texts), size):
            out.extend(self._embed_batch(texts[start : start + size]))

        return out

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        try:
            response = self._client.embeddings.create(
                model=self.model_name,
                input=batch,
                encoding_format="float",
            )
        except Exception as exc:  # noqa: BLE001 —— SDK 的异常层次随版本变，统一在这里归类
            raise self._classify(exc) from exc

        # 按 index 排序而不是按返回顺序 —— 协议保证 data 带 index，
        # 但各家实现是否保序没有保证，显式排序更稳
        items = sorted(response.data, key=lambda d: d.index)
        vectors = [list(item.embedding) for item in items]

        if len(vectors) != len(batch):
            raise EmbeddingError(
                f"返回向量数（{len(vectors)}）与请求文本数（{len(batch)}）不一致",
                retryable=True,
            )

        self._check_dimension(vectors)
        return vectors

    def _classify(self, exc: Exception) -> EmbeddingError:
        """把 SDK 异常翻译成带 retryable 标记的 EmbeddingError。

        不区分的话，上层对所有错误一律重试，鉴权失败会被重试 3 次；
        或者一律不重试，限流时直接丢任务。两种都不对。
        """
        status = getattr(exc, "status_code", None)
        text = str(exc)[:300]

        if status is None:
            # 连不上 / 超时之类，没有 HTTP 状态
            return EmbeddingError(f"embedding 请求失败：{text}", retryable=True)

        retryable = status == 429 or status >= 500
        return EmbeddingError(f"embedding 服务返回 {status}：{text}", retryable=retryable, status=status)

    def _check_dimension(self, vectors: list[list[float]]) -> None:
        """维度不对时给出**能看懂的**错误。

        默认的报错只会说 "dimension mismatch"，不会告诉你期望值和实际值，
        排查要绕一大圈。
        """
        actual = len(vectors[0])
        if actual != self.dimension:
            raise EmbeddingError(
                f"embedding 维度不匹配：配置是 {self.dimension} 维（FASHIONOPS_EMBEDDING_DIM），"
                f"模型 {self.model_name!r} 返回的是 {actual} 维。\n"
                f"改配置，或者换一个维度匹配的模型 —— 注意向量库的 collection "
                f"也要一起重建（维度建索引时就固定了）。",
                retryable=False,
            )

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


# ---------------------------------------------------------------------------
# Mock 实现：**不是随机向量**，而是离线可用的哈希向量化
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]")


def _features(text: str) -> list[str]:
    """把文本拆成特征。

    中文没有空格，按整词切分需要分词器（会增加依赖）。
    这里用**字符 + 字符二元组**（bigram）：这是检索里很经典的轻量做法 ——
    "退货" 和 "退货率" 会共享 ``退`` / ``货`` / ``退货`` 三个特征，
    所以它们的向量会真的接近。对"能不能召回"的验证来说足够。
    """
    lowered = text.lower()
    words = _TOKEN_RE.findall(lowered)

    feats = list(words)
    # 中文二元组：只对中文字符做，英文词的 bigram 意义不大
    han = "".join(c for c in lowered if "\u4e00" <= c <= "\u9fff")
    feats.extend(han[i : i + 2] for i in range(len(han) - 1))
    return feats


class MockEmbeddingClient:
    """离线向量化：**确定性哈希 + 符号散列**。

    ## 为什么不用随机向量

    随机向量也能让"维度对不对、写入读得到吗"这类测试通过，但**检索质量**完全测不了 ——
    搜"退货"召回哪几篇是随机的，于是检索相关的断言只能写得很弱
    （比如"返回了 3 条"），而真正该锁住的"应该召回退货那篇"就测不到。

    这里用**符号散列（signed hashing）**把 n-gram 特征映射到固定维度：
    相同特征落到相同位置，余弦相似度就真的近似于文本的词汇重合度。
    于是离线也能写出有意义的检索断言：

        store.search(vectorize("退货率太高"))  →  应该召回讲退货的文章

    ## 代价

    它只懂字面重合，不懂语义。搜"性价比"召不回只写"价格便宜"的文章 ——
    而这正是**真实 embedding 的价值所在**，也是切换到真模型后能讲出来的差异。
    """

    provider = "mock"

    def __init__(self, *, dimension: int = 1024, model: str = "mock-embedding") -> None:
        self.dimension = dimension
        self.model_name = model

    def embed(self, texts: list[str], *, batch_size: int | None = None) -> list[list[float]]:
        del batch_size
        return [self._vectorize(t) for t in texts]

    def embed_one(self, text: str) -> list[float]:
        return self._vectorize(text)

    def _vectorize(self, text: str) -> list[float]:
        vec = [0.0] * self.dimension

        for feature in _features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            # 符号散列：每个特征随机取 +1/-1。
            # 不这么做的话，所有特征都做加法，任意两段文本的余弦相似度都被推高，
            # 相似度就失去了区分度（这是"hashing trick"里的标准技巧）
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[index] += sign

        # L2 归一化：这样余弦相似度就是内积，越界问题也一并解决
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            # 空文本 / 全是标点。返回一个确定的单位向量而不是零向量 ——
            # 零向量在余弦距离下是未定义的，Qdrant 会直接报错
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
_EMBEDDING_BASE_URLS = {
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
    "local": None,  # 本地部署的 embedding 服务（vLLM / Xinference），base_url 必须显式给
}


def get_embedding_client(settings: Settings | None = None) -> EmbeddingClient:
    """按配置返回向量化客户端。

    默认**复用 chat 的 provider 和 Key** —— 智谱等厂商同一个 Key 同时管 chat 和
    embedding，再让用户配一份 Key 是多余的心智负担。
    需要单独指定时（比如 chat 用智谱、embedding 用本地 BGE）填
    ``FASHIONOPS_EMBEDDING_PROVIDER`` / ``FASHIONOPS_EMBEDDING_API_KEY`` 覆盖。

    **不缓存实例**：``OpenAI`` 客户端持有连接池，缓存会拖住文件句柄，
    而建索引 / 单次检索都是短生命周期，重建的开销可以忽略。
    """
    settings = settings or get_settings()

    provider = (settings.embedding_provider or settings.model_provider or "mock").lower()
    api_key = settings.embedding_api_key or settings.model_api_key
    model = settings.embedding_model
    dimension = settings.embedding_dim

    if provider == "mock" or not api_key:
        if provider != "mock":
            logger.warning(
                "embedding_provider=%s 但没配 api_key，降级为 mock（离线哈希向量）。"
                "检索仍可用，但只有字面匹配能力，语义召回会明显变差。",
                provider,
            )
        return MockEmbeddingClient(dimension=dimension)

    base_url = settings.embedding_base_url or _EMBEDDING_BASE_URLS.get(provider)

    return OpenAICompatEmbeddingClient(
        api_key=api_key,
        model=model,
        dimension=dimension,
        base_url=base_url,
    )


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "EmbeddingClient",
    "EmbeddingError",
    "MockEmbeddingClient",
    "OpenAICompatEmbeddingClient",
    "get_embedding_client",
]
