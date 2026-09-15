"""知识库与 RAG。

```python
from fashionops.kb import build_index, search_kb

build_index()                                   # 建索引
search_kb("退货怎么处理", top_k=3)               # 检索
```

这个包负责**与检索质量有关**的一切：切分、向量化、召回、融合、重排。
它不负责"怎么用检索结果回答用户" —— 那是 Agent 的事。
"""

from fashionops.kb.chunking import Chunk, build_chunks, normalize, split_text
from fashionops.kb.indexer import IndexStats, build_index, chunk_id, search_kb

__all__ = [
    "Chunk",
    "IndexStats",
    "build_chunks",
    "build_index",
    "chunk_id",
    "normalize",
    "search_kb",
    "split_text",
]
