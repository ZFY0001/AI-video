"""账号体系：注册 / 登录 / JWT / 个人资料 / 使用统计。

**为什么不引 PyJWT / passlib**
本项目的依赖原则是"标准库优先"（`requirements.txt` 里只有管线必需的那几个）。
HS256 的签发与校验本质就是 `base64url + hmac-sha256`，三十行写完，没有版本
兼容风险；密码哈希用标准库 `hashlib.scrypt`（内存硬，抗 GPU 暴力破解），
比 bcrypt 少装一个 C 扩展。

**数据放哪**
`work/yiyan.db`（SQLite，标准库）。放 work/ 而不是项目根，是因为 work/
已经是"运行时产物"目录，备份与清理规则都现成。

**认证方式**
`Authorization: Bearer <token>`。token 只放用户 id 与过期时间，不放昵称邮箱
——资料会变，签进去之后就永远过期不了。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from pipeline.config import ROOT

from . import artifacts

# ------------------------------------------------------------------ 存储
DB_PATH = ROOT / "work" / "yiyan.db"
SECRET_PATH = ROOT / "work" / "jwt_secret.key"
RUNS = ROOT / "runs"

#: token 有效期：7 天。本地工具，太长没意义，太短天天要重登
TOKEN_TTL = 7 * 24 * 3600

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

#: 访客凭据：浏览器本地生成的 32 位十六进制串。
#: 它不是账号，也不换 token——只用来把"游客创建的项目"绑到这台浏览器上。
#: 没有它，游客项目就是无主的，任何能访问本机服务的人都能读、能取消。
_GUEST_RE = re.compile(r"^[0-9a-f]{32}$")


class OwnershipUnavailable(RuntimeError):
    """归属查询失败（数据库不可用等）。

    调用方必须**拒绝访问**。把查询失败当成"没有归属记录"，就等于数据库一坏、
    所有私有项目自动变成公开——这是最不该出现的失败方向。
    """


class OwnerConflict(RuntimeError):
    """项目已有其他归属。任何创建路径都不该覆盖它。"""


@dataclass(frozen=True)
class Principal:
    """请求主体：注册用户或匿名访客。"""

    kind: str          # "user" | "guest"
    id: int | str
    user: dict[str, Any] | None = None

    @property
    def key(self) -> str:
        return f"u:{self.id}" if self.kind == "user" else f"g:{self.id}"

    @property
    def user_id(self) -> int | None:
        return int(self.id) if self.kind == "user" else None


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """一次请求一个连接。

    `with sqlite3.connect(...)` 只提交事务、**不关闭连接**，直接用会泄漏句柄；
    这里用 closing 语义包一层：正常提交、异常回滚、无论如何关闭。
    每请求新建（约 0.1ms）比维护线程池+锁简单，也不会踩"跨线程复用连接"。
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")   # 读写并发不互斥
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
                nickname      TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                avatar        TEXT NOT NULL DEFAULT '',
                bio           TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_owners (
                run_id    TEXT PRIMARY KEY,
                user_id   INTEGER NOT NULL DEFAULT 0,
                owner_key TEXT NOT NULL DEFAULT '',
                created   REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_run_owners_user ON run_owners(user_id);
            """
        )
        # 老库没有 owner_key：补列并回填。旧记录都是注册用户的，回填成 u:<id>。
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(run_owners)")}
        if "owner_key" not in columns:
            conn.execute("ALTER TABLE run_owners ADD COLUMN owner_key TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "UPDATE run_owners SET owner_key = 'u:' || user_id "
            "WHERE owner_key = '' AND user_id > 0"
        )


def record_owner(run_id: str, principal: Principal | None) -> None:
    """记下这条 run 归谁。已有记录不覆盖。

    失败**不再静默**：抛 `OwnershipUnavailable`，由调用方决定是中止任务还是
    在界面/日志里留痕。归属丢了，成片就成了"公共"的——静默失败等于悄悄降低
    安全性。
    """
    if not run_id or principal is None:
        return
    key = principal.key
    user_id = principal.user_id or 0
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT owner_key, user_id FROM run_owners WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            if row is not None:
                existing = row["owner_key"] or (f"u:{row['user_id']}" if row["user_id"] else "")
                if existing and existing != key:
                    raise OwnerConflict(f"{run_id} 已归属其他账号")
                return
            conn.execute(
                "INSERT INTO run_owners(run_id, user_id, owner_key, created) VALUES (?,?,?,?)",
                (str(run_id), user_id, key, time.time()),
            )
    except OwnerConflict:
        raise
    except sqlite3.IntegrityError as exc:
        # 并发首次登记：另一个请求刚好插进去了。按现有归属判定，而不是报故障。
        existing = run_owner_key(run_id)
        if existing == key:
            return
        raise OwnerConflict(f"{run_id} 已被其他主体登记") from exc
    except sqlite3.Error as exc:
        raise OwnershipUnavailable(f"归属写入失败：{exc}") from exc


def run_owner_key(run_id: str) -> str | None:
    """读归属键。None = 没有记录（账号体系上线前的旧项目）。

    数据库故障抛 `OwnershipUnavailable`，绝不返回 None——否则故障状态下所有
    私有项目都会被判成公共。
    """
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT owner_key, user_id FROM run_owners WHERE run_id = ?", (str(run_id),)
            ).fetchone()
    except sqlite3.Error as exc:
        raise OwnershipUnavailable(f"归属查询失败：{exc}") from exc
    if row is None:
        return None
    return row["owner_key"] or (f"u:{row['user_id']}" if row["user_id"] else None)


# ------------------------------------------------------------------ 密码
def hash_password(password: str) -> str:
    """scrypt 哈希，格式 `scrypt$n$r$p$salt$hash`（base64）。

    n=2^14 / r=8 / p=1 是 scrypt 的常用交互档：单次约 50-80ms，
    够挡住离线爆破，又不至于让登录变慢。
    """
    n, r, p = 2 ** 14, 8, 1
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=n, r=r, p=p, dklen=32)
    enc = lambda b: base64.b64encode(b).decode()   # noqa: E731
    return f"scrypt${n}${r}${p}${enc(salt)}${enc(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=int(n), r=int(r), p=int(p), dklen=len(expected))
        # 定长时间比较：否则"前几位对了就慢一点"会把哈希泄露出去
        return hmac.compare_digest(dk, expected)
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ JWT
def _secret() -> bytes:
    """首次运行随机生成并落盘。

    不写死默认密钥——那等于没有签名。权限尽力设为仅本人可读
    （Windows 上 chmod 语义有限，失败不影响功能）。
    """
    if SECRET_PATH.exists():
        return SECRET_PATH.read_bytes()
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(48)
    SECRET_PATH.write_bytes(key)
    try:
        os.chmod(SECRET_PATH, 0o600)
    except OSError:
        pass
    return key


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_token(user_id: int) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"},
                                separators=(",", ":")).encode())
    now = int(time.time())
    payload = _b64url(json.dumps({"sub": user_id, "iat": now, "exp": now + TOKEN_TTL},
                                 separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    sig = hmac.new(_secret(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


def decode_token(token: str) -> int | None:
    """校验签名与过期，返回 user_id；任何异常都返回 None。"""
    try:
        header, payload, sig = token.split(".")
        expected = hmac.new(_secret(), f"{header}.{payload}".encode(),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url_decode(sig), expected):
            return None
        data = json.loads(_b64url_decode(payload))
        if int(data.get("exp", 0)) < int(time.time()):
            return None
        return int(data["sub"])
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ 依赖
def public_user(row: Any) -> dict[str, Any]:
    """对外返回的用户信息：**绝不带 password_hash**。"""
    return {
        "id": int(row["id"]),
        "email": row["email"],
        "nickname": row["nickname"],
        "avatar": row["avatar"] or "",
        "bio": row["bio"] or "",
        "created_at": row["created_at"],
    }


def optional_user(authorization: str | None = Header(default=None)) -> dict[str, Any] | None:
    """可选认证：缺 token / 过期 / 签名不对，一律返回 None，不抛错。

    创作台允许游客使用，所以大部分接口用这个；个人中心才用 require_user。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    user_id = decode_token(authorization.split(" ", 1)[1].strip())
    if user_id is None:
        return None
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return public_user(row) if row else None


def require_user(user: dict[str, Any] | None = Depends(optional_user)) -> dict[str, Any]:
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def _user_principal(user_id: int) -> Principal | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return Principal("user", int(row["id"]), public_user(row)) if row else None


def _resolve_principal(authorization: str | None, guest_id: str | None) -> Principal | None:
    """账号优先；没有账号时才退到访客凭据。

    两者都存在时只认账号，避免"登录后还用着切号前的游客项目"这种越权路径。
    """
    if authorization and authorization.lower().startswith("bearer "):
        user_id = decode_token(authorization.split(" ", 1)[1].strip())
        if user_id is not None:
            return _user_principal(user_id)
    guest = (guest_id or "").strip().lower()
    if guest and _GUEST_RE.match(guest):
        return Principal("guest", guest)
    return None


def optional_principal(
    authorization: str | None = Header(default=None),
    x_guest_id: str | None = Header(default=None),
) -> Principal | None:
    """可选主体：注册用户或访客。都缺时返回 None。"""
    return _resolve_principal(authorization, x_guest_id)


def require_principal(
    principal: Principal | None = Depends(optional_principal),
) -> Principal:
    """创建入口用：新项目必须有归属主体，否则只能生成一份"公共"数据。"""
    if principal is None:
        raise HTTPException(status_code=401, detail="缺少访客凭据，请刷新页面后重试")
    return principal


def optional_principal_media(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
    guest: str | None = Query(default=None),
) -> Principal | None:
    """媒体/SSE 专用主体解析：`<img>` / `<video>` / `EventSource` 带不了请求头。

    与 `optional_user_media` 同样接受 `?token=`，额外接受 `?guest=`。
    """
    if authorization and authorization.lower().startswith("bearer "):
        user_id = decode_token(authorization.split(" ", 1)[1].strip())
        if user_id is not None:
            return _user_principal(user_id)
    if token:
        user_id = decode_token(token.strip())
        if user_id is not None:
            return _user_principal(user_id)
    return _resolve_principal(None, guest)


# ------------------------------------------------------------------ 请求体
class RegisterBody(BaseModel):
    email: str = Field(min_length=5, max_length=120)
    password: str = Field(min_length=8, max_length=128)
    nickname: str = Field(default="", max_length=24)


class LoginBody(BaseModel):
    email: str = Field(min_length=5, max_length=120)
    password: str = Field(min_length=1, max_length=128)


class ProfileBody(BaseModel):
    nickname: str | None = Field(default=None, max_length=24)
    bio: str | None = Field(default=None, max_length=120)
    avatar: str | None = Field(default=None, max_length=16)


# ------------------------------------------------------------------ 路由
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register")
def register(body: RegisterBody) -> dict[str, Any]:
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="邮箱格式不对")
    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="密码至少 8 位")
    nickname = (body.nickname or "").strip() or email.split("@")[0][:24]

    with db() as conn:
        if conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
            raise HTTPException(status_code=409, detail="这个邮箱已经注册过了")
        cur = conn.execute(
            "INSERT INTO users(email, nickname, password_hash, created_at) VALUES (?,?,?,?)",
            (email, nickname, hash_password(body.password),
             time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        user_id = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    return {"token": sign_token(user_id), "user": public_user(row)}


@router.post("/login")
def login(body: LoginBody) -> dict[str, Any]:
    email = body.email.strip().lower()
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    # 邮箱不存在与密码错误给同一句话：别告诉攻击者"这个邮箱注册过"
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="邮箱或密码不正确")
    return {"token": sign_token(int(row["id"])), "user": public_user(row)}


@router.get("/me")
def me(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return {"user": user}


@router.patch("/me")
def update_me(body: ProfileBody,
              user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    fields: dict[str, str] = {}
    if body.nickname is not None:
        name = body.nickname.strip()
        if not name:
            raise HTTPException(status_code=422, detail="昵称不能为空")
        fields["nickname"] = name
    if body.bio is not None:
        fields["bio"] = body.bio.strip()
    if body.avatar is not None:
        fields["avatar"] = body.avatar.strip()

    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        with db() as conn:
            conn.execute(f"UPDATE users SET {sets} WHERE id = ?",
                         (*fields.values(), user["id"]))
            row = conn.execute("SELECT * FROM users WHERE id = ?",
                               (user["id"],)).fetchone()
        user = public_user(row)
    return {"user": user}


@router.get("/stats")
def stats(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    """个人中心用的汇总：作品数 / 总时长 / 总大小 / 近 7 天新增。

    只统计**归属于自己的** run。旧数据没有归属记录，不计入任何人的统计——
    宁可少算，也不能把别人的作品算到某个人头上。
    """
    with db() as conn:
        owned = [r["run_id"] for r in conn.execute(
            "SELECT run_id FROM run_owners WHERE user_id = ? ORDER BY created DESC",
            (user["id"],)).fetchall()]

    total, duration, size_mb, week = 0, 0.0, 0.0, 0
    cutoff = time.time() - 7 * 86400
    for run_id in owned:
        path = RUNS / run_id
        if not path.is_dir():
            continue        # 目录被删了但记录还在，跳过
        total += 1
        try:
            if path.stat().st_mtime >= cutoff:
                week += 1
        except OSError:
            pass
        board = path / "output" / "storyboard.json"
        if board.is_file():
            try:
                project = (json.loads(board.read_text(encoding="utf-8"))
                           .get("project") or {})
                duration += float(project.get("total_duration") or 0)
            except Exception:  # noqa: BLE001
                pass
        video = artifacts.current_video(path)
        if video is not None:
            try:
                size_mb += video.stat().st_size / 1048576
            except OSError:
                pass

    return {
        "runs": total,
        "total_duration": round(duration, 1),
        "total_size_mb": round(size_mb, 1),
        "week_runs": week,
        "storage_mb": round(size_mb, 1),
    }
