"""Creative Fabrica MCP 客户端（实测协议：Cookie 或 Bearer 鉴权，无状态调用）。

实测结论（2026-09）：
- POST https://mcp.creativefabrica.com/mcp，Accept: application/json, text/event-stream
- Cookie 账号：直接带浏览器导出的 Cookie + 原 UA 即可通过
- 服务端不返回 Mcp-Session-Id（无状态）；带会话头的逻辑保留以兼容未来变化
- 工具：list_models / get_model / generate / get_generation / list_voices / ...
- generate 返回 structuredContent.generationId；get_generation 轮询 structuredContent.status
  终态：completed / succeeded / failed / error / cancelled
- 失败：HTTP 4xx/5xx 或 result.isError=true + content[].text 错误信息
"""
import asyncio
import json
import time

import httpx

from .config import get_config

_last_request: dict[str, float] = {}
_locks: dict[str, asyncio.Lock] = {}


def _account_lock(key: str) -> asyncio.Lock:
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


class CFError(Exception):
    """上游错误。kind: auth / billing / rate_limited / upstream / invalid / network"""

    def __init__(self, message: str, kind: str = "upstream", status: int = 0,
                 retry_after: int = 0):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after

    @property
    def transient(self) -> bool:
        return self.kind in ("rate_limited", "upstream", "network")


BILLING_WORDS = ("subscription", "insufficient", "coin", "credit", "balance", "payment",
                 "billing", "积分", "余额", "订阅")


def classify_error_text(text: str) -> str:
    low = (text or "").lower()
    if any(w in low for w in BILLING_WORDS):
        return "billing"
    return "upstream"


def parse_cookie_export(raw: str) -> tuple[str, str]:
    """解析 Cookie 导出内容：浏览器插件 JSON 数组或 'k=v; k2=v2' 头字符串。

    返回 (cookie_header, user_agent)。_user_agent 伪 Cookie 转为 UA 使用。
    """
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    ua = ""
    jar: dict[str, str] = {}
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except ValueError:
            raise ValueError("Cookie JSON 解析失败")
        if not isinstance(data, list):
            raise ValueError("Cookie JSON 应为数组（浏览器插件导出格式）")
        for c in data:
            if not isinstance(c, dict):
                continue
            name, val, dom = c.get("name"), c.get("value"), c.get("domain", "")
            if not name or val is None:
                continue
            if "creativefabrica.com" not in (dom or "").lower():
                continue
            if name == "_user_agent":
                ua = val
                continue
            jar[name] = val
    else:
        for pair in raw.split(";"):
            if "=" not in pair:
                continue
            k, v = pair.strip().split("=", 1)
            jar[k.strip()] = v.strip()
    if not jar:
        raise ValueError("未找到 creativefabrica.com 的有效 Cookie")
    return "; ".join(f"{k}={v}" for k, v in jar.items()), ua


class CFClient:
    """与单个账号绑定的客户端。auth: ('cookie', header, ua) 或 ('bearer', token)。"""

    def __init__(self, account_key: str, auth: tuple, timeout: float = 120.0):
        cfg = get_config()
        self.key = account_key
        self.endpoint = cfg.MCP_ENDPOINT
        self.timeout = timeout
        kind, cred, ua = auth
        headers = {
            "Accept": "application/json, text/event-stream",
            "User-Agent": ua or "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0",
        }
        if kind == "cookie":
            headers["Cookie"] = cred
        else:
            headers["Authorization"] = f"Bearer {cred}"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=20.0), headers=headers)
        self.session_id = ""
        self._id = 0

    async def aclose(self):
        await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def _post(self, payload: dict, timeout: float | None = None) -> httpx.Response:
        # 每账号请求间隔保护
        gap_seconds = get_config().REQUEST_GAP
        async with _account_lock(self.key):
            gap = time.time() - _last_request.get(self.key, 0)
            if gap < gap_seconds:
                await asyncio.sleep(gap_seconds - gap)
            _last_request[self.key] = time.time()
            headers = {"Mcp-Session-Id": self.session_id} if self.session_id else {}
            return await self.client.post(self.endpoint, json=payload, headers=headers,
                                          timeout=timeout or self.timeout)

    def _capture_session(self, resp: httpx.Response):
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid

    def _parse(self, resp: httpx.Response, rid: int) -> dict:
        if resp.status_code == 429:
            ra = resp.headers.get("retry-after") or 0
            raise CFError("上游 429 限流", "rate_limited", 429, int(ra) if str(ra).isdigit() else 60)
        if resp.status_code in (401, 403):
            raise CFError(f"鉴权失败({resp.status_code})，Cookie/令牌可能已过期", "auth", resp.status_code)
        if resp.status_code == 402:
            raise CFError("上游 402：账号积分不足", "billing", 402)
        if resp.status_code >= 500:
            raise CFError(f"上游服务错误({resp.status_code})", "upstream", resp.status_code)
        if resp.status_code >= 400:
            raise CFError(f"上游请求错误({resp.status_code}): {resp.text[:200]}", "invalid", resp.status_code)
        ctype = resp.headers.get("content-type", "")
        obj = None
        if "text/event-stream" in ctype:
            for line in resp.text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    cand = json.loads(chunk)
                except ValueError:
                    continue
                # 优先取与请求 id 匹配的响应；否则记住最后一个带 result/error 的
                if isinstance(cand, dict):
                    if cand.get("id") == rid:
                        obj = cand
                        break
                    if "result" in cand or "error" in cand:
                        obj = obj or cand
            if obj is None:
                raise CFError("SSE 响应中未找到结果", "upstream")
        else:
            try:
                obj = resp.json()
            except Exception:
                raise CFError(f"无法解析响应: {resp.text[:120]}", "upstream")
        if isinstance(obj, dict) and obj.get("error"):
            err = obj["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise CFError(f"MCP 错误: {msg}", classify_error_text(msg))
        return obj or {}

    async def rpc(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        self._id += 1
        rid = self._id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        resp = await self._post(payload, timeout)
        self._capture_session(resp)
        return self._parse(resp, rid).get("result") or {}

    # ---------------- 协议方法 ----------------

    async def initialize(self) -> dict:
        cfg = get_config()
        result = await self.rpc("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": cfg.CLIENT_NAME, "version": cfg.CLIENT_VERSION},
        })
        # initialized 通知（失败不影响主流程）
        try:
            async with _account_lock(self.key):
                _last_request[self.key] = 0  # 通知不占用间隔
            await self.client.post(self.endpoint, headers=(
                {"Mcp-Session-Id": self.session_id} if self.session_id else {}),
                json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            pass
        return result

    async def list_tools(self) -> list[dict]:
        return (await self.rpc("tools/list")).get("tools") or []

    async def list_models(self, modality: str = "") -> list[dict]:
        params = {"modality": modality} if modality else {}
        result = await self.rpc("tools/call", {"name": "list_models", "arguments": params})
        return self._models_from_result(result)

    async def get_model(self, model: str) -> dict | None:
        result = await self.rpc("tools/call", {"name": "get_model", "arguments": {"model": model}})
        return self._json_from_result(result)

    async def generate(self, model: str, modality: str, prompt: str,
                       options: dict | None = None,
                       files: list[dict] | None = None) -> dict:
        """提交生成。返回 result（含 structuredContent）。isError 时抛 CFError(billing/...)。"""
        args = {"model": model, "modality": modality, "prompt": prompt,
                "options": options or {}}
        if files:
            args["files"] = files
        result = await self.rpc("tools/call", {"name": "generate", "arguments": args},
                                timeout=max(self.timeout, 180.0))
        self._raise_if_error(result, "generate")
        return result

    async def get_generation(self, generation_id: str, limit: int = 1) -> dict:
        result = await self.rpc("tools/call", {
            "name": "get_generation", "arguments": {"generationId": generation_id, "limit": limit}})
        self._raise_if_error(result, "get_generation")
        return result

    # ---------------- 结果解析 ----------------

    @staticmethod
    def _raise_if_error(result: dict, where: str):
        if result.get("isError"):
            text = ""
            for item in result.get("content") or []:
                if isinstance(item, dict) and item.get("text"):
                    text = item["text"]
                    break
            kind = classify_error_text(text)
            msg = f"{where} 失败: {text[:300]}"
            if kind == "billing" and "payment required" in text.lower():
                msg += ("（该模型按分辨率/时长动态计费，账号金币不足以支付本次费用："
                        "可降低 resolution/duration，或给账号充值/换有余额的账号）")
            raise CFError(msg, kind)

    @staticmethod
    def structured(result: dict) -> dict:
        sc = result.get("structuredContent")
        return sc if isinstance(sc, dict) else {}

    @staticmethod
    def _models_from_result(result: dict) -> list[dict]:
        payload = CFClient._json_from_result(result)
        models = payload.get("models") if isinstance(payload, dict) else None
        return models or []

    @staticmethod
    def _json_from_result(result: dict):
        """content[0].text 通常是 JSON 字符串。"""
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                try:
                    return json.loads(item["text"])
                except ValueError:
                    continue
        return None


TERMINAL_OK = ("completed", "succeeded")
TERMINAL_FAIL = ("failed", "error", "cancelled")


def walk_urls(obj, out: list | None = None) -> list[str]:
    """递归收集结果里的 URL。"""
    if out is None:
        out = []
    if isinstance(obj, str):
        if obj.startswith("http://") or obj.startswith("https://"):
            out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            walk_urls(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_urls(v, out)
    return out


def pick_result_url(result: dict) -> str:
    """优先正式成片，正式地址不存在时才选择预览。"""
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    outputs = structured.get("outputs") if isinstance(structured, dict) else None

    if isinstance(outputs, list):
        for output in outputs:
            if not isinstance(output, dict):
                continue
            for key in ("url", "outputUrl", "downloadUrl"):
                value = output.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    return value.rstrip(".,;:)")

        for output in outputs:
            if not isinstance(output, dict):
                continue
            value = output.get("previewUrl")
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value.rstrip(".,;:)")

    urls = walk_urls(result)
    if not urls:
        return ""

    keywords = (
        ".mp4", ".webm", ".png", ".jpg", ".jpeg", ".webp",
        ".gif", ".mp3", ".wav", "download", "cdn", "storage",
        "media", "result", "output", "render"
    )
    preview_words = ("preview", "thumbnail", "thumb")

    scored = sorted(
        urls,
        key=lambda url: (
            -sum(word in url.lower() for word in keywords),
            sum(word in url.lower() for word in preview_words),
        ),
    )
    return scored[0].rstrip(".,;:)")


def shrink_for_storage(obj, limit: int = 60000):
    """递归截断超长字符串（内联 base64 图片等），保留 URL。"""
    if isinstance(obj, str):
        return obj if (len(obj) <= limit or obj.startswith("http")) else obj[:200] + f"...<截断{len(obj)}字符>"
    if isinstance(obj, dict):
        return {k: shrink_for_storage(v, limit) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shrink_for_storage(v, limit) for v in obj]
    return obj
