"""Creative Fabrica OAuth：动态发现元数据 + 动态客户端注册 + PKCE 授权码流程 + 刷新。"""
import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx

from .config import get_config

_metadata_cache: dict = {}


class OAuthError(Exception):
    pass


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


async def discover(force: bool = False) -> dict:
    """发现 AS 元数据（authorization_endpoint / token_endpoint / registration_endpoint）。"""
    global _metadata_cache
    if _metadata_cache and not force:
        return _metadata_cache
    cfg = get_config()
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            # 先取受保护资源元数据，确认 authorization_servers
            try:
                pr = await c.get(
                    f"{cfg.MCP_RESOURCE.rstrip('/')}/.well-known/oauth-protected-resource"
                )
                if pr.status_code >= 400:
                    pr = await c.get(
                        cfg.MCP_RESOURCE.rstrip("/").rsplit("/", 1)[0]
                        + "/.well-known/oauth-protected-resource"
                    )
                pr.raise_for_status()
                servers = pr.json().get("authorization_servers") or []
                issuer = servers[0] if servers else cfg.OAUTH_ISSUER
            except Exception:
                issuer = cfg.OAUTH_ISSUER

            meta = await c.get(f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server")
            meta.raise_for_status()
            _metadata_cache = meta.json()
            _metadata_cache["_issuer"] = issuer
            return _metadata_cache
    except Exception as e:
        raise OAuthError(f"OAuth 元数据发现失败: {e}")


async def register_client(redirect_uri: str) -> dict:
    """RFC 7591 动态客户端注册。返回 {"client_id":..., "client_secret": str|None}。"""
    meta = await discover()
    reg_ep = meta.get("registration_endpoint")
    if not reg_ep:
        raise OAuthError("授权服务器不支持动态客户端注册，请手动配置 client_id")
    cfg = get_config()
    body = {
        "client_name": cfg.CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
        "scope": cfg.OAUTH_SCOPES,
    }
    async with httpx.AsyncClient(timeout=20) as c:
        resp = await c.post(reg_ep, json=body)
        if resp.status_code >= 400:
            raise OAuthError(f"动态注册失败({resp.status_code}): {resp.text[:200]}")
        data = resp.json()
    return {
        "client_id": data["client_id"],
        "client_secret": data.get("client_secret") or "",
    }


def build_authorize_url(client_id: str, state: str, redirect_uri: str,
                        meta: dict) -> tuple[str, str]:
    """返回 (authorize_url, code_verifier)。"""
    verifier, challenge = _pkce_pair()
    cfg = get_config()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": cfg.OAUTH_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    # RFC 8707 resource 参数：请求受众绑定的令牌（服务器不支持时忽略）
    params["resource"] = cfg.MCP_RESOURCE
    url = meta["authorization_endpoint"] + "?" + urlencode(params)
    return url, verifier


async def exchange_code(code: str, verifier: str, redirect_uri: str,
                        client_id: str, client_secret: str) -> dict:
    """授权码换令牌。返回 {access_token, refresh_token, expires_in, scope, token_type}。"""
    meta = await discover()
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    if client_secret:
        body["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=30) as c:
        resp = await c.post(meta["token_endpoint"], data=body,
                            headers={"Accept": "application/json"})
        if resp.status_code >= 400:
            raise OAuthError(f"令牌交换失败({resp.status_code}): {resp.text[:300]}")
        return resp.json()


async def refresh_token(refresh_tok: str, client_id: str, client_secret: str) -> dict:
    meta = await discover()
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": client_id,
    }
    if client_secret:
        body["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=30) as c:
        resp = await c.post(meta["token_endpoint"], data=body,
                            headers={"Accept": "application/json"})
        if resp.status_code >= 400:
            raise OAuthError(f"令牌刷新失败({resp.status_code}): {resp.text[:300]}")
        return resp.json()
