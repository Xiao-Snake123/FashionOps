"""知识库 / RAG 测试。

分四层，从下往上：

1. **切分（纯函数）** —— 不碰数据库、不碰网络。这一层最该写厚，
   因为切分质量直接决定检索质量，而它又最容易写错（切在句子中间、
   产生半句话 chunk、相邻块完全重复……）
2. **离线向量化** —— 验证 ``MockEmbeddingClient`` 是**真的**按字面重合
   给分。如果它是随机向量，下面第 3 层的检索断言就全是假的
3. **索引与检索端到端** —— 临时库 + 内存向量库 + 离线向量化，
   全程不碰 MySQL / Qdrant / 智谱
4. **工具层契约** —— 重点锁住 ``reason`` 的取值。
   "索引没建" 和 "没找到答案" 是**完全不同**的两件事，
   混在一起会让客服把运维事故当成业务结论告诉用户

## 为什么这一层必须离线

索引和检索会打三种外部依赖：MySQL、embedding API、向量库。
任何一个不可用都会让测试变成"看运气"。这里全换成可注入的实现，
所以这个文件**永远能跑**，也永远在几百毫秒内跑完。
"""
from __future__ import annotations

import math

import pytest
from sqlalchemy.orm import Session

from fashionops.db.base import Base, build_engine, reset_engine
from fashionops.db.models import KBArticle
from fashionops.kb import build_index, chunk_id, search_kb, split_text
from fashionops.kb.chunking import (
    MAX_CHUNK_CHARS,
    build_chunks,
    normalize,
)
from fashionops.kb.indexer import article_chunks
from fashionops.kb.sparse import encode
from fashionops.llm.embeddings import MockEmbeddingClient
from fashionops.vectors import Filter, MemoryVectorStore, SparseVector, VectorRecord

# ===========================================================================
# 测试数据
# ===========================================================================
#: 短文章直接成块，用来验证"不要为了流程统一硬切"
_SHORT = "退货需在收货后 7 天内申请。"

#: 长文档：6 个段落 × 约 280 字 ≈ 1700 字，远超 MAX_CHUNK_CHARS，
#: 一定会走多块路径。**这一条是专门补的**：
#: 种子数据里每篇 retrieval_text 都短，多块路径从来没被执行过，
#: 而"检索结果永远是同一篇的前半段"这种 bug 恰好只在多块时暴露。
_LONG_PARAGRAPHS = [
    # 每段写够 250 字以上，否则总长可能不到上限 —— 测试数据自己太短
    # 会让"多块切分"这条路径根本跑不到（这是写这个测试时踩到的第一个坑）。
    "退货申请流程说明。第一步，登录账号进入订单详情页，找到需要退货的订单，"
    "点击申请退货按钮。第二步，选择退货原因，原因分为质量问题、尺码不合适、"
    "颜色与描述不符、不想要了四类，选择不同的原因会影响运费承担方和审核时长。"
    "第三步，上传能证明问题的照片，照片要求清晰可见商品本体和标签，"
    "模糊的照片会被驳回，建议在自然光下拍摄并带上订单号纸条一起入镜。"
    "第四步，提交后商家会在 48 小时内审核，审核结果以短信和站内信双通道通知。",
    "退货时效规定。自签收之日起 7 天内可以申请无理由退货，超过 7 天但不满 15 天的"
    "只能以质量问题为由申请。质量问题造成的退货不受 7 天限制，但需要提供"
    "有效的质量检测凭证或者清晰的问题照片。预售商品的时效从实际发货日开始计算，"
    "而不是从下单日开始，这一点经常被误读为「下单后 7 天」而导致超期。"
    "跨境订单的时效从清关完成日开始计算，通常比境内订单多 3 个工作日。",
    "退款到账时间说明。退货商品验收合格后，退款会在 1 到 3 个工作日内原路返回。"
    "使用花呗或信用卡支付的订单，退款到账时间还要加上银行侧的处理时间，"
    "通常再多 3 到 7 个工作日，这笔时间不在我们可控范围内。使用优惠券的订单，"
    "优惠券是否退回取决于券的类型：满减券按实际支付比例退回，折扣券不退回，"
    "运费券不退回。使用积分抵扣的部分会原额退回积分，积分的有效期不变。",
    "运费承担规则。因为质量问题、发错货、少发漏发造成的退货，往返运费都由商家承担"
    "并且不需要买家先垫付。因为个人原因比如尺码不合适、颜色不喜欢造成的退货，"
    "运费由买家承担，使用顺丰寄回的按实际产生费用结算。如果订单本身包邮，"
    "退货时只扣除买家寄回的运费，不额外扣除当初的发货运费。"
    "参加满减活动的订单退款后，若剩余金额不再满足活动门槛，优惠金额会被追回。",
    "不可退货的商品范围。贴身衣物、内衣裤、已拆封的化妆品、定制类商品、"
    "以及明确标注清仓不退的商品，不支持无理由退货。但如果是质量问题，"
    "这些商品依然可以退换，商家不得以商品性质为由拒绝质量问题的退换要求。"
    "食品类商品开封后不支持退货，但保质期内出现变质、异味、霉斑的除外。"
    "活体植物和鲜花类商品因物流损坏的，需要在签收后 24 小时内提供开箱视频。",
    "换货流程。换货和退货的申请入口相同，在售后类型里选择换货而不是退货即可。"
    "换货需要商品有货，无货时会自动转为退货并全额退款，不会因为缺货而一直挂着。"
    "换货产生的物流信息会在申请通过后以短信形式发送到下单手机号上。"
    "同一订单最多支持换货两次，超过两次只能走退货重拍流程。"
    "换货后的商品重新计算 7 天无理由时效，从新商品签收日算起。",
]

_ARTICLE_SPECS = [
    # (article_no, title, category, retrieval_text, tags, is_active)
    (
        "KB-0001",
        "退换货政策说明",
        "退换货",
        "退货 退款 换货 政策 时效 七天无理由 申请流程 签收之日起 7 天内可以申请退货",
        ["退货", "退款"],
        True,
    ),
    (
        "KB-0002",
        "物流配送说明",
        "物流",
        "物流 快递 发货 配送 到货时间 默认发顺丰 偏远地区需要 3 到 5 天",
        ["物流"],
        True,
    ),
    (
        "KB-0003",
        "尺码选择指南",
        "尺码",
        "尺码 尺寸 测量 版型 偏大偏小 建议按平时穿的尺码选 身材数据对照表",
        ["尺码"],
        True,
    ),
    (
        "KB-0004",
        "已下架的历史政策",
        "退换货",
        "退货 旧的退货政策 已作废 不要引用",
        ["退货"],
        False,  # 下架文章不该被索引
    ),
]

#: 内存向量库里的 collection 名。故意和真 Qdrant 的配置名不同，
#: 万一哪天有测试漏了注入，会直接报"collection 不存在"而不是悄悄连上真库
COLLECTION = "pytest_kb"


# ===========================================================================
# fixture
# ===========================================================================
@pytest.fixture
def embedder() -> MockEmbeddingClient:
    """离线向量化。

    维度取 256 而不是 1024：哈希散列在 256 维下碰撞概率已经足够低，
    而向量越小测试越快。用 1024 会让这个文件从几百毫秒变成几秒。
    """
    return MockEmbeddingClient(dimension=256, model="mock-embedding")


@pytest.fixture
def store() -> MemoryVectorStore:
    return MemoryVectorStore()


@pytest.fixture
def kb_env(tmp_path, monkeypatch):
    """临时 SQLite 库 + 灌好知识库文章。

    只 patch 一个东西：``fashionops.db.base.get_settings``。
    索引和检索的 settings / store / embedder 都是**显式传参**进去的，
    不依赖全局配置 —— 这样测试不会因为开发者本地 ``.env`` 里
    配了真 Qdrant / 真智谱 Key 而改变行为。
    """
    from fashionops.config import Settings
    from fashionops.db import base as db_base

    settings = Settings(
        data_dir=tmp_path,
        db_host=None,  # 落到 SQLite 文件
        model_provider="mock",
        embedding_provider="mock",
        embedding_dim=256,
    )
    reset_engine()
    monkeypatch.setattr(db_base, "get_settings", lambda: settings)

    engine = build_engine(settings)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        for article_no, title, category, retrieval_text, tags, is_active in _ARTICLE_SPECS:
            session.add(
                KBArticle(
                    article_no=article_no,
                    title=title,
                    category=category,
                    # content 故意写得和 retrieval_text 不同：
                    # 用来验证索引优先用 retrieval_text 而不是正文
                    content=f"{title}的正文内容，这里是很长的正式表述。",
                    retrieval_text=retrieval_text,
                    tags=tags,
                    is_active=is_active,
                )
            )
        session.commit()

    yield settings

    Base.metadata.drop_all(engine)
    engine.dispose()
    reset_engine()


@pytest.fixture
def indexed(kb_env, store, embedder):
    """建好索引的环境。返回 (store, embedder, settings)。"""
    build_index(store=store, embedder=embedder, settings=kb_env, collection=COLLECTION)
    return store, embedder, kb_env


# ===========================================================================
# 1. 切分
# ===========================================================================
class TestNormalize:
    def test_crlf_becomes_lf(self):
        """Windows 换行必须统一。

        不统一的后果很隐蔽：段落正则是按 ``\\n\\s*\\n`` 写的，
        带 ``\\r`` 的文本切不出段落，于是整篇变成一个大块，
        向量被平均化 —— 而"看起来一模一样"的两段文本检索得分不同，
        排查起来极其费解。
        """
        assert normalize("第一段\r\n\r\n第二段") == "第一段\n\n第二段"

    def test_fullwidth_space_becomes_ascii(self):
        """全角空格在中文排版里很常见，不归一化会和半角空格产生不同向量。"""
        assert normalize("尺码\u3000\u3000对照") == "尺码 对照"

    def test_runs_of_blank_lines_collapse(self):
        assert normalize("A\n\n\n\n\nB") == "A\n\nB"

    def test_inline_whitespace_collapses_but_newlines_kept(self):
        assert normalize("A   \t B\nC") == "A B\nC"

    def test_surrounding_whitespace_stripped(self):
        assert normalize("\n\n  A  \n\n") == "A"

    def test_empty_is_empty(self):
        assert normalize("") == ""
        assert normalize("   \n  ") == ""


class TestSplitText:
    def test_empty_input_yields_nothing(self):
        assert split_text("") == []
        assert split_text("   ") == []

    def test_short_text_is_one_chunk(self):
        """短文本别为了"流程统一"硬切 —— 切了只会产生更差的向量。"""
        assert split_text(_SHORT) == [_SHORT]

    def test_text_at_hard_limit_is_not_split(self):
        """正好等于上限时**不**切。边界差一导致的切分行为变化最难排查。"""
        text = "甲" * MAX_CHUNK_CHARS
        assert len(split_text(text)) == 1

    def test_long_document_produces_multiple_chunks(self):
        doc = "\n\n".join(_LONG_PARAGRAPHS)
        chunks = split_text(doc)
        assert len(chunks) >= 3, f"1500 字的长文档只切出 {len(chunks)} 块，切分逻辑有问题"

    def test_every_chunk_within_hard_limit(self):
        """硬上限是**必须**遵守的：超长 chunk 会把向量语义稀释掉。"""
        doc = "\n\n".join(_LONG_PARAGRAPHS)
        for chunk in split_text(doc):
            assert len(chunk) <= MAX_CHUNK_CHARS, f"chunk 长度 {len(chunk)} 超过上限"

    def test_adjacent_chunks_overlap(self):
        """相邻块要有重叠。

        重叠是为了不让关键信息正好落在切点上 —— "退货需在收货后" 和
        "7 天内申请" 被切开之后，两边各自都不完整，命中哪一块喂给模型都是残缺的。
        """
        doc = "\n\n".join(_LONG_PARAGRAPHS)
        chunks = split_text(doc)
        assert len(chunks) >= 2
        # 取前一块尾部一小段，它应该出现在后一块的开头附近
        tail = chunks[0][-40:]
        assert tail in chunks[1], "相邻块之间没有重叠，切点处的信息会丢失"

    def test_no_content_lost(self):
        """切分不能吞字。抽样校验几个关键词都还在。"""
        doc = "\n\n".join(_LONG_PARAGRAPHS)
        joined = "".join(split_text(doc))
        for keyword in ("退货申请流程", "退款到账时间", "换货流程", "不可退货的商品范围"):
            assert keyword in joined, f"切分后 {keyword!r} 丢了"

    def test_paragraph_boundary_preferred_over_char_count(self):
        """优先按段落切：段落是作者已经划好的语义边界，免费的信息。"""
        paragraph = "甲" * 400
        doc = "\n\n".join([paragraph, paragraph, paragraph])
        chunks = split_text(doc)
        assert len(chunks) >= 2
        # 每块都不该是 400 字硬切的产物（那样会切在段中间）
        assert all(len(c) >= 300 for c in chunks)

    def test_long_paragraph_split_by_sentence(self):
        """超长段落按句子切，不该出现"半句话"。"""
        sentence = "这是一句完整的话，用于验证句子边界切分是否正确。"
        paragraph = sentence * 40  # 约 900+ 字，超过上限
        chunks = split_text(paragraph)
        assert len(chunks) >= 2
        # 每块（除最后一块）都该以句号结尾
        for chunk in chunks[:-1]:
            assert chunk.rstrip().endswith("。"), f"按句切分产生了半句话：{chunk[-20:]!r}"

    def test_paragraph_without_punctuation_falls_back_to_hard_split(self):
        """整段没有标点时退化成按字符硬切。

        真实语料里这种情况主要是用户粘贴的日志、长串商品编号 ——
        没有语义边界可用，硬切是唯一选择，但要保证不丢内容、不超限。

        这里显式关掉重叠才能断言"一字不差"：重叠是**故意**制造的内容重复，
        带重叠时 join 出来的结果必然比原文长，那不是丢内容。
        """
        blob = "A" * (MAX_CHUNK_CHARS * 2 + 100)
        chunks = split_text(blob, overlap_chars=0)
        assert len(chunks) >= 2
        assert all(len(c) <= MAX_CHUNK_CHARS for c in chunks)
        assert "".join(chunks) == blob

    def test_hard_split_with_overlap_duplicates_but_loses_nothing(self):
        """默认（带重叠）时结果会比原文长，但**不能少字**。

        重叠是刻意付出的存储代价，用来换"切点处的信息不丢"。
        单独锁一下这个性质：将来有人把重叠逻辑改成"去掉重复"时，
        得知道这里本来是有意重复的。
        """
        blob = "A" * (MAX_CHUNK_CHARS * 2 + 100)
        joined = "".join(split_text(blob))
        # 重叠前缀是用 "\n\n" 拼到下一块开头的，所以会多出换行符
        assert set(joined) <= {"A", "\n"}
        assert len(joined.replace("\n", "")) >= len(blob)
        assert len(joined) > len(blob), "重叠没生效，切点处的信息可能会丢"

    def test_target_chars_is_respected(self):
        """显式给 target_chars 时按时生效（检索调参就靠这个）。"""
        doc = "\n\n".join(_LONG_PARAGRAPHS)
        small = split_text(doc, target_chars=200)
        large = split_text(doc, target_chars=800)
        assert len(small) > len(large)

    def test_overlap_can_be_disabled(self):
        """overlap_chars=0 时不该有重叠。

        调试切分问题时需要能关掉重叠 —— 否则"为什么同一句话出现两次"
        会浪费很多时间去查别的地方。
        """
        doc = "\n\n".join(_LONG_PARAGRAPHS)
        chunks = split_text(doc, overlap_chars=0)
        assert len(chunks) >= 2
        assert chunks[0][-40:] not in chunks[1]


class TestBuildChunks:
    def test_indexes_start_at_zero_and_increment(self):
        doc = "\n\n".join(_LONG_PARAGRAPHS)
        chunks = build_chunks(doc)
        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_source_chars_is_normalized_length(self):
        """``source_chars`` 记的是归一化之后的长度，用来算"切分率"。

        拿它和块长之和比，能发现重叠带来的膨胀是否合理。
        """
        text = "一段文字\r\n\r\n另一段文字"
        chunks = build_chunks(text)
        assert all(c.source_chars == len(normalize(text)) for c in chunks)

    def test_chars_property(self):
        chunks = build_chunks(_SHORT)
        assert chunks[0].chars == len(chunks[0].text)

    def test_empty_text_gives_no_chunks(self):
        assert build_chunks("") == []


# ===========================================================================
# 2. 离线向量化 —— 它是第 3 层检索断言成立的前提
# ===========================================================================
class TestMockEmbedding:
    def test_dimension_matches_config(self, embedder):
        assert embedder.dimension == 256
        assert len(embedder.embed_one("退货")) == 256

    def test_is_deterministic(self, embedder):
        """同样的文本必须给同样的向量。

        不定的话，重跑索引会把同一篇文章写成不同向量，
        而"重建前后检索结果应该一致"这类断言就没法写了。
        """
        assert embedder.embed_one("退货政策") == embedder.embed_one("退货政策")

    def test_is_l2_normalized(self, embedder):
        """归一化后余弦相似度等于内积，省一次除法，也避免数值溢出。"""
        for text in ("退货", "尺码怎么选" * 20, ""):
            norm = math.sqrt(sum(v * v for v in embedder.embed_one(text)))
            assert abs(norm - 1.0) < 1e-9, f"{text!r} 的向量不是单位向量"

    def test_empty_text_gives_valid_unit_vector_not_zero(self, embedder):
        """空文本必须给**有效单位向量**而不是零向量。

        零向量在余弦距离下未定义，Qdrant 会直接报错 ——
        一个空的检索 query 就能把接口打成 500。
        """
        vector = embedder.embed_one("")
        assert any(v != 0 for v in vector)
        assert abs(math.sqrt(sum(v * v for v in vector)) - 1.0) < 1e-9

    def test_related_text_scores_higher_than_unrelated(self, embedder):
        """**这个文件里最重要的一条断言**。

        它证明 mock 向量化是真的按字面重合给分的，所以下面
        "搜退货应该召回退货那篇" 才有意义。
        如果是随机向量，那条断言只能退化成"返回了 3 条" —— 等于没测。
        """
        query = embedder.embed_one("退货怎么处理")
        related = embedder.embed_one("退货 退款 换货 政策 时效")
        unrelated = embedder.embed_one("物流 快递 发货 配送 到货时间")

        def cosine(a, b):
            return sum(x * y for x, y in zip(a, b, strict=True))

        assert cosine(query, related) > cosine(query, unrelated)

    def test_batch_and_single_agree(self, embedder):
        """批量接口和单条接口必须给一样的结果。

        批量走的是服务端的 batch 端点，两条代码路径分开实现时
        很容易出现"索引用一种、查询用另一种"的不一致。
        """
        texts = ["退货", "物流", "尺码"]
        assert embedder.embed(texts) == [embedder.embed_one(t) for t in texts]

    def test_batch_size_is_accepted_and_ignored(self, embedder):
        """``batch_size`` 参数要接受（接口契约），离线实现不用管它。"""
        assert embedder.embed(["a", "b"], batch_size=1) == embedder.embed(["a", "b"])


# ===========================================================================
# 3. 索引与检索端到端（临时库 + 内存向量库 + 离线向量化）
# ===========================================================================
class TestChunkId:
    def test_deterministic_and_readable(self):
        """id 里带上 article_no，排查时看一眼就知道来自哪篇。

        更重要的是**确定性**：重跑索引必须覆盖自己而不是追加。
        追加的后果是同一段内容在 top-k 里出现三次。
        """
        assert chunk_id("KB-0001", 3) == "KB-0001#3"
        assert chunk_id("KB-0001", 3) == chunk_id("KB-0001", 3)
        assert chunk_id("KB-0001", 3) != chunk_id("KB-0001", 4)


class TestArticleChunks:
    def test_prefers_retrieval_text(self):
        """有 ``retrieval_text`` 时只用它。

        正文里有大量寒暄废话（"亲，感谢您的支持"），
        会把向量往"客服用语"的方向带偏，反而降低区分度。
        """
        article = KBArticle(
            article_no="X",
            title="标题",
            category="退换货",
            content="正文正文正文",
            retrieval_text="关键词 关键词",
        )
        pieces = article_chunks(article)
        assert len(pieces) == 1
        assert "正文" not in pieces[0]
        assert "关键词" in pieces[0]

    def test_falls_back_to_title_plus_content(self):
        """没有 ``retrieval_text`` 时退回"标题 + 正文"，且**标题必须带上**。

        只索引正文的话，搜"退货政策"可能召不回标题叫「退换货政策说明」
        但正文里没出现这几个字的文章。
        """
        article = KBArticle(
            article_no="X",
            title="退换货政策说明",
            category="退换货",
            content="自签收之日起 7 天内可以申请。",
            retrieval_text=None,
        )
        pieces = article_chunks(article)
        assert len(pieces) == 1
        assert "退换货政策说明" in pieces[0]
        assert "7 天" in pieces[0]

    def test_blank_retrieval_text_also_falls_back(self):
        """``retrieval_text`` 是空白字符串（不是 None）时也要退回。"""
        article = KBArticle(
            article_no="X",
            title="物流说明",
            category="物流",
            content="默认发顺丰。",
            retrieval_text="   ",
        )
        assert "顺丰" in article_chunks(article)[0]

    def test_multi_chunk_article_gets_multiple_pieces(self):
        """长文章要真的切成多块。

        这条是补覆盖率的：种子数据里每篇 retrieval_text 都短，
        多块路径从来没跑过 —— 而"检索永远只召回第一段"这种 bug
        恰好只在多块时暴露。
        """
        article = KBArticle(
            article_no="X",
            title="长文档",
            category="退换货",
            content="",
            retrieval_text="\n\n".join(_LONG_PARAGRAPHS),
        )
        assert len(article_chunks(article)) >= 3


class TestBuildIndex:
    def test_writes_all_active_articles(self, kb_env, store, embedder):
        stats = build_index(
            store=store, embedder=embedder, settings=kb_env, collection=COLLECTION
        )
        # 4 篇里 1 篇下架，只索引 3 篇
        assert stats.articles == 3
        assert stats.chunks == 3  # 每篇 retrieval_text 都很短，各成一块
        assert store.count(COLLECTION) == 3

    def test_skips_inactive_articles(self, kb_env, store, embedder):
        """下架文章不该被召回。

        ``is_active=False`` 的 KB-0004 里也写了"退货"，
        如果被索引，搜退货时它可能挤掉有效的 KB-0001 ——
        用户会拿到一个已经作废的政策。
        """
        build_index(store=store, embedder=embedder, settings=kb_env, collection=COLLECTION)
        hits = store.search(
            COLLECTION,
            embedder.embed_one("退货"),
            limit=10,
        )
        assert all(hit.source_id != "KB-0004" for hit in hits)

    def test_is_idempotent(self, kb_env, store, embedder):
        """重复建索引不该产生重复数据。

        这是"改了切分参数重跑一次"的日常操作。不幂等的后果是
        top-k 里全是同一篇的多个副本，检索质量直接崩掉而报错却一个没有。
        """
        first = build_index(
            store=store, embedder=embedder, settings=kb_env, collection=COLLECTION
        )
        count_after_first = store.count(COLLECTION)

        second = build_index(
            store=store, embedder=embedder, settings=kb_env, collection=COLLECTION
        )
        assert second.chunks == first.chunks
        assert store.count(COLLECTION) == count_after_first
        assert second.before_count == count_after_first

    def test_rebuild_clears_first(self, kb_env, store, embedder):
        """``rebuild=True`` 时先清空。

        改了切分参数就该重建：新旧切分同时存在，检索结果里会出现
        内容重叠但 id 不同的两批数据。
        """
        build_index(store=store, embedder=embedder, settings=kb_env, collection=COLLECTION)
        assert store.count(COLLECTION) == 3

        stats = build_index(
            store=store,
            embedder=embedder,
            settings=kb_env,
            collection=COLLECTION,
            rebuild=True,
        )
        assert stats.before_count == 3
        assert store.count(COLLECTION) == 3  # 还是 3，不是 6

    def test_raises_when_embedding_model_changed(self, kb_env, store, embedder):
        """换 embedding 模型后**必须**报错，不能默默继续。

        两个模型的向量空间完全不兼容，混在一起检索会给出
        "看起来正常但实际无意义"的相似度 —— 比直接报错危险得多，
        因为没有任何信号提示你答案是错的。
        """
        build_index(store=store, embedder=embedder, settings=kb_env, collection=COLLECTION)

        other = MockEmbeddingClient(dimension=256, model="another-model")
        with pytest.raises(RuntimeError) as excinfo:
            build_index(store=store, embedder=other, settings=kb_env, collection=COLLECTION)

        message = str(excinfo.value)
        assert "another-model" in message
        assert "mock-embedding" in message
        assert "--rebuild" in message, "报错必须告诉用户怎么修"

    def test_rebuild_allows_model_change(self, kb_env, store, embedder):
        """显式 ``rebuild=True`` 时允许换模型 —— 清空之后就没有混用了。"""
        build_index(store=store, embedder=embedder, settings=kb_env, collection=COLLECTION)

        other = MockEmbeddingClient(dimension=256, model="another-model")
        stats = build_index(
            store=store,
            embedder=other,
            settings=kb_env,
            collection=COLLECTION,
            rebuild=True,
        )
        assert stats.embedding_model == "another-model"
        assert stats.warnings, "换模型重建应该留下一条告警，方便事后复盘"
        assert store.count(COLLECTION) == 3

    def test_stats_summary_mentions_everything_important(self, kb_env, store, embedder):
        stats = build_index(
            store=store, embedder=embedder, settings=kb_env, collection=COLLECTION
        )
        summary = stats.summary()
        for token in (COLLECTION, "3", "mock-embedding", "256"):
            assert token in summary

    def test_stats_records_dimension_and_model(self, kb_env, store, embedder):
        stats = build_index(
            store=store, embedder=embedder, settings=kb_env, collection=COLLECTION
        )
        assert stats.dimension == 256
        assert stats.embedding_model == "mock-embedding"

    def test_payload_carries_source_and_model(self, kb_env, store, embedder):
        """payload 要带够溯源信息。

        出了错误回复之后要能回答"召回的是哪一篇、用哪个模型的向量" ——
        这是 RAG 归因的第一步，缺了就只能靠翻日志猜。
        """
        build_index(store=store, embedder=embedder, settings=kb_env, collection=COLLECTION)
        hits = store.search(COLLECTION, embedder.embed_one("退货"), limit=1)
        payload = hits[0].payload
        assert payload["source_id"] == "KB-0001"
        assert payload["embedding_model"] == "mock-embedding"
        assert payload["category"] == "退换货"
        assert payload["is_active"] is True
        assert payload["chunk_index"] == 0


class TestSearchKB:
    def test_finds_the_right_article(self, indexed):
        """搜"退货怎么处理"应该召回讲退货的 KB-0001。

        这一条能成立，靠的是离线向量化真的按字面重合给分（见第 2 层）。
        """
        store, embedder, settings = indexed
        hits = search_kb(
            "退货怎么处理", store=store, embedder=embedder, settings=settings,
            collection=COLLECTION,
        )
        assert hits, "检索没有命中任何内容"
        assert hits[0]["article_no"] == "KB-0001"
        assert hits[0]["category"] == "退换货"

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("发什么快递几天能到", "KB-0002"),
            ("尺码怎么选", "KB-0003"),
            ("退款要多久到账", "KB-0001"),
        ],
    )
    def test_routes_to_correct_category(self, indexed, query, expected):
        store, embedder, settings = indexed
        hits = search_kb(
            query, store=store, embedder=embedder, settings=settings,
            collection=COLLECTION, top_k=1,
        )
        assert hits[0]["article_no"] == expected

    def test_result_shape_is_prompt_ready(self, indexed):
        """返回值必须**只**包含给模型看需要的字段。

        把 VectorHit 整个序列化进去会浪费 token，还会让模型困惑于
        payload / score 这些内部结构。
        """
        store, embedder, settings = indexed
        hit = search_kb(
            "退货", store=store, embedder=embedder, settings=settings, collection=COLLECTION
        )[0]
        assert set(hit) == {"article_no", "title", "category", "content", "score"}
        assert isinstance(hit["score"], float)

    def test_score_is_descending(self, indexed):
        store, embedder, settings = indexed
        hits = search_kb(
            "退货 物流 尺码", store=store, embedder=embedder, settings=settings,
            collection=COLLECTION, top_k=3,
        )
        scores = [h["score"] for h in hits]
        assert scores == sorted(scores, reverse=True)

    def test_top_k_is_honored(self, indexed):
        store, embedder, settings = indexed
        hits = search_kb(
            "退货", store=store, embedder=embedder, settings=settings,
            collection=COLLECTION, top_k=2,
        )
        assert len(hits) <= 2

    def test_empty_query_returns_empty(self, indexed):
        """空 query 直接返回空，不要去打一次 embedding API 浪费钱。"""
        store, embedder, settings = indexed
        assert search_kb(
            "   ", store=store, embedder=embedder, settings=settings, collection=COLLECTION
        ) == []

    def test_category_filter_excludes_others(self, indexed):
        store, embedder, settings = indexed
        hits = search_kb(
            "退货", store=store, embedder=embedder, settings=settings,
            collection=COLLECTION, category="物流", top_k=10,
        )
        assert all(h["category"] == "物流" for h in hits)

    def test_category_filter_can_yield_nothing(self, indexed):
        """过滤到空是**正常业务结果**，不是错误。

        调用方要靠"返回空列表"和"抛 RuntimeError"区分
        "没搜到" 和 "索引没建"。
        """
        store, embedder, settings = indexed
        hits = search_kb(
            "退货", store=store, embedder=embedder, settings=settings,
            collection=COLLECTION, category="不存在的类目",
        )
        assert hits == []

    def test_raises_when_index_missing(self, kb_env, store, embedder):
        """**索引没建必须抛错，不能返回空列表**。

        返回空列表的话，客服 Agent 会以为"知识库里没有相关内容"，
        于是把"运维忘了建索引"这件事当成业务事实告诉用户。
        静默变空比报错危险得多。
        """
        with pytest.raises(RuntimeError) as excinfo:
            search_kb(
                "退货", store=store, embedder=embedder, settings=kb_env, collection=COLLECTION
            )
        assert "fashionops-kb index" in str(excinfo.value), "报错要给出修复命令"

    def test_raises_when_collection_exists_but_empty(self, kb_env, store, embedder):
        """collection 存在但一个点都没有，同样算"索引没建"。"""
        store.ensure_collection(COLLECTION, dimension=256)
        with pytest.raises(RuntimeError, match="不存在或为空"):
            search_kb(
                "退货", store=store, embedder=embedder, settings=kb_env, collection=COLLECTION
            )

    def test_filters_out_inactive_records_at_query_time(self, indexed):
        """查询侧再防一次下架文章。

        索引侧已经只写 ``is_active=True`` 了，为什么还要在查询侧过滤？
        因为"改数据"和"重建索引"之间有窗口期 —— 运营把文章下架了，
        但向量还在库里，下一次建索引前它仍会被召回。
        两侧都防，这个窗口才真正关上。
        """
        store, embedder, settings = indexed
        query = "退货怎么处理"

        # 手工塞一条"已下架"的记录，向量和 query 完全一致 ——
        # 不过滤的话它必然是第一名
        store.upsert(
            COLLECTION,
            [
                VectorRecord(
                    id="KB-9999#0",
                    vector=embedder.embed_one(query),
                    payload={
                        "text": "已作废的退货政策",
                        "source_id": "KB-9999",
                        "title": "已作废",
                        "category": "退换货",
                        "is_active": False,
                        "embedding_model": "mock-embedding",
                    },
                )
            ],
        )

        hits = search_kb(
            query, store=store, embedder=embedder, settings=settings,
            collection=COLLECTION, top_k=10,
        )
        assert all(h["article_no"] != "KB-9999" for h in hits), "下架文章被召回了"
        assert hits[0]["article_no"] == "KB-0001"


class TestHybridRetrieval:
    """Phase 3：把稠密 + 稀疏收进向量库的混合检索。

    全部离线（内存向量库 + 离线向量化），验证：
    1. 默认开启混合检索时 ``search_kb`` 走 ``search_hybrid``（返回形状不变、分数降序）
    2. 关闭 ``retrieval_hybrid`` 能正确退回纯稠密检索（与改造前一致）
    3. 直接调 ``MemoryVectorStore.search_hybrid`` 时，稀疏这一路确实参与了融合
    """

    def test_hybrid_is_default_and_works(self, indexed):
        store, embedder, settings = indexed
        hits = search_kb(
            "尺码怎么选", store=store, embedder=embedder, settings=settings,
            collection=COLLECTION, top_k=3,
        )
        assert hits, "混合检索没有命中"
        # 形状必须和改之前完全一致，工具层才不会崩
        assert set(hits[0]) == {"article_no", "title", "category", "content", "score"}
        scores = [h["score"] for h in hits]
        assert scores == sorted(scores, reverse=True)

    def test_falls_back_to_dense_when_hybrid_disabled(self, kb_env, store, embedder):
        # 关掉混合检索后，应当退回纯稠密向量检索，行为与改造前一致
        kb_env.retrieval_hybrid = False
        build_index(store=store, embedder=embedder, settings=kb_env, collection=COLLECTION)
        hits = search_kb(
            "退款要多久到账", store=store, embedder=embedder, settings=kb_env,
            collection=COLLECTION, top_k=1,
        )
        assert hits[0]["article_no"] == "KB-0001"

    def test_search_hybrid_fuses_sparse_signal(self, indexed):
        """稀疏这一路要真的参与融合，而不只是稠密结果的装饰。

        用一组只出现在 KB-0003 retrieval_text 里的词做查询，验证
        ``search_hybrid`` 能把 KB-0003 的 chunk 召回到前排。
        """
        store, embedder, settings = indexed
        q = "尺码 尺寸 测量"
        sparse = {"bm25": encode(q)}  # idf=None → 纯 TF，足以验证融合链路
        hits = store.search_hybrid(
            COLLECTION,
            embedder.embed_one(q),
            sparse,
            filter=Filter(must={"is_active": True}),
            limit=5,
        )
        ids = [h.id for h in hits]
        assert any(i.startswith("KB-0003#") for i in ids), "稀疏路没把 KB-0003 召回到前排"
