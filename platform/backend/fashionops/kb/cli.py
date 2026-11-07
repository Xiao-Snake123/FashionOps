"""知识库命令行。

用法::

    fashionops-kb index                 # 增量建索引（已存在的覆盖）
    fashionops-kb index --rebuild       # 先清空再建（改了切分参数后必须用这个）
    fashionops-kb search "退货怎么申请"  # 交互式检索，用来调参数
    fashionops-kb stats                 # 看 collection 现状
    fashionops-kb drop --yes            # 删掉整个 collection
    fashionops-kb chunk "一段长文本"      # 看切分效果（调切分参数时用）

``search`` 和 ``stats`` 是给"调检索效果"用的：
RAG 的效果好坏没法靠感觉判断，必须能一条条看召回结果。
"""

from __future__ import annotations

import sys
from argparse import ArgumentParser, Namespace

from fashionops.config import get_settings
from fashionops.logging_setup import setup_logging

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _store_and_embedder():
    from fashionops.llm.embeddings import get_embedding_client
    from fashionops.vectors import build_vector_store

    settings = get_settings()
    return settings, build_vector_store(settings), get_embedding_client(settings)


def cmd_index(args: Namespace) -> int:
    from fashionops.kb import build_index

    settings, store, embedder = _store_and_embedder()
    print(f"向量库  : {store.backend}")
    print(f"维度    : {embedder.dimension}（{embedder.model_name}）")
    print()

    stats = build_index(store=store, embedder=embedder, settings=settings, rebuild=args.rebuild)
    print(stats.summary())
    print()
    print("完成。")
    return 0


def cmd_search(args: Namespace) -> int:
    from fashionops.kb import search_kb

    settings, store, embedder = _store_and_embedder()
    hits = search_kb(
        args.query,
        store=store,
        embedder=embedder,
        settings=settings,
        top_k=args.top_k,
        category=args.category,
    )

    print("=" * 74)
    print(f"  查询: {args.query}")
    if args.category:
        print(f"  类目过滤: {args.category}")
    print("=" * 74)

    if not hits:
        print("\n  没有命中任何内容。\n")
        return 1

    for rank, hit in enumerate(hits, 1):
        print(f"\n  [{rank}] score={hit['score']:.4f}  {hit['article_no']}  [{hit['category']}]")
        print(f"      {hit['title']}")
        print(f"      {hit['content']}")
    print()
    return 0


def cmd_stats(_args: Namespace) -> int:
    settings, store, _embedder = _store_and_embedder()
    collection = settings.qdrant_collection_kb
    info = store.info(collection)

    print("=" * 74)
    print("  知识库索引状态")
    print("=" * 74)
    print(f"  后端        : {store.backend}")
    print(f"  collection  : {collection}")
    print(f"  是否存在    : {info.exists}")
    if info.exists:
        print(f"  向量数      : {info.vector_count}")
        print(f"  维度        : {info.dimension}")
        print(f"  距离度量    : {info.distance}")
        model = info.embedding_model or "（取不到）"
        print(f"  向量模型    : {model}")

        current = settings.embedding_model
        if info.embedding_model and info.embedding_model != current:
            print(
                f"\n  ⚠ 库里的向量是 {info.embedding_model!r} 生成的，"
                f"当前配置是 {current!r} —— 两者向量空间不兼容，"
                f"检索结果没有意义。重建：fashionops-kb index --rebuild"
            )

    ok, detail = store.healthy()
    print(f"\n  探活        : {'✓' if ok else '✗'} {detail}")
    print()
    return 0 if info.exists else 1


def cmd_drop(args: Namespace) -> int:
    settings, store, _embedder = _store_and_embedder()
    collection = settings.qdrant_collection_kb

    if not args.yes:
        print(f"这会删掉 collection {collection!r} 里的全部向量。确认请加 --yes")
        return 2

    removed = store.drop_collection(collection)
    if removed:
        print(f"已删除 collection {collection!r}")
    else:
        print(f"collection {collection!r} 本来就不存在")
    return 0


def cmd_chunk(args: Namespace) -> int:
    """打印切分结果。调 ``target_chars`` / ``overlap_chars`` 时靠它看效果。"""
    from fashionops.kb.chunking import (
        DEFAULT_OVERLAP_CHARS,
        DEFAULT_TARGET_CHARS,
        build_chunks,
    )

    chunks = build_chunks(
        args.text,
        target_chars=args.target_chars or DEFAULT_TARGET_CHARS,
        overlap_chars=args.overlap_chars if args.overlap_chars is not None else DEFAULT_OVERLAP_CHARS,
    )

    print("=" * 74)
    print(f"  原文 {len(args.text)} 字 → {len(chunks)} 个 chunk")
    print("=" * 74)
    for chunk in chunks:
        print(f"\n  --- chunk {chunk.index}（{chunk.chars} 字）---")
        print(f"  {chunk.text}")
    print()
    return 0


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="fashionops-kb", description="知识库索引与检索")
    sub = parser.add_subparsers(dest="command")

    p_index = sub.add_parser("index", help="建索引（幂等，可反复跑）")
    p_index.add_argument("--rebuild", action="store_true", help="先清空 collection 再建")

    p_search = sub.add_parser("search", help="检索，用来检查召回效果")
    p_search.add_argument("query")
    p_search.add_argument("--top-k", type=int, default=3)
    p_search.add_argument("--category", default=None)

    sub.add_parser("stats", help="查看索引状态")

    p_drop = sub.add_parser("drop", help="删除整个 collection（危险）")
    p_drop.add_argument("--yes", action="store_true")

    p_chunk = sub.add_parser("chunk", help="预览切分效果")
    p_chunk.add_argument("text")
    p_chunk.add_argument("--target-chars", type=int, default=None)
    p_chunk.add_argument("--overlap-chars", type=int, default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "index": cmd_index,
        "search": cmd_search,
        "stats": cmd_stats,
        "drop": cmd_drop,
        "chunk": cmd_chunk,
    }
    if args.command is None:
        parser.print_help()
        return 0
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
