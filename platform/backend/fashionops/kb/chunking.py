"""文本切分（chunking）。

## 为什么切分质量直接决定检索质量

这是 RAG 里最容易被低估的一环。常见错误是"按固定字符数切"：

* 切在句子中间 → "退货需在收货后" / "7 天内申请" 两个 chunk 各自都不完整，
  检索引擎不管命中哪一个，喂给模型的信息都是残缺的
* chunk 太大（比如整篇 2000 字）→ 向量把主题"平均化"了，
  一篇讲"尺码 + 退货 + 物流"的文章，向量和"怎么退货"的相似度被稀释，
  反而不如一篇只讲退货的短文章
* chunk 太小（比如按句切）→ 一句话的向量语义信息太少，
  而且同一篇文章会产生大量近乎重复的 chunk，挤占 top-k

所以这里做的是**结构化优先 + 尺寸兜底**：

1. 先按**段落**切（空行分隔）—— 段落是作者已经划好的语义边界，免费的信息
2. 再把小段落**贪心合并**到目标长度附近，避免碎片
3. 单个超长段落才按句子边界切，并加**重叠**（overlap）——
   重叠是为了不让关键信息正好落在切点上而两边都读不到

## 参数怎么定的

``target_chars=400``：中文一个字约等于 1-2 个 token，
400 字大约 400-600 token。检索时取 top-3 拼起来约 1500 token，
留给模型回答的空间还够，成本也可控。

``overlap_chars=60``：约 15% 的重叠。太小（<10%）盖不住切点处的信息丢失，
太大（>30%）会让相邻 chunk 高度重复，检索结果里出现三份几乎一样的内容。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 单个 chunk 的目标字符数
DEFAULT_TARGET_CHARS = 400
#: 相邻 chunk 的重叠字符数
DEFAULT_OVERLAP_CHARS = 60
#: chunk 的硬上限。超过这个长度即便语义完整也应该切开 ——
#: 否则一个异常长的段落会变成一个超大 chunk，把向量语义稀释掉
MAX_CHUNK_CHARS = 900
#: 短于这个长度的 chunk 不单独成块，会被并入前一块。
#: 标题行、"见上"这类碎片检索起来毫无价值，只会占位
MIN_CHUNK_CHARS = 30

#: 段落分隔：一个或多个空行
_PARAGRAPH_RE = re.compile(r"\n\s*\n+")
#: 句子边界。中文句号/问号/叹号/分号 + 英文句号问号感叹号。
#: 只看后面跟空白或结尾的情况，避免把 "3.5 折" 的句点当句子结束
_SENTENCE_RE = re.compile(r"(?<=[。！？；.!?])\s*")


@dataclass(frozen=True)
class Chunk:
    """一个切分出来的片段。"""

    #: 在原文中的序号，从 0 开始。用来做 chunk id，也用来定位"命中的是哪一段"
    index: int
    text: str
    #: 原文总长度。用于统计"切分率"，异常时能发现
    source_chars: int

    @property
    def chars(self) -> int:
        return len(self.text)


def normalize(text: str) -> str:
    """统一空白与常见全角字符。

    不做的后果：同一篇文章的两种排版会产生不同的向量，
    而"看起来一模一样"的两段文本检索得分却不同 —— 排查起来非常费解。
    """
    # Windows 换行统一成 \n，否则段落分隔的正则匹配不上
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 全角空格（中文排版里很常见）统一成半角
    text = text.replace("\u3000", " ")
    # 行内多余空白压成一个空格，但保留换行
    text = re.sub(r"[ \t]+", " ", text)
    # 三个以上连续换行压成两个（段落边界）
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_text(
    text: str,
    *,
    target_chars: int = DEFAULT_TARGET_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[str]:
    """把一段文本切成若干 chunk。"""
    text = normalize(text)
    if not text:
        return []

    # 已经很短，直接成块，别为了"流程统一"硬切
    #
    # 注意这里比的是 ``target_chars`` 而不是 ``MAX_CHUNK_CHARS``。
    # 曾经用的是后者，后果是：一篇 700 字、讲了尺码+退货+物流三件事的文章
    # 因为没超过 900 而被整篇返回成一个 chunk，向量被三个主题"平均化"，
    # 三个方向都搜不准 —— 而这正是本模块开头说要避免的情况。
    # ``target_chars`` 才是目标粒度，``MAX_CHUNK_CHARS`` 只是不可逾越的硬上限。
    if len(text) <= target_chars:
        return [text]

    paragraphs = [p.strip() for p in _PARAGRAPH_RE.split(text) if p.strip()]

    pieces: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= MAX_CHUNK_CHARS:
            pieces.append(paragraph)
        else:
            # 超长段落：按句子切，再走一遍合并逻辑
            pieces.extend(_split_long_paragraph(paragraph))

    return _merge(pieces, target_chars=target_chars, overlap_chars=overlap_chars)


def _split_long_paragraph(paragraph: str, *, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """按句子边界切超长段落。

    如果连句子切完还超限（比如整段没有标点），才退化成按字符硬切 ——
    这种情况在真实语料里主要是用户粘贴的日志、长串商品编号。
    """
    sentences = [s for s in _SENTENCE_RE.split(paragraph) if s.strip()]
    if len(sentences) <= 1:
        # 没有句子边界可用，只能按字符切。硬切处不加重叠 ——
        # 那是"关键信息被切在中间"的补偿手段，而按字符切本就没有句子级语义，
        # 补 60 个字只是白白多存一份重复内容。
        # （注意：这些块之后还会经过 ``_merge``，那里的重叠是块与块之间的，
        #  解决的是另一个问题 —— 相邻 chunk 的主题衔接。）
        return [paragraph[i : i + limit] for i in range(0, len(paragraph), limit)]

    out: list[str] = []
    buffer = ""
    for sentence in sentences:
        if len(buffer) + len(sentence) > limit and buffer:
            out.append(buffer)
            buffer = sentence
        else:
            buffer += sentence
    if buffer:
        out.append(buffer)
    return out


def _merge(pieces: list[str], *, target_chars: int, overlap_chars: int) -> list[str]:
    """把小片段贪心合并到目标长度，并给相邻块加重叠。

    两个细节：

    * **碎片就地并入前一块**，而不是单独成块。单独成块会让 top-k 被
      标题行、"详见上文"这类无信息内容占满。
    * **重叠是从前一块的尾部取的**，不是复制整块。取尾部是因为切点后面的
      内容最可能与下一块的开头构成完整语义。
    """
    chunks: list[str] = []
    buffer = ""

    for piece in pieces:
        candidate = f"{buffer}\n\n{piece}" if buffer else piece

        if len(candidate) <= target_chars:
            buffer = candidate
            continue

        if buffer:
            chunks.append(buffer)

        # 新块以「上一块的尾部」开头做起手式，形成重叠。
        # 只在上一块够长时才加 —— 很短的一块全量重复没有意义
        if overlap_chars > 0 and buffer and len(buffer) > overlap_chars * 2:
            buffer = buffer[-overlap_chars:] + "\n\n" + piece
        else:
            buffer = piece

        # 重叠之后又超限（piece 本身很长），先吐出去
        while len(buffer) > MAX_CHUNK_CHARS:
            chunks.append(buffer[:MAX_CHUNK_CHARS])
            buffer = buffer[MAX_CHUNK_CHARS - overlap_chars :]

    if buffer:
        chunks.append(buffer)

    # 最后收口：太短的尾块并入前一块，避免产生"半句话"chunk
    if len(chunks) >= 2 and len(chunks[-1]) < MIN_CHUNK_CHARS:
        tail = chunks.pop()
        chunks[-1] = f"{chunks[-1]}\n\n{tail}"

    return [c.strip() for c in chunks if c.strip()]


def build_chunks(text: str, **kwargs: int) -> list[Chunk]:
    """切分并带上序号信息。索引层用这个。"""
    normalized = normalize(text)
    total = len(normalized)
    return [Chunk(index=i, text=piece, source_chars=total) for i, piece in enumerate(split_text(normalized, **kwargs))]


__all__ = [
    "DEFAULT_OVERLAP_CHARS",
    "DEFAULT_TARGET_CHARS",
    "MAX_CHUNK_CHARS",
    "MIN_CHUNK_CHARS",
    "Chunk",
    "build_chunks",
    "normalize",
    "split_text",
]
