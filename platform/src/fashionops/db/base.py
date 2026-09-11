"""数据层基础设施：声明基类、引擎、会话、时间与金额约定。

约定（全项目统一，不要各写各的）：

* **时间**：数据库里一律存 **naive UTC**。MySQL 的 ``DATETIME`` 不带时区，
  存本地时间会在跨时区/夏令时场景出错；统一存 UTC，在 API 层再转成
  ``Asia/Shanghai`` 给前端。
* **金额**：一律 ``Numeric(14, 2)``，映射到 Python ``Decimal``。
  绝不用 float —— 电商对账差一分钱就是事故。
* **比例**：一律 ``Numeric(8, 6)`` 存 0~1 的小数（如退货率 0.0832），
  展示时再乘 100。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from fashionops.config import Settings, get_settings

# 说明：金额/比例的列类型统一定义在 models.py 的 _MONEY / _RATIO，
# 那里同时要 import Numeric，放一处免得两边各写一遍精度。


def utcnow() -> datetime:
    """当前 UTC 时间（naive）。

    直接用 ``datetime.now(UTC)`` 会带 tzinfo，写进 MySQL ``DATETIME`` 时
    pymysql 会把它当本地时间静默丢弃时区信息 —— 这是最隐蔽的一类时间 bug。
    这里显式去掉 tzinfo，保证"存进去的就是 UTC"。
    """
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。

    额外提供一个 ``__tablename_prefix__`` 之类的钩子暂时不需要，
    保持基类干净；表名由各模型自己显式声明。
    """

    def to_dict(self, exclude: tuple[str, ...] = ()) -> dict:
        """转成可 JSON 序列化的 dict。

        只用于调试和审计快照，正式 API 响应走 Pydantic schema。
        """
        result: dict = {}
        for column in self.__table__.columns:
            if column.name in exclude:
                continue
            value = getattr(self, column.name)
            if isinstance(value, Decimal):
                # Decimal 不能直接被 json.dumps，转 float 会丢精度但对展示够用
                value = float(value)
            elif isinstance(value, datetime):
                value = value.isoformat()
            result[column.name] = value
        return result

    def __repr__(self) -> str:
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


# ---------------------------------------------------------------------------
# 引擎 / 会话
# ---------------------------------------------------------------------------
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def build_engine(settings: Settings | None = None) -> Engine:
    """按配置建引擎。

    ``pool_pre_ping=True`` 很关键：MySQL 默认 ``wait_timeout`` 是 8 小时，
    隔夜之后池子里的连接已经被服务端单方面关掉了，客户端却还以为可用，
    于是每天早上的第一批请求必然报 "MySQL server has gone away"。
    开了 pre_ping 会在借出连接时先探一下，代价是一次极轻量的 ping。
    """
    settings = settings or get_settings()
    common: dict = {
        "echo": settings.db_echo,
        "pool_pre_ping": True,
        "future": True,
    }

    if settings.is_sqlite:
        # FastAPI 的线程池里多个线程会共用一个 SQLite 连接，
        # 不关掉这个检查会直接抛 ProgrammingError。
        common["connect_args"] = {"check_same_thread": False}
    else:
        common.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_pool_size * 2,
            pool_recycle=settings.db_pool_recycle,
        )

    engine = create_engine(settings.sqlalchemy_url, **common)

    if settings.is_sqlite:
        _enable_sqlite_foreign_keys(engine)

    return engine


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    """SQLite 默认不校验外键！必须每个连接显式打开。

    否则本地跑单测时删父表不报错、生产 MySQL 上却报错 —— 典型的
    "测试全绿，上线就炸"。这里统一打开，让两边行为一致。
    """

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_engine(settings: Settings | None = None) -> Engine:
    """进程内单例引擎。"""
    global _engine
    if _engine is None:
        _engine = build_engine(settings)
    return _engine


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    """进程内单例 Session 工厂。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(settings),
            expire_on_commit=False,  # 提交后还要读对象属性，不 expire 少一次查询
            autoflush=False,
        )
    return _session_factory


def reset_engine() -> None:
    """丢掉单例，让下次重新按当前配置建。

    测试里改完环境变量后必须调用，否则会一直复用旧引擎指向的库。
    """
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务边界。

    用法::

        with session_scope() as s:
            s.add(obj)          # 出块自动 commit
                                # 抛异常自动 rollback

    脚本和后台任务用这个；FastAPI 路由用依赖注入版本。
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个 Session。

    只做 ``close``，不在这里 commit —— 让路由自己决定事务边界，
    避免"读接口顺带提交了半截写操作"。
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
