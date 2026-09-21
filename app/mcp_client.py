"""Creative Fabrica MCP (Streamable HTTP) 客户端。

协议要点：
- POST JSON-RPC 2.0 到 MCP endpoint，Accept 同时允许 JSON 与 SSE
- initialize 响应头可能返回 Mcp-Session-Id，后续请求需携带
- initialize 成功后发送 notifications/initialized 通知
- tools/list 支持游标分页；tools/call 返回 content 数组
- 401 由上层（服务层）负责刷新令牌后重试
"""
import json
import re

import httpx

from .config import get_config


class MCPError(Exception):
    """携带上游状态码的 MCP 调用异常。"""

    def __init__(self, message: str, status: int = 0, *, transient: bool = False):
        super().__init__(message)
        self.status = status
        self.transient = transient

    @property
    def is_auth_error(self) -> bool:
        return self.status == 401 or self.status == 403

    @property
    def is_balance_error(self) -> bool:
        return self.status == 402

    @property
    def is_rate_limited(self) -> bool:
        return self.status == 429


def _parse_response(resp: httpx.Response) -> dict:
    """MCP Streamable HTTP 响应可能是 JSON，也可能是 SSE 流。"""
    ctype = resp.headers.get("content-type", "")
    if resp.status_code == 401 or resp.status_code == 403:
        desc = ""
        try:
            body = resp.json()
            desc = body.get("error_description") or body.get("error") or ""
        except Exception:
            desc = resp.text[:200]
        raise MCPError(f"上游认证失败({resp.status_code}): {desc}", resp.status_code)
    if resp.status_code == 402:
        raise MCPError("上游返回 402：账号积分(credits)不足", 402)
    if resp.status_code == 429:
        raise MCPError("上游返回 429：请求过于频繁", 429, transient=True)
    if resp.status_code >= 500:
        raise MCPError(f"上游服务错误({resp.status_code})", resp.status_code, transient=True)
    if resp.status_code >= 400:
        try:
            body = resp.json()
            msg = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("error")
        except Exception:
            msg = resp.text[:200]
        raise MCPError(f"上游请求错误({resp.status_code}): {msg or resp.text[:120]}", resp.status_code)

    if "text/event-stream" in ctype:
        last_json = None
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                    last_json = obj
        if last_json is None:
            raise MCPError("SSE 响应中未找到 JSON-RPC 结果", 0, transient=True)
        obj = last_json
    else:
        try:
            obj = resp.json()
        except Exception:
            raise MCPError(f"无法解析上游响应: {resp.text[:120]}", 0, transient=True)

    if isinstance(obj, dict) and obj.get("error"):
        err = obj["error"]
        msg = err.get("message", err) if isinstance(err, dict) else str(err)
        code = err.get("code", 0) if isinstance(err, dict) else 0
        raise MCPError(f"MCP 错误: {msg}", 0, transient=(code == -32001))
    return obj


class McpClient:
    """与单个账号关联的 MCP 会话客户端（无状态封装，会话信息由调用方传入/回传）。"""

    def __init__(self, access_token: str, session_id: str = "", protocol_version: str = "",
                 timeout: float = 60.0):
        cfg = get_config()
        self.endpoint = cfg.MCP_ENDPOINT
        self.access_token = access_token
        self.session_id = session_id
        self.protocol_version = protocol_version or "2025-03-26"
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "User-Agent": f"{cfg.CLIENT_NAME}/{cfg.CLIENT_VERSION}",
            },
        )

    async def aclose(self):
        await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    def _headers(self) -> dict:
        h = {}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _capture_session(self, resp: httpx.Response):
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid

    async def _post(self, payload: dict, timeout: float | None = None) -> httpx.Response:
        return await self.client.post(
            self.endpoint, json=payload, headers=self._headers(),
            timeout=timeout or self.client.timeout,
        )

    async def initialize(self) -> dict:
        """initialize + notifications/initialized。返回服务器信息。"""
        cfg = get_config()
        resp = await self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": cfg.CLIENT_NAME, "version": cfg.CLIENT_VERSION},
            },
        })
        self._capture_session(resp)
        obj = _parse_response(resp)
        result = obj.get("result") or {}
        # 协议版本协商：采用服务器返回的版本
        if result.get("protocolVersion"):
            self.protocol_version = result["protocolVersion"]
        # 发送 initialized 通知（202 无内容，忽略解析失败）
        try:
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except MCPError:
            pass
        return result

    async def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        while True:
            params: dict = {}
            if cursor:
                params["cursor"] = cursor
            resp = await self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": params})
            obj = _parse_response(resp)
            result = obj.get("result") or {}
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict, timeout: float = 600.0) -> dict:
        """tools/call。返回 {"content": [...], "isError": bool, "raw": 原始 result}。"""
        resp = await self._post({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }, timeout=timeout)
        obj = _parse_response(resp)
        result = obj.get("result") or {}
        return result

    async def ping(self) -> bool:
        try:
            resp = await self._post({"jsonrpc": "2.0", "id": 9, "method": "ping"})
            _parse_response(resp)
            return True
        except MCPError as e:
            if e.status == 404:
                # 会话失效：丢弃会话后由上层重建
                self.session_id = ""
                return True
            raise


def extract_text(result: dict) -> str:
    """把 tools/call 结果里的 text 内容拼接为字符串。"""
    parts = []
    for item in result.get("content") or []:
        if isinstance(item, dict):
            if item.get("type") == "text" and item.get("text"):
                parts.append(item["text"])
            elif item.get("type") == "resource":
                res = item.get("resource") or {}
                if isinstance(res.get("text"), str):
                    parts.append(res["text"])
    return "\n".join(parts)


_URL_RE = re.compile(r"https?://[^\s\"'<>\\\)\]]+")


def extract_urls(text: str) -> list[str]:
    if not text:
        return []
    return _URL_RE.findall(text)


def pick_result_url(result: dict) -> str:
    """从结果文本中挑选最可能的成果 URL（优先包含图片/视频/文件扩展名或常见 CDN 特征）。"""
    urls = extract_urls(extract_text(result))
    if not urls:
        return ""
    keywords = (".mp4", ".webm", ".png", ".jpg", ".jpeg", ".webp", ".gif", "video", "image",
                "download", "cdn", "storage", "media", "result", "output")
    scored = sorted(urls, key=lambda u: -sum(k in u.lower() for k in keywords))
    best = scored[0]
    return best.rstrip(".,;:)")
