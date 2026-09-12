# 数据库迁移（Alembic）

> 这个文件是 UTF-8，可以随便写中文。
> **但 `platform/alembic.ini` 不能** —— 原因见下面「一个必踩的坑」。

## 为什么从 `create_all` 换成 Alembic

阶段 0 表结构还在剧烈变动，那时 `Base.metadata.create_all()` 最省事。
**上线之后不行了**：不能每次改一列就 drop 重建。

现在的规矩：**任何表结构变更都必须有迁移文件**。
`create_all` 只在单测临时库里用（`tests/` 的 fixture），不再用于开发库和线上库。

## 常用命令

两种入口都能用，等价。

### 入口 1：项目自己的 CLI（推荐）

```powershell
cd platform

.venv\Scripts\fashionops-db.exe info              # 连接信息 + 迁移版本 + 各表行数
.venv\Scripts\fashionops-db.exe init              # 升到最新（幂等，可反复跑）
.venv\Scripts\fashionops-db.exe current           # 当前版本
.venv\Scripts\fashionops-db.exe history           # 迁移历史
.venv\Scripts\fashionops-db.exe revision -m "加 users 表"   # 自动对比模型生成迁移
.venv\Scripts\fashionops-db.exe downgrade -1 --yes         # 回退一步（危险）
.venv\Scripts\fashionops-db.exe reset --yes       # 删库 + 按迁移重建（危险）
```

### 入口 2：原生 alembic

```powershell
.venv\Scripts\alembic.exe upgrade head
.venv\Scripts\alembic.exe check            # ← 最有用的一个，见下
.venv\Scripts\alembic.exe current
```

### `alembic check` 是干什么的

它比对**数据库的真实结构**和**模型定义**，有差异就报出来并**返回非 0**。

这是防"有人改了模型忘了写迁移"的利器，应该放进 CI：

```powershell
.venv\Scripts\alembic.exe check   # 期望输出：No new upgrade operations detected.
```

> 我们刚接入时跑过一次，输出正是 `No new upgrade operations detected.` ——
> 说明历史上那些 `create_all` 建出来的表，和现在生成的迁移**逐列一致**，
> 基线是干净的。

## 一个必踩的坑：`alembic.ini` 必须是纯 ASCII

**症状**

```
UnicodeDecodeError: 'gbk' codec can't decode byte 0x82 in position 18: illegal multibyte sequence
```

任何 `alembic` 子命令（连 `--help`）都直接崩。

**根因**

Alembic 读 ini 时写的是 `configparser.read(path, encoding="locale")`，
Windows 中文环境的 locale encoding 是 **GBK**。
ini 里出现一个中文字符就解不开。

**`PYTHONUTF8=1` 救不了** —— `encoding="locale"` 走的是 `locale.getencoding()`，
UTF-8 模式下它仍然返回 GBK。这一点很容易误判，别浪费时间去试。

**我们怎么处理的**

1. `alembic.ini` 全英文（只有指向本文件的注释）
2. `fashionops-db` **不读 ini**，用 `alembic.config.Config()` 程序化构造 ——
   所以即使 ini 被弄坏，也还有一条能跑的迁移通道
3. `tests/test_migrations.py` 里有两条用例锁住这件事：
   - `test_alembic_ini_is_ascii_only` —— 有人加了中文就红
   - `test_ini_reading_actually_breaks_on_non_ascii` —— 用真实数据证明这个坑存在，
     将来 Alembic/Python 改了行为，这条会失败，提醒我们可以撤销这条注意事项

## 迁移文件为什么这么设计

`migrations/env.py` 有三处和默认模板不一样，都是必要的：

| 设置 | 为什么 |
| --- | --- |
| 连接串从 `Settings` 取，不从 ini 读 | 否则会出现「迁移连 A 库、应用连 B 库」，而两边都"成功"了 |
| 显式 `import fashionops.db.models` | metadata 是**导入时**才填充的。不导入模型，`--autogenerate` 会以为"库里一堆表、metadata 里一个都没有"，然后生成一堆 `DROP TABLE` |
| `render_as_batch=True` | SQLite 不支持大部分 `ALTER TABLE`，batch 会退化成"建新表 → 拷数据 → 换名"，让同一份迁移在两个方言上都能跑 |
| `compare_type=True`<br>`compare_server_default=True` | **默认是关的**。改了 `String(32)` → `String(64)` 不会生成任何迁移，静默漏掉 —— 这是 autogenerate 最容易骗人的地方 |

## 工作流

### 改表

```powershell
# 1. 改 src/fashionops/db/models.py
# 2. 生成迁移（对着当前开发库对比）
.venv\Scripts\fashionops-db.exe revision -m "orders 加渠道字段"
# 3. **读一遍生成的脚本**（重点看 downgrade 和有没有意外的 DROP）
# 4. 应用
.venv\Scripts\fashionops-db.exe init
# 5. 确认无残留差异
.venv\Scripts\alembic.exe check
```

### 接管一个"先有表、后有迁移"的库

```powershell
.venv\Scripts\fashionops-db.exe init
```

它会自己判断：如果库里**已有业务表但没有 `alembic_version`**，
就只写版本号（`stamp`），**不执行任何 DDL**；否则正常 `upgrade`。

判断依据是"库里有没有业务表"，不是"有没有版本表"——
后者只说明迁移体系没跑过，前者才说明 schema 已经有了。

我们自己的开发库就是这样接管过来的（15 张表是早期 `create_all` 建的）。

### 上线时的迁移

```powershell
# 上线前：先看要执行什么 SQL，不实际执行
.venv\Scripts\alembic.exe upgrade head --sql > migrate.sql
# 人工 review 那个文件
# 上线时执行
.venv\Scripts\fashionops-db.exe init
```

> ⚠️ MySQL 的 DDL **不支持事务回滚**（Alembic 会打印
> `Will assume non-transactional DDL`）。所以：
> **大表加索引/改列类型之前必须备份**，迁移失败不会自动回滚。

## 写迁移时的注意事项

1. **`downgrade` 必须真的能回**。`tests/test_migrations.py` 会跑一个完整的来回，
   回不去就红。写不出来就别 merge。
2. **别在迁移里 `import` 应用代码**（除了必要的 `sqlalchemy`）。
   迁移是历史快照，依赖会变，半年后它可能 import 不到那个函数了。
   要数据就用 `op.execute("...")` 写裸 SQL。
3. **加非空列要分三步**：先加可空列 → 回填数据 → 再改成非空。
   一步到位在已有数据的表上会失败。
4. **删列/改类型前先备份**。DDL 不会回滚。

## 文件结构

```
platform/
├── alembic.ini                  ← 纯 ASCII！见上文
└── migrations/
    ├── README.md                ← 本文件
    ├── env.py                   ← 迁移执行环境（连接串、metadata、batch 模式）
    ├── script.py.mako           ← 新迁移的模板
    └── versions/
        └── 20260912_0810_0484f1659d5e_initial_schema_15_tables.py
```
