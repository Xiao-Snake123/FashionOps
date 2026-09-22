"""词法（稀疏）向量编码 —— 混合检索里的"另一路"召回源。

## 为什么需要它

纯稠密向量检索对**精确词 / 编号 / 罕见术语**召回差：用户搜"KB-0003"、
"SKU 编码规则"、"7 天无理由"里的"7 天"这种字面意图，语义向量未必给高分，
但词项命中几乎必中。这路召回补的就是这个盲区。

## 不引入 SPLADE / 神经网络稀疏模型

原方案写过"用 SPLADE 神经稀疏向量"，但那要 torch + 大模型下载，
在当前 Windows 单机、无 Docker 的环境里不现实。这里改用**本地词法编码**：

* 分词：优先 ``jieba``（质量好）；装不上就退化成"字符 + 二元组"bigram
  （和 ``embeddings.MockEmbeddingClient`` 的 ``_features`` 同一思路，零依赖）。
* 权重：BM25 风格的 ``(1 + ln(tf)) * idf``，idf 在**建索引时**对整个语料算好，
  存进 ``data_dir/kb_sparse_idf.json``，检索时复用 —— 两侧用同一套词项权重，
  Qdrant 里稀疏向量的点积才近似"词项重合度"。

## 和 Qdrant 怎么接

编码结果是 ``SparseVector(indices, values)``：词项经 blake2b 哈希取模得到稳定的
整数下标。Qdrant 原生支持稀疏向量，建索引时把稠密向量（命名 ``dense``）和这一路
（命名 ``bm25``）一起写进去，检索时用 ``FusionQuery(RRF)`` 在库内做融合。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import Iterable

from fashionops.vectors.base import SparseVector

logger = logging.getLogger("fashionops.kb.sparse")

#: 哈希下标的整数空间大小（31 位）。碰撞概率极低，且碰撞只会**轻微**稀释权重，
#: 不会让召回出错，所以不需要维护一张 vocab 映射表。
_INDEX_SPACE = 2**31

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]")


def _bigram_han(text: str) -> list[str]:
    """中文按字符二元组切（英文词不拆）。"""
    han = "".join(c for c in text.lower() if "\u4e00" <= c <= "\u9fff")
    return [han[i : i + 2] for i in range(len(han) - 1)]


def tokenize(text: str) -> list[str]:
    """把文本拆成词项。

    优先 jieba；导入失败（没装）就退化成"正则英文/数字词 + 中文二元组"。
    两者都是确定性的，建索引和检索两侧结果一致，融合才有意义。
    """
    try:
        import jieba  # type: ignore

        return [w for w in jieba.lcut((text or "").lower()) if w.strip()]
    except Exception:  # noqa: BLE001 —— jieba 是可选依赖，缺失就走兜底
        lowered = (text or "").lower()
        words = _TOKEN_RE.findall(lowered)
        return words + _bigram_han(lowered)


def _term_index(term: str) -> int:
    digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest[:4], "big") % _INDEX_SPACE


def build_idf(texts: Iterable[str]) -> dict[str, float]:
    """对整个语料算 IDF。

    ``idf = ln((N + 1) / (df + 1)) + 1``（平滑版，避免 0）。
    只在建索引时跑一次，结果落盘，检索时直接读。
    """
    df: Counter[str] = Counter()
    n = 0
    for text in texts:
        n += 1
        for term in set(tokenize(text)):
            df[term] += 1
    return {term: math.log((n + 1) / (c + 1)) + 1.0 for term, c in df.items()}


def encode(text: str, idf: dict[str, float] | None = None) -> SparseVector:
    """把一段文本编成稀疏向量。

    ``idf`` 为 ``None`` 时退化为纯 TF（检索侧 idf 文件丢失时的兜底，
    仍可用，只是词频加权没考虑稀有度）。
    """
    tf = Counter(tokenize(text))
    if not tf:
        return SparseVector.empty()

    indices: list[int] = []
    values: list[float] = []
    for term, count in tf.items():
        weight = (1.0 + math.log(count)) * (idf.get(term, 1.0) if idf else 1.0)
        if weight <= 0.0:
            continue
        indices.append(_term_index(term))
        values.append(weight)
    return SparseVector(indices=tuple(indices), values=tuple(values))


def _idf_path(data_dir) -> Path:
    return Path(data_dir) / "kb_sparse_idf.json"


def save_idf(data_dir, idf: dict[str, float]) -> None:
    """把 IDF 落盘，供检索侧复用。"""
    path = _idf_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(idf, ensure_ascii=False), encoding="utf-8")
    logger.info("已写出稀疏检索 IDF（%d 个词项）→ %s", len(idf), path)


def load_idf(data_dir) -> dict[str, float]:
    """读回 IDF。文件不存在返回空 dict（encode 会退化为纯 TF）。"""
    path = _idf_path(data_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取稀疏检索 IDF 失败（将退化为纯 TF）：%s", exc)
        return {}
