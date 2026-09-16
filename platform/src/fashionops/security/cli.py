"""用户与权限命令行 —— ``fashionops-user``。

用法::

    fashionops-user create-admin admin        # 建第一个超管，密码打印一次
    fashionops-user create zhang --role operator --email z@a.com
    fashionops-user list
    fashionops-user show zhang                # 看角色与**实际生效**的权限
    fashionops-user passwd zhang              # 改密码（会撤销其全部会话）
    fashionops-user grant zhang ops_manager
    fashionops-user revoke zhang operator
    fashionops-user disable zhang / enable zhang / unlock zhang
    fashionops-user sessions zhang            # 看登录设备
    fashionops-user kick zhang                # 全部踢下线
    fashionops-user roles                     # 看角色及其权限
    fashionops-user sync                      # 把代码里的权限码同步进库

## 为什么 `create-admin` 要把密码打印到终端

因为它需要让人**拿到**密码。可选方案只有三个：

1. 自动生成 + 打印到终端 —— 选这个
2. 自动生成 + 写进日志 —— ❌ 日志会被采集到 ELK/Loki，等于把凭据公开
3. 让人用参数传 —— ❌ 会进 shell history 和进程列表（`ps` 一眼看到）

终端是**一次性、只属于当前操作者**的通道，所以它是三个里唯一合适的。
代码里也刻意把它标成"只显示一次"，并提示立刻改掉。

## 为什么所有命令都要 `--yes` 之外还要打印"做了什么"

用户与权限的变更是**需要事后审计**的：谁在什么时候给了谁什么权限。
CLI 不做权限检查（能跑这个命令的人本来就有 shell 权限），
但每条命令都写一条 `audit_logs`，让"权限是怎么变成现在这样的"可回溯。
"""

from __future__ import annotations

import sys
from argparse import ArgumentParser, Namespace

from sqlalchemy import func, select

from fashionops.config import get_settings
from fashionops.db import session_scope
from fashionops.db.models_identity import (
    USER_STATUS_ACTIVE,
    USER_STATUS_DISABLED,
    Role,
    User,
    UserRole,
)
from fashionops.security import service
from fashionops.security.permissions import (
    PERMISSIONS,
    ROLE_PRESETS,
    user_permission_codes,
)
from fashionops.security.service import AuthError

# Windows 控制台默认 GBK，打印中文和 emoji 会直接抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _ok(message: str) -> None:
    print(f"✓ {message}")


def _warn(message: str) -> None:
    print(f"⚠ {message}")


def _fail(message: str) -> None:
    print(f"✗ {message}", file=sys.stderr)


def _audit(session, *, action: str, target: str, detail: dict) -> None:  # noqa: ANN001
    """CLI 的操作也进审计。

    走 `record_audit` 的同一张表，actor_username 记 `cli:<登录名>` ——
    这样在审计列表里能一眼看出"这条不是从界面点的"。
    """
    import getpass

    from fashionops.db.models import AuditLog

    try:
        actor = getpass.getuser()
    except Exception:  # noqa: BLE001
        actor = "cli"

    session.add(
        AuditLog(
            tool_name="cli:fashionops-user",
            action=action,
            resource=target,
            actor=f"cli:{actor}"[:64],
            actor_username=f"cli:{actor}"[:64],
            args_digest=detail,
            status="success",
        )
    )
    session.flush()


def _require_user(session, username: str) -> User:  # noqa: ANN001
    user = service.find_user(session, username)
    if user is None:
        _fail(f"用户 {username} 不存在")
        raise SystemExit(1)
    return user


# ---------------------------------------------------------------------------
# 各子命令
# ---------------------------------------------------------------------------
def cmd_create_admin(args: Namespace) -> int:
    from fashionops.security.permissions import ROLE_ADMIN

    with session_scope() as session:
        try:
            result = service.create_user(
                session,
                username=args.username,
                password=args.password,  # None 则自动生成
                email=args.email,
                full_name=args.full_name or "系统管理员",
                roles=(ROLE_ADMIN,),
                is_superuser=True,
                created_by="cli",
                settings=get_settings(),
            )
        except AuthError as exc:
            _fail(str(exc))
            return 1

        _audit(session, action="user.create_admin", target=args.username, detail={"superuser": True})
        _ok(f"已创建超级管理员 {result.user.username}（id={result.user.id}）")
        if result.generated_password:
            print()
            print("  ┌─ 初始密码（**只显示这一次**）─" + "─" * 22)
            print(f"  │  {result.generated_password}")
            print("  └─" + "─" * 40)
            print("  请立即登录并改掉：fashionops-user passwd <username>")
        for warning in result.warnings:
            _warn(warning)
    return 0


def cmd_create(args: Namespace) -> int:
    with session_scope() as session:
        try:
            result = service.create_user(
                session,
                username=args.username,
                password=args.password,
                email=args.email,
                full_name=args.full_name,
                roles=tuple(args.role or ()),
                created_by="cli",
                settings=get_settings(),
            )
        except AuthError as exc:
            _fail(str(exc))
            return 1

        _audit(
            session,
            action="user.create",
            target=args.username,
            detail={"roles": list(args.role or ())},
        )
        _ok(f"已创建用户 {result.user.username}（id={result.user.id}）")
        if result.generated_password:
            print()
            print("  ┌─ 初始密码（**只显示这一次**）─" + "─" * 22)
            print(f"  │  {result.generated_password}")
            print("  └─" + "─" * 40)
        if not args.role:
            _warn("没有分配任何角色 —— 该用户能登录但什么都做不了。分配角色：fashionops-user grant")
        for warning in result.warnings:
            _warn(warning)
    return 0


def cmd_list(args: Namespace) -> int:
    with session_scope() as session:
        users = list(session.scalars(select(User).order_by(User.id)))
        if not users:
            print("（还没有任何用户）")
            print("建第一个管理员：fashionops-user create-admin admin")
            return 0

        counts = dict(
            session.execute(
                select(UserRole.user_id, func.count()).group_by(UserRole.user_id)
            ).all()
        )

        print(f"{'ID':>4}  {'用户名':<20} {'状态':<9} {'角色':<28} 最后登录")
        print("-" * 96)
        for user in users:
            flag = "★" if user.is_superuser else " "
            roles = ",".join(user.role_codes()) or "-"
            last = user.last_login_at.strftime("%Y-%m-%d %H:%M") if user.last_login_at else "从未"
            status = user.status
            if user.is_locked_now:
                status = "locked*"
            print(
                f"{user.id:>4} {flag} {user.username:<19} {status:<9} {roles:<28} {last}"
                f"  ({counts.get(user.id, 0)} 角色)"
            )
        print()
        print(f"共 {len(users)} 个用户（★ = 超级管理员）")
    return 0


def cmd_show(args: Namespace) -> int:
    with session_scope() as session:
        user = _require_user(session, args.username)
        perms = user_permission_codes(user)

        print(f"用户      : {user.username}（id={user.id}）")
        print(f"姓名/邮箱 : {user.full_name or '-'} / {user.email or '-'}")
        print(f"状态      : {user.status}" + ("（**锁定中**）" if user.is_locked_now else ""))
        print(f"超级管理员: {'是（绕过全部权限检查）' if user.is_superuser else '否'}")
        print(f"角色      : {', '.join(user.role_codes()) or '（无）'}")
        print(f"失败次数  : {user.failed_login_count}")
        if user.password_hash is None:
            print("密码      : **未设置**（只能走第三方扫码登录）")
        print()

        print(f"实际生效的权限（{len(perms)} 项）:")
        if not perms:
            print("  （无）—— 该用户能登录，但任何需要权限的操作都会被拒")
        else:
            by_cat: dict[str, list[str]] = {}
            for spec in PERMISSIONS:
                if spec.code in perms:
                    by_cat.setdefault(spec.category, []).append(spec.code)
            for category in sorted(by_cat):
                print(f"  [{category}] {'  '.join(sorted(by_cat[category]))}")

        # 有角色但没权限 → 大概率是角色授权被清空了，而这个现象很难自行发现
        if user.role_codes() and not perms:
            _warn("有角色但没有任何权限 —— 角色授权可能被清空了，跑 `fashionops-user roles` 看")
    return 0


def cmd_passwd(args: Namespace) -> int:
    with session_scope() as session:
        user = _require_user(session, args.username)
        password = args.password
        if password is None:
            from fashionops.security.passwords import generate_password

            password = generate_password()
            print("  新密码（**只显示这一次**）:", password)

        try:
            service.set_password(session, user, password)
        except Exception as exc:  # noqa: BLE001
            _fail(f"密码不符合要求：{exc}")
            return 1

        _audit(session, action="user.set_password", target=args.username, detail={})
        _ok("密码已更新，该用户的**全部登录会话已撤销**（需要重新登录）")
    return 0


def cmd_grant(args: Namespace) -> int:
    with session_scope() as session:
        user = _require_user(session, args.username)
        try:
            added = service.assign_role(session, user, args.role, granted_by="cli")
        except AuthError as exc:
            _fail(str(exc))
            return 1
        if not added:
            _warn(f"用户 {user.username} 已经有 {args.role} 角色了（未重复添加）")
            return 0
        _audit(
            session, action="user.grant_role", target=args.username, detail={"role": args.role}
        )
        _ok(f"已授予 {user.username} 角色 {args.role}")
        # 权限是每次请求现算的，所以这里**不需要**提示"重新登录才生效"
        print("  权限立即生效，用户不需要重新登录（权限不从 token 里读）")
    return 0


def cmd_revoke(args: Namespace) -> int:
    with session_scope() as session:
        user = _require_user(session, args.username)
        role_row = session.scalar(select(Role).where(Role.code == args.role))
        if role_row is None:
            _fail(f"角色 {args.role} 不存在")
            return 1
        row = session.scalar(
            select(UserRole).where(
                UserRole.user_id == user.id, UserRole.role_id == role_row.id
            )
        )
        if row is None:
            _warn(f"用户 {user.username} 本来就没有 {args.role} 角色")
            return 0
        session.delete(row)
        _audit(
            session, action="user.revoke_role", target=args.username, detail={"role": args.role}
        )
        _ok(f"已撤销 {user.username} 的角色 {args.role}")
    return 0


def cmd_set_status(args: Namespace) -> int:
    status = USER_STATUS_ACTIVE if args.action == "enable" else USER_STATUS_DISABLED
    with session_scope() as session:
        user = _require_user(session, args.username)
        user.status = status
        if status == USER_STATUS_ACTIVE:
            user.failed_login_count = 0
            user.locked_until = None

        revoked = 0
        if status == USER_STATUS_DISABLED:
            # 停用必须同时踢下线。只改 status 的话，用户手里那个 access token
            # 在过期前（最长 30 分钟）**仍然能用** —— 因为鉴权只查库里的 user.status，
            # 而 refresh 才查会话。停用的意图是"立刻不能用了"。
            revoked = service.revoke_all(session, user.id, reason="user_disabled")

        _audit(session, action=f"user.{args.action}", target=args.username, detail={})
        _ok(f"用户 {user.username} 已{'启用' if status == USER_STATUS_ACTIVE else '停用'}")
        if revoked:
            print(f"  已同时撤销 {revoked} 个登录会话")
    return 0


def cmd_unlock(args: Namespace) -> int:
    with session_scope() as session:
        user = _require_user(session, args.username)
        user.failed_login_count = 0
        user.locked_until = None
        if user.status != USER_STATUS_ACTIVE:
            user.status = USER_STATUS_ACTIVE
        _audit(session, action="user.unlock", target=args.username, detail={})
        _ok(f"已解锁 {user.username}")
    return 0


def cmd_sessions(args: Namespace) -> int:
    with session_scope() as session:
        user = _require_user(session, args.username)
        rows = service.list_sessions(session, user.id)
        if not rows:
            print(f"用户 {user.username} 当前没有有效会话")
            return 0
        print(f"{'ID':>4}  {'创建时间':<20} {'过期时间':<20} {'来源 IP':<18} User-Agent")
        print("-" * 118)
        for row in rows:
            print(
                f"{row.id:>4}  {row.created_at:%Y-%m-%d %H:%M:%S}   "
                f"{row.expires_at:%Y-%m-%d %H:%M:%S}   {row.ip or '-':<18} "
                f"{(row.user_agent or '-')[:40]}"
            )
        print()
        print(f"共 {len(rows)} 个有效会话。全部踢下线：fashionops-user kick {user.username}")
    return 0


def cmd_kick(args: Namespace) -> int:
    with session_scope() as session:
        user = _require_user(session, args.username)
        count = service.revoke_all(session, user.id, reason="cli_kick")
        _audit(session, action="user.kick", target=args.username, detail={"count": count})
        _ok(f"已撤销 {user.username} 的 {count} 个会话")
        if count:
            print("  注意：**已经签发出去的 access token 在过期前仍然有效**（无状态 JWT）。")
            print(f"  最长 {get_settings().access_token_minutes} 分钟后彻底失效。")
            print("  需要立刻生效时，用 `disable` 停用账号（那条路径会查库校验状态）。")
    return 0


def cmd_roles(args: Namespace) -> int:
    with session_scope() as session:
        roles = list(session.scalars(select(Role).order_by(Role.id)))
        if not roles:
            print("（还没有角色）跑 `fashionops-user sync` 创建内置角色")
            return 0
        presets = {r.code: r for r in ROLE_PRESETS}
        for role in roles:
            user_count = session.scalar(
                select(func.count()).select_from(UserRole).where(UserRole.role_id == role.id)
            )
            codes = sorted(role.permission_codes())
            print(f"[{role.code}] {role.name}  · {user_count} 人  · {len(codes)} 项权限")
            print(f"    {role.description or '-'}")
            default = presets.get(role.code)
            if default is not None:
                # 和代码里的默认授权对比：不一致说明有人改过授权，
                # 这是**正常且允许的**，但要让人看得见，否则"改了没生效"会很难查
                now = set(codes)
                if now != set(default.permissions):
                    added = now - default.permissions
                    removed = default.permissions - now
                    delta = []
                    if added:
                        delta.append(f"+{','.join(sorted(added))}")
                    if removed:
                        delta.append(f"-{','.join(sorted(removed))}")
                    print(f"    与代码默认不同：{'  '.join(delta)}（这是允许的，以库为准）")
            if codes:
                by_cat: dict[str, list[str]] = {}
                for spec in PERMISSIONS:
                    if spec.code in codes:
                        by_cat.setdefault(spec.category, []).append(spec.code)
                for category in sorted(by_cat):
                    print(f"    [{category}] {'  '.join(sorted(by_cat[category]))}")
            print()
    return 0


def cmd_sync(args: Namespace) -> int:
    from fashionops.security.permissions import sync_identity

    with session_scope() as session:
        stats = sync_identity(session)
        _audit(session, action="identity.sync", target="permissions+roles", detail=stats)
        perms = stats["permissions"]
        roles = stats["roles"]
        _ok(
            f"权限同步完成：新增 {perms['created']} / 更新 {perms['updated']} / 移除 {perms['removed']}"
        )
        _ok(f"角色同步完成：新增 {roles['created']} 个角色，写入 {roles['granted']} 条授权")
        if perms["removed"]:
            _warn(
                f"有 {perms['removed']} 个权限码在代码里已不存在，已从库中删除。"
                "如果这是误删，说明代码里的 PERMISSIONS 少了东西"
            )
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="fashionops-user",
        description="用户与权限管理（认证系统只有这一套入口，HTTP 与 CLI 共用同一份规则）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-admin", help="创建超级管理员（第一个账号用这个）")
    p.add_argument("username")
    p.add_argument("--password", default=None, help="留空自动生成并打印一次")
    p.add_argument("--email", default=None)
    p.add_argument("--full-name", default="")
    p.set_defaults(func=cmd_create_admin)

    p = sub.add_parser("create", help="创建普通用户")
    p.add_argument("username")
    p.add_argument("--password", default=None, help="留空自动生成并打印一次")
    p.add_argument("--email", default=None)
    p.add_argument("--full-name", default="")
    p.add_argument("--role", action="append", help="可重复：--role operator --role analyst")
    p.set_defaults(func=cmd_create)

    sub.add_parser("list", help="列出全部用户").set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="看某个用户的角色与**实际生效**的权限")
    p.add_argument("username")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("passwd", help="改密码（会撤销该用户全部会话）")
    p.add_argument("username")
    p.add_argument("--password", default=None, help="留空自动生成并打印一次")
    p.set_defaults(func=cmd_passwd)

    p = sub.add_parser("grant", help="授予角色")
    p.add_argument("username")
    p.add_argument("role")
    p.set_defaults(func=cmd_grant)

    p = sub.add_parser("revoke", help="撤销角色")
    p.add_argument("username")
    p.add_argument("role")
    p.set_defaults(func=cmd_revoke)

    for action, help_text in (("enable", "启用账号"), ("disable", "停用账号（并踢下线）")):
        p = sub.add_parser(action, help=help_text)
        p.add_argument("username")
        p.set_defaults(func=cmd_set_status, action=action)

    p = sub.add_parser("unlock", help="解除登录失败锁定")
    p.add_argument("username")
    p.set_defaults(func=cmd_unlock)

    p = sub.add_parser("sessions", help="看某个用户的登录设备")
    p.add_argument("username")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("kick", help="把某个用户全部踢下线")
    p.add_argument("username")
    p.set_defaults(func=cmd_kick)

    sub.add_parser("roles", help="列出角色及其权限").set_defaults(func=cmd_roles)
    sub.add_parser("sync", help="把代码里的权限码/内置角色同步进库").set_defaults(func=cmd_sync)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SystemExit as exc:
        return int(exc.code or 0)
    except AuthError as exc:
        _fail(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
