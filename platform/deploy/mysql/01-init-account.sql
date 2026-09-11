-- ============================================================================
-- 服装电商多 Agent 运营中台 —— 创建应用专用账号
-- ============================================================================
-- 【为什么不用 root】
--   应用进程拥有 root 权限时，任何一处 SQL 注入或依赖投毒都能直接 DROP DATABASE。
--   专用账号只拿到 fashionops 一个库的权限，爆炸半径被限制住。
--
-- 【为什么不把密码写死在这个文件里】
--   这个文件在 git 仓库里。密码由 init-mysql.ps1 随机生成后直接写进 .env，
--   不经过版本控制，也不经过任何外部服务。
--
-- 手动执行方式（把 <PASSWORD> 换成你自己生成的强密码）：
--   mysql -u root -p < 01-init-account.sql
-- ============================================================================

-- 只允许本机连接：开发期不需要从其他机器访问
CREATE USER IF NOT EXISTS 'fashionops'@'localhost'
    IDENTIFIED BY '<PASSWORD>';

-- 幂等：账号已存在时重置密码（方便重跑脚本）
ALTER USER 'fashionops'@'localhost'
    IDENTIFIED BY '<PASSWORD>';

-- 只给这一个库的权限，不给全局权限
GRANT ALL PRIVILEGES ON `fashionops`.* TO 'fashionops'@'localhost';

FLUSH PRIVILEGES;

-- 确认结果
SELECT
    user            AS `账号`,
    host            AS `来源`,
    plugin          AS `认证插件`
FROM mysql.user
WHERE user = 'fashionops';

SHOW GRANTS FOR 'fashionops'@'localhost';
