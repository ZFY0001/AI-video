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
from typing import Any, Iterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from pipeline.config import ROOT

# ------------------------------------------------------------------ 存储
DB_PATH = ROOT / "work" / "yiyan.db"
SECRET_PATH = ROOT / "work" / "jwt_secret.key"
RUNS = ROOT / "runs"

#: token 有效期：7 天。本地工具，太长没意义，太短天天要重登
TOKEN_TTL = 7 * 24 * 3600

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


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
                run_id  TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_run_owners_user ON run_owners(user_id);
            """
        )


def record_run_owner(run_id: str, user_id: int) -> None:
    """记下这条 run 归谁。

    为什么不在 `POST /runs` 里写：run_id 由管线在工作线程里生成，接口返回
    job_id 的那一刻它还不存在。所以只能等结果出来再补写。

    失败静默：归属丢了顶多让成片进不了"我的作品"，不该连累任务本身。
    """
    if not run_id or not user_id:
        return
    try:
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO run_owners(run_id, user_id, created) VALUES (?,?,?)",
                (str(run_id), int(user_id), time.time()),
            )
    except Exception:  # noqa: BLE001
        pass


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


def optional_user_media(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> dict[str, Any] | None:
    """媒体地址专用：`<img src>` / `<video src>` **带不了请求头**。

    封面和成片是浏览器自己发起的 GET，前端的 `apiFetch` 管不到它们——只认
    Authorization 头的话，登录用户看自己作品的封面照样 404，而列表却是正常的，
    排查起来非常费劲（表现为"有的卡片没封面"）。

    所以这两类接口额外接受 `?token=`。本地单机工具，token 本来也只存在本机
    localStorage 里，进 URL 的风险等同于进磁盘。
    """
    if authorization and authorization.lower().startswith("bearer "):
        user_id = decode_token(authorization.split(" ", 1)[1].strip())
        if user_id is not None:
            with db() as conn:
                row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return public_user(row) if row else None
    if token:
        user_id = decode_token(token.strip())
        if user_id is not None:
            with db() as conn:
                row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return public_user(row) if row else None
    return None


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
        video = path / "output" / "final.mp4"
        if video.is_file():
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
