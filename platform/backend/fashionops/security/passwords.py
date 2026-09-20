"""密码哈希与强度校验。

## 为什么是 argon2id 而不是 bcrypt

两个原因，第二个是硬伤：

1. **抗 GPU 爆破**：argon2id 是内存硬（memory-hard）的，攻击者拿 GPU 集群也堆不出
   bcrypt 那种性价比。2015 年密码哈希竞赛的获胜设计，现在是 OWASP 的首选推荐。
2. **bcrypt 有 72 字节截断，而且是静默截断**。密码超过 72 字节后面的部分
   **直接被丢掉**，不报错。中文密码按 UTF-8 算一个字 3 字节 —— 24 个汉字就到上限了。
   后果是"前 72 字节相同的两个不同密码都能登录"，而这种问题**在测试里几乎不可能发现**，
   因为没人会拿 25 个汉字的密码去测。

## 为什么哈希串自带 salt 和参数，不用额外存列

argon2 的哈希串形如 `$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>`，
盐和参数都在里面。所以数据库只要一个 `password_hash` 字段。

这还带来一个升级路径：`needs_rehash()` 能读出旧哈希用的参数，
发现比当前策略弱就在用户下次登录时**顺手重算**（`hash_password`）。
不需要"全体用户强制改密码"这种伤用户的操作。

## 为什么校验函数在密码错误和用户不存在时行为要一致

`verify_password` 对"用户不存在"也走一遍哈希计算（拿 `_DUMMY_HASH` 校验），
耗时和真校验一样。否则响应时间会泄漏"这个用户名存不存在" ——
攻击者可以先枚举出有效用户名，再去爆破密码，成本降低一个数量级。
这类漏洞叫**时序侧信道**，靠 code review 基本看不出来。
"""
from __future__ import annotations

import unicodedata

from argon2 import PasswordHasher
from argon2 import exceptions as argon2_exc

#: argon2 默认参数（argon2-cffi 选的是当前硬件的合理值，别手调）
_HASHER = PasswordHasher()

#: 一个固定的、无意义的哈希串，专门用来在"用户不存在"时消耗等量时间。
#: 它的明文是随机串，谁都猜不到，也不需要能对上。
_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$"
    "c29tZXNhbHRzb21lc2FsdA$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"
)

#: 密码最大长度。argon2 本身没有 bcrypt 那种截断问题，但设上限是为了
#: 防止有人拿 10MB 的 "密码" 来做 CPU 消耗攻击（DoS）。
MAX_PASSWORD_BYTES = 1024

#: 常见弱密码前缀/完整串。只挡最离谱的一批 ——
#: 真正的黑名单要靠 HaveIBeenPwned 之类的服务，这里不做过度设计。
_COMMON_PASSWORDS = frozenset(
    {
        "password", "passw0rd", "12345678", "123456789", "1234567890",
        "qwertyuiop", "admin123", "administrator", "letmein", "welcome",
        "iloveyou", "abc123456", "fashionops", "fashionops123",
    }
)


class PasswordPolicyError(ValueError):
    """密码不满足强度要求。消息是给最终用户看的，要说人话。"""


def _normalize(password: str) -> str:
    """Unicode NFKC 归一化。

    不做的话，同一个视觉上的密码可能有多种码点序列（全角/半角、组合字符），
    用户在手机上设置、在电脑上输入就可能对不上，而**他看到的两次输入是一样的**。
    """
    return unicodedata.normalize("NFKC", password)


def hash_password(password: str) -> str:
    """算出哈希串。异常只可能是内存/CPU 相关，不吞。"""
    return _HASHER.hash(_normalize(password))


def verify_password(password: str, password_hash: str | None) -> bool:
    """校验密码。``password_hash`` 为空（SSO 用户）时**恒返回 False**。

    注意这里也走一遍假哈希：调用方通常在"用户不存在"时传 ``None``，
    而那条路必须和"密码错"耗时一致（见模块 docstring）。
    """
    target = password_hash or _DUMMY_HASH
    try:
        _HASHER.verify(target, _normalize(password))
    except argon2_exc.VerifyMismatchError:
        return False
    except argon2_exc.InvalidHashError:
        # 库里的哈希串坏了（被手工改过、迁移时截断）→ 当作校验失败，
        # 不抛异常：抛了会让攻击者通过 500 与否区分出"这个账号哈希是坏的"
        return False
    except argon2_exc.VerificationError:
        return False
    return password_hash is not None


def needs_rehash(password_hash: str) -> bool:
    """这个哈希串是否用了比当前更弱的参数（该在下次登录时重算）。"""
    try:
        return _HASHER.check_needs_rehash(password_hash)
    except argon2_exc.InvalidHashError:
        return True


def validate_strength(
    password: str, *, min_length: int = 10, require_classes: int = 2
) -> None:
    """强度校验。不满足抛 `PasswordPolicyError`，消息直接给用户看。

    为什么要求"几类字符"而不是"必须含特殊符号"：
    强制特殊符号会把人逼到 `Password1!` 这种**比长密码更弱**的形态，
    而且大家用的符号就那么几个，攻击者的字典里早就有了。
    要求"至少 N 类"是 OWASP 的折中做法，对可用性更友好。

    长度按**字节**还是**字符**算？按字符。`min_length=10` 对"10 个汉字"
    和"10 个字母"一视同仁 —— 而汉字的信息量其实高得多。从严没有意义。
    """
    normalized = _normalize(password)

    if len(normalized) < min_length:
        raise PasswordPolicyError(f"密码至少 {min_length} 位，当前 {len(normalized)} 位")

    if len(normalized.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(f"密码过长（不能超过 {MAX_PASSWORD_BYTES} 字节）")

    if normalized.strip() == "":
        raise PasswordPolicyError("密码不能全是空白字符")

    if normalized.lower() in _COMMON_PASSWORDS:
        raise PasswordPolicyError("这个密码太常见了，换一个")

    classes = sum(
        (
            any(c.islower() for c in normalized),
            any(c.isupper() for c in normalized),
            any(c.isdigit() for c in normalized),
            any(not c.isalnum() for c in normalized),
        )
    )
    # 全中文密码只有 0 类（既非大小写也非数字符号）—— 但它的实际熵很高，
    # 所以单独放行，否则会逼中文用户去加一个数字，反而变弱。
    if classes == 0 and all("\u4e00" <= c <= "\u9fff" for c in normalized):
        return

    if classes < require_classes:
        raise PasswordPolicyError(
            f"密码需要至少包含 {require_classes} 类字符"
            "（小写字母 / 大写字母 / 数字 / 符号），当前只有 "
            f"{classes} 类"
        )


def generate_password(length: int = 20) -> str:
    """生成一个随机密码。CLI 建号时用。

    用 `secrets` 而不是 `random`：后者是可预测的 Mersenne Twister，
    在安全场景下等于没随机。
    """
    import secrets
    import string

    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    # 保证至少各有一类，避免生成出来的密码不满足自己的策略
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        try:
            validate_strength(candidate, min_length=length, require_classes=3)
        except PasswordPolicyError:
            continue
        return candidate


__all__ = [
    "MAX_PASSWORD_BYTES",
    "PasswordPolicyError",
    "generate_password",
    "hash_password",
    "needs_rehash",
    "validate_strength",
    "verify_password",
]
