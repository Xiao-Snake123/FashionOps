-- ============================================================================
-- 服装电商多 Agent 运营中台 —— 建库
-- ============================================================================
-- 由 deploy/scripts/init-mysql.ps1 自动执行；也可以手动跑：
--   mysql -u root -p < 00-init-database.sql
--
-- 字符集必须是 utf8mb4：商品标题、客服会话里会有 emoji 和生僻字，
-- utf8（3 字节）存不下，会在写入时直接报错或截断。
-- ============================================================================

CREATE DATABASE IF NOT EXISTS `fashionops`
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_0900_ai_ci;

-- 建表时用于存储过程/触发器的字符集（本项目暂未用，先设好避免踩坑）
SET NAMES utf8mb4;

SELECT
    SCHEMA_NAME                     AS `库名`,
    DEFAULT_CHARACTER_SET_NAME      AS `字符集`,
    DEFAULT_COLLATION_NAME          AS `排序规则`
FROM information_schema.SCHEMATA
WHERE SCHEMA_NAME = 'fashionops';
