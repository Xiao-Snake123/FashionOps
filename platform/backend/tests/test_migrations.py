"""迁移体系本身的测试。

为什么值得给"迁移工具"写测试：迁移是**唯一一个出错就会毁数据**的东西，
而它平时不跑，只在关键时刻跑。`--autogenerate` 生成的脚本看着都对，
但常见问题有：

* ``downgrade`` 漏写 → 出事回不去
* 索引/约束名重复 → 在另一个库上跑直接报错
* 用了某个方言特有的类型 → 单测（SQLite）过、生产（MySQL）挂

所以这里不断言"能跑起来"，而是断言**一个完整的来回**：
upgrade → 表齐全 → downgrade → 表清空 → 再 upgrade → 还能起来。

另一组用例覆盖 ``fashionops-db init`` 的两种入口，尤其是
**"库里已有表但没有 alembic_version"** 这条 —— 真实项目的库往往
先于迁移体系存在，处理错就是全量建表冲突。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text

from fashionops.config import get_settings
from fashionops.db import base as db_base
from fashionops.db.cli import ALEMBIC_VERSION_TABLE, _alembic_config, cmd_init
from fashionops.db.models import ALL_MODELS

EXPECTED_TABLES = sorted(m.__tablename__ for m in ALL_MODELS)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """把 Settings 指到一个临时库，并清掉单例引擎/配置缓存。

    不这么做的话测试会连到**开发库**，然后 downgrade base 把真数据删光 ——
    迁移测试是少数几个"跑错地方就造成真实损失"的测试。
    """
    db_file = tmp_path / "mig.db"
    monkeypatch.setenv("FASHIONOPS_DATABASE_URL", f"sqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("FASHIONOPS_DATA_DIR", str(tmp_path))

    get_settings.cache_clear()
    db_base.reset_engine()
    yield tmp_path
    db_base.reset_engine()
    get_settings.cache_clear()


def _tables() -> list[str]:
    engine = db_base.get_engine(get_settings())
    return sorted(t for t in inspect(engine).get_table_names() if t != ALEMBIC_VERSION_TABLE)


def _version() -> str | None:
    engine = db_base.get_engine(get_settings())
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE}")).scalar_one_or_none()


class TestMigrationRoundTrip:
    def test_upgrade_creates_every_model_table(self, temp_db):
        """upgrade 出来的表必须**逐张**对得上模型定义。

        对不上的话，说明有人加了模型却忘了生成迁移 ——
        本地因为跑的是 create_all 看不出来，生产上就是"表不存在"。
        """
        command.upgrade(_alembic_config(), "head")
        assert _tables() == EXPECTED_TABLES, (
            '迁移与模型不一致。加/改模型后必须跑：\n  .venv\\Scripts\\alembic.exe revision --autogenerate -m "描述"'
        )

    def test_downgrade_is_complete(self, temp_db):
        """downgrade 必须把表删干净，否则回滚等于没回。"""
        command.upgrade(_alembic_config(), "head")
        assert len(_tables()) == len(EXPECTED_TABLES)

        command.downgrade(_alembic_config(), "base")
        assert _tables() == [], "downgrade 没删干净 —— 迁移不可逆"

    def test_can_reupgrade_after_downgrade(self, temp_db):
        """反复升降要能收敛。曾经踩过：downgrade 留下半截索引，再 upgrade 就撞名。"""
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
        assert _tables() == EXPECTED_TABLES

    def test_upgrade_is_idempotent(self, temp_db):
        """已经是 head 再 upgrade 应该是 no-op，不能重复执行 DDL。"""
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        before = _version()

        command.upgrade(cfg, "head")  # 再跑一次
        assert _version() == before
        assert _tables() == EXPECTED_TABLES


class TestAdoptExistingDatabase:
    """``fashionops-db init`` 要能接管"先有库、后有迁移"的情况。"""

    def test_init_on_empty_database(self, temp_db):
        assert cmd_init(_Args()) == 0
        assert _tables() == EXPECTED_TABLES
        assert _version() is not None

    def test_init_adopts_database_without_version_table(self, temp_db):
        """库里已有 15 张表但没有 alembic_version → 只 stamp，不跑 DDL。

        这是真实项目最常见的起点：表是早期 ``create_all`` 建的。
        如果这里直接 upgrade，会因为 "table already exists" 整条失败。
        """
        engine = db_base.get_engine(get_settings())
        db_base.Base.metadata.create_all(engine)
        # 前置条件：表都在，但没有任何迁移记录
        assert _tables() == EXPECTED_TABLES
        assert ALEMBIC_VERSION_TABLE not in inspect(engine).get_table_names()

        assert cmd_init(_Args()) == 0

        assert _version() is not None, "没有标记版本，后续 upgrade 会从头再跑一遍"
        assert _tables() == EXPECTED_TABLES, "接管过程不该动表"

    def test_init_is_idempotent_on_adopted_database(self, temp_db):
        engine = db_base.get_engine(get_settings())
        db_base.Base.metadata.create_all(engine)

        assert cmd_init(_Args()) == 0
        first = _version()
        assert cmd_init(_Args()) == 0
        assert _version() == first
        assert _tables() == EXPECTED_TABLES

    def test_drop_also_removes_version_table(self, temp_db):
        """``drop`` 必须连 ``alembic_version`` 一起删。

        不删的话下次 ``init`` 会认为"迁移已经跑过了"从而跳过建表，
        结果是一个空库 + 一个"已是最新"的版本号 —— 最难查的那种状态。
        """
        from fashionops.db.cli import cmd_drop

        cmd_init(_Args())
        assert cmd_drop(_Args(yes=True)) == 0

        engine = db_base.get_engine(get_settings())
        names = inspect(engine).get_table_names()
        assert names == [], f"drop 后有残留表：{names}"


class _Args:
    """``fashionops-db`` 子命令用到的参数集合。不用 argparse 是为了让测试直接调函数。"""

    def __init__(self, **kw) -> None:
        self.yes = kw.get("yes", False)
        self.revision = kw.get("revision", "head")
        self.message = kw.get("message", "test")
        self.no_autogenerate = kw.get("no_autogenerate", True)


def test_alembic_ini_is_ascii_only():
    """``alembic.ini`` 必须是纯 ASCII。

    Alembic 用 ``encoding="locale"`` 读它 —— Windows 中文环境是 GBK，
    ini 里一个中文字符就会让 **CLI 完全不可用**：

        UnicodeDecodeError: 'gbk' codec can't decode byte 0x82 ...

    而且 ``PYTHONUTF8=1`` 救不了：configparser 走的是 ``locale.getencoding()``。

    这条用例是给未来的自己/同事看的：改 ini 时别顺手加中文注释。
    """
    # 测试现在在 backend/tests 下，平台根是 parents[2]（alembic.ini 留在仓库根）。
    # migrations/versions 在 backend/migrations，用 parents[1] 即可。
    ini = Path(__file__).resolve().parents[2] / "alembic.ini"
    raw = ini.read_bytes()
    try:
        raw.decode("ascii")
    except UnicodeDecodeError as exc:
        bad = raw[max(0, exc.start - 24) : exc.end + 24]
        pytest.fail(
            f"alembic.ini 含非 ASCII 字节（偏移 {exc.start}）： {bad!r}\n"
            "Windows 上 Alembic CLI 会直接崩。把注释改成英文，"
            "详细说明写到 migrations/README.md。"
        )


def test_cli_does_not_depend_on_ini_encoding():
    """``fashionops-db`` 走程序化 Config，不应读 ini。

    这样即使 ini 被弄坏，迁移也还有一条能走的路 ——
    相当于给运维留了一个"救命通道"。
    """
    cfg = _alembic_config()
    assert Path(cfg.get_main_option("script_location")).name == "migrations"
    # 没读 ini 的话 config_file_name 是 None
    assert cfg.config_file_name in (None, ""), (
        "如果这里不是 None，说明有人在配置里指了 ini —— 那 Windows 上的 GBK 编码问题就又回来了"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="GBK 编码问题只存在于中文 Windows")
def test_ini_reading_actually_breaks_on_non_ascii():
    """用真实数据证明"非 ASCII 会让 Alembic CLI 崩"，而不是只靠注释里的一句话。

    做法：造一个有中文的 ini，用 Alembic 读它，断言抛 UnicodeDecodeError。
    这样如果哪天 Python/Alembic 改了行为（比如默认 UTF-8 了），
    这条用例会失败，提醒我们"那条注意事项可以撤销了"。
    """
    import tempfile

    from alembic.config import Config as AlembicConfig

    with tempfile.TemporaryDirectory() as tmp:
        bad_ini = Path(tmp) / "bad.ini"
        # 写一个最小可用的 ini，但带一行中文
        bad_ini.write_bytes("[alembic]\nscript_location = migrations\n# 中文注释会让 GBK 解码失败\n".encode())
        cfg = AlembicConfig(str(bad_ini))
        with pytest.raises(UnicodeDecodeError):
            _ = cfg.file_config  # 惰性解析，读到这里才炸


# ---------------------------------------------------------------------------
# 迁移文件的静态检查
#
# 下面几条**故意不连数据库**。它们抓的是一类"SQLite 上完全正常、MySQL 上必炸"
# 的写法，用静态分析就能拦住 —— 比在 CI 里起一个 MySQL 便宜得多，
# 而且本地开发时就能立刻发现。
# ---------------------------------------------------------------------------
def _migration_files() -> list[Path]:
    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    files = sorted(versions.glob("*.py"))
    assert files, "一个迁移文件都没有，是不是路径写错了？"
    return files


#: 从 ``batch_alter_table('X')`` 里抽出表名
_BATCH_TABLE_RE = re.compile(r"batch_alter_table\(\s*['\"]([A-Za-z0-9_]+)['\"]")
#: 从 ``op.drop_table('X')`` 里抽出表名
_DROP_TABLE_RE = re.compile(r"op\.drop_table\(\s*['\"]([A-Za-z0-9_]+)['\"]")


def _find_redundant_index_drops(src: str) -> list[tuple[int, str]]:
    """找出「删表前多余的 drop_index」的行号与内容。

    判定：一个 ``drop_index`` 之后 10 行内出现 ``drop_table``，
    **并且两者说的是同一张表**，才算多余 —— 因为 DROP TABLE 会把
    该表上的索引和外键一起删掉。

    ## 为什么必须比对表名

    只看"后面 10 行里有没有 drop_table"会**误报**：一个 downgrade 里完全
    可能"删掉 A 表上的一个索引"+"删掉 B 表"，这两件事毫无关系，前者是必要的。

    真踩过的例子：一次迁移里加了 ``task_todos`` 表，**同时**给
    ``chat_approvals`` 加了两个索引和两列。downgrade 里既要删那两个索引
    （合法，表还在），又要删 ``task_todos``（合法，DROP TABLE 带走自己的索引）——
    被只管"距离"的旧版本判成 2 处违规。

    **检查器误报比没有检查器更糟**：一旦有人开始看到告警就"改代码绕过它"，
    它就再也拦不住真问题了。

    表名从**外层缩进块**取，不从索引名前缀猜 —— 索引名不一定以表名开头
    （本项目里 ``chat_approvals`` 表上的索引叫 ``ix_chat_approval_task``）。
    """
    lines = src.splitlines()
    offenders: list[tuple[int, str]] = []
    current_table: str | None = None
    for i, line in enumerate(lines):
        batch = _BATCH_TABLE_RE.search(line)
        if batch:
            current_table = batch.group(1)
        if "batch_op.drop_index(" not in line:
            continue
        window = "\n".join(lines[i : i + 10])
        # 只有"删的正是那张马上要被删的表"的索引，才是多余的
        if current_table and current_table in _DROP_TABLE_RE.findall(window):
            offenders.append((i + 1, line.strip()))
    return offenders


def test_the_offender_detector_actually_detects():
    """给检查器喂已知有问题的代码，确认它真的能报出来。

    没有这条的话，上面那个 parametrize 用例**可能因为正则/窗口写错而永远绿**——
    看起来有覆盖，实际什么都没查。
    """
    bad = (
        "def downgrade() -> None:\n"
        "    with op.batch_alter_table('orders', schema=None) as batch_op:\n"
        "        batch_op.drop_index('ix_orders_sku')\n"
        "\n"
        "    op.drop_table('orders')\n"
    )
    found = _find_redundant_index_drops(bad)
    assert found, "检查器没能识别出已知有问题的写法"
    assert found[0][0] == 3  # 行号也要对得上

    # 反例 1：只删索引、不删表 —— 这是合法用法，不该被拦
    legit = (
        "def downgrade() -> None:\n"
        "    with op.batch_alter_table('orders', schema=None) as batch_op:\n"
        "        batch_op.drop_index('ix_orders_sku')\n"
        "        batch_op.drop_column('sku_id')\n"
    )
    assert not _find_redundant_index_drops(legit), "误报了：只删索引不删表是合法的"

    # 反例 2：drop_index 和 drop_table 离得很远（>10 行），不该被当成一对
    far = "batch_op.drop_index('ix_x')\n" + "\n" * 12 + "op.drop_table('x')\n"
    assert not _find_redundant_index_drops(far), "误报了：距离过远的调用不该被关联"

    # 反例 3：**删 A 表的索引 + 删 B 表**，两者无关，不该被拦。
    # 这条是补上"只看距离"那个版本的误报 —— 真实迁移里就这么误报过。
    cross = (
        "def downgrade() -> None:\n"
        "    with op.batch_alter_table('chat_approvals', schema=None) as batch_op:\n"
        "        batch_op.drop_index('ix_chat_approval_task')\n"
        "        batch_op.drop_column('task_no')\n"
        "\n"
        "    op.drop_table('task_todos')\n"
    )
    assert not _find_redundant_index_drops(cross), "误报了：删 A 表的索引和删 B 表是两件无关的事"


@pytest.mark.parametrize("path", _migration_files(), ids=lambda p: p.name)
def test_no_drop_index_before_drop_table(path: Path):
    """删表之前**不能**先单独 drop_index。

    这是踩过的真坑。Alembic 的 autogenerate 会把 downgrade 生成为
    「先 drop_index、再 drop_table」，在 SQLite 上毫无问题（batch 模式
    重建整表），但在 MySQL 上直接报错：

        (1553, "Cannot drop index 'ix_orders_sku':
               needed in a foreign key constraint")

    原因：MySQL 的外键会**复用已有的索引**。``orders.sku_id`` 的外键发现
    ``ix_orders_sku`` 正好能用就挂上去了，于是这个索引被外键"占用"，
    单独 DROP 会被拒绝。``DROP TABLE`` 则会把索引和外键一起带走。

    这是"两个方言行为不一致"的典型：**只有 MySQL 能暴露**，
    而我们的单测跑 SQLite。所以用静态检查兜住。
    """
    offenders = _find_redundant_index_drops(path.read_text(encoding="utf-8"))

    assert not offenders, (
        f"{path.name} 里有 {len(offenders)} 处「删表前的多余 drop_index」，"
        f"这在 MySQL 上会报 errno 1553。删掉这些行即可（DROP TABLE 会连索引一起删）。\n"
        + "\n".join(f"  第 {ln} 行: {txt}" for ln, txt in offenders)
    )


@pytest.mark.parametrize("path", _migration_files(), ids=lambda p: p.name)
def test_downgrade_is_not_a_stub(path: Path):
    """每个迁移都必须有真的 downgrade。

    Alembic 生成的空模板里 downgrade 是 ``pass``。留着它的话，
    出事时想回滚只会得到一个"命令成功但什么都没变"的假象 ——
    比明确报错更糟。
    """
    src = path.read_text(encoding="utf-8")
    assert "def downgrade() -> None:" in src, f"{path.name} 没有 downgrade"

    body = src.split("def downgrade() -> None:", 1)[1]
    # 去掉注释和空行，看有没有实质语句
    statements = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert statements, f"{path.name} 的 downgrade 是空的"
    assert statements[0] != "pass", f"{path.name} 的 downgrade 是个 pass 存根，回滚等于没回"


@pytest.mark.parametrize("path", _migration_files(), ids=lambda p: p.name)
def test_revision_chain_is_linear(path: Path):
    """检查 revision / down_revision 的链条完整，没有分叉或断链。

    分叉（两个迁移的 down_revision 是同一个）在多人协作时很容易出现，
    表现是 ``upgrade head`` 报 "Multiple head revisions" ——
    一报就卡住所有人的上线，而且通常发生在最忙的时候。
    """
    src = path.read_text(encoding="utf-8")
    assert "revision: str = " in src, f"{path.name} 缺 revision"
    assert "down_revision" in src, f"{path.name} 缺 down_revision"


def test_all_migration_files_are_chained():
    """把各文件的 revision / down_revision 拼起来，验证是一条单链。"""
    import re

    chain: dict[str | None, str] = {}  # down_revision -> revision
    for path in _migration_files():
        src = path.read_text(encoding="utf-8")
        rev = re.search(r"^revision: str = ['\"]([^'\"]+)['\"]", src, re.M)
        down = re.search(r"^down_revision: str \| None = (['\"]([^'\"]+)['\"]|None)", src, re.M)
        assert rev, f"{path.name} 解析不出 revision"
        assert down, f"{path.name} 解析不出 down_revision"
        down_value = down.group(2)  # None 时 group(2) 为 None
        assert down_value not in chain, (
            f"迁移链分叉了：{chain[down_value]} 和 {rev} 的 down_revision 都是 {down_value!r}"
        )
        chain[down_value] = rev.group(1)

    # 从 None（起点）开始应该能一路走到唯一的一个 head
    assert None in chain, "没有找到根迁移（down_revision = None）"
    heads = set(chain.values()) - set(chain.keys())
    assert len(heads) == 1, f"应该只有一个 head，实际有 {len(heads)} 个：{heads}"
