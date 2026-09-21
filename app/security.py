"""安全工具：AES-256-GCM 凭据加密、API Key 生成/哈希、管理端 JWT。"""
import base64
import hashlib
import hmac
import secrets

import jwt

from . import config

_SECRET = b""


def init_security(secret_key: str):
    global _SECRET
    _SECRET = bytes.fromhex(secret_key) if _is_hex(secret_key) else secret_key.encode("utf-8")
    if len(_SECRET) < 32:
        _SECRET = hashlib.sha256(_SECRET).digest()


def _is_hex(s: str) -> bool:
    try:
        bytes.fromhex(s)
        return True
    except ValueError:
        return False


# ---------- AES-256-GCM ----------

def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aead = AESGCM(_SECRET)
    nonce = secrets.token_bytes(12)
    ct = aead.encrypt(nonce, plaintext.encode("utf-8"), None)
    return "v1:" + base64.b64encode(nonce + ct).decode("ascii")


def decrypt(token: str) -> str:
    if not token:
        return ""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not token.startswith("v1:"):
        return ""
    raw = base64.b64decode(token[3:])
    nonce, ct = raw[:12], raw[12:]
    aead = AESGCM(_SECRET)
    return aead.decrypt(nonce, ct, None).decode("utf-8")


# ---------- API Key ----------

def generate_api_key() -> tuple[str, str, str, str]:
    """返回 (完整key, key_hash, key_prefix, key_id)。"""
    key = "sk-cf-" + secrets.token_hex(24)
    return key, hash_api_key(key), key[:14], "key_" + secrets.token_hex(4)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def mask_secret(value: str, keep: int = 8) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "******"


# ---------- 管理端 JWT ----------

JWT_ALG = "HS256"
JWT_TTL = 12 * 3600


def issue_admin_token(username: str) -> str:
    import time
    now = int(time.time())
    return jwt.encode(
        {"sub": username, "iat": now, "exp": now + JWT_TTL, "scope": "admin"},
        _SECRET, algorithm=JWT_ALG,
    )


def verify_admin_token(token: str) -> bool:
    try:
        payload = jwt.decode(token, _SECRET, algorithms=[JWT_ALG])
        return payload.get("scope") == "admin"
    except Exception:
        return False


def check_admin_password(username: str, password: str) -> bool:
    cfg = config.get_config()
    expect_user = cfg.ADMIN_USER
    expect_pass = cfg.ADMIN_PASSWORD or cfg.ADMIN_PASSWORD_DEFAULT
    return hmac.compare_digest(username, expect_user) and hmac.compare_digest(password, expect_pass)
