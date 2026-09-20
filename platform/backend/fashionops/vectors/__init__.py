"""向量库层。

对外只暴露这个包。业务代码从 ``fashionops.vectors`` 导入，
不要直接 import 具体实现模块 —— 那样换后端时改动会散到各处。

```python
from fashionops.vectors import Filter, VectorRecord, get_vector_store

store = get_vector_store()
store.search("kb_articles", vector, limit=5, filter=Filter(must={"category": "退货"}))
```
"""

from fashionops.vectors.base import (
    PAYLOAD_ID,
    PAYLOAD_MODEL,
    PAYLOAD_SOURCE_ID,
    PAYLOAD_TEXT,
    CollectionInfo,
    Filter,
    VectorHit,
    VectorRecord,
    VectorStore,
)
from fashionops.vectors.factory import build_vector_store, get_vector_store, reset_vector_store
from fashionops.vectors.memory_store import MemoryVectorStore
from fashionops.vectors.qdrant_store import QdrantVectorStore

__all__ = [
    "PAYLOAD_ID",
    "PAYLOAD_MODEL",
    "PAYLOAD_SOURCE_ID",
    "PAYLOAD_TEXT",
    "CollectionInfo",
    "Filter",
    "MemoryVectorStore",
    "QdrantVectorStore",
    "VectorHit",
    "VectorRecord",
    "VectorStore",
    "build_vector_store",
    "get_vector_store",
    "reset_vector_store",
]
