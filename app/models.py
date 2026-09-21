import json
import secrets
import time

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, Text

from .db import Base
from sqlalchemy.orm import Mapped, mapped_column


def now() -> float:
    return time.time()


class JSONText:
    """TEXT 存 JSON 的简易序列化助手。"""

    @staticmethod
    def dump(v) -> str:
        return json.dumps(v, ensure_ascii=False) if v is not None else None

    @staticmethod
    def load(s, default=None):
        if not s:
            return default
        try:
            return json.loads(s)
        except Exception:
            return default


class Account(Base):
    """Creative Fabrica 账号（OAuth 授权）。"""
    __tablename__ = "accounts"

    STATUS_PENDING = "pending_auth"      # 已创建，等待 OAuth 授权
    STATUS_ACTIVE = "active"
    STATUS_DISABLED = "disabled"
    STATUS_EXPIRED = "expired"           # 令牌失效且刷新失败
    STATUS_INSUFFICIENT = "insufficient_balance"
    STATUS_RATE_LIMITED = "rate_limited"
    STATUS_ERROR = "error"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, default=STATUS_PENDING)
    auth_type: Mapped[str] = mapped_column(Text, default="cookie")  # cookie / oauth
    # 上游渠道: creative-fabrica(MCP) / capcut(直连 common_task)
    provider: Mapped[str] = mapped_column(Text, default="creative-fabrica")

    # Cookie 模式（实测可行，主推）：完整 Cookie 头加密存储 + 导出时的浏览器 UA
    cookie_enc: Mapped[str] = mapped_column(Text, default="")
    user_agent: Mapped[str] = mapped_column(Text, default="")

    # CapCut 请求签名（JSON）：{"new":{"sign":..,"device_time":..,"tdid":..},...}
    # 签名算法已逆向（app/capcut_signs.py），默认按当前时间现签（CAPCUT_SIGN_MODE=auto）；
    # 这里存的是「该账号专用」的签名，用于需要与账号会话完全一致的场景（或算法变更时应急）。
    capcut_signs_text: Mapped[str] = mapped_column(Text, default="")

    # OAuth 客户端（动态注册得到）与令牌（加密存储，备用方案）
    client_id: Mapped[str] = mapped_column(Text, default="")
    client_secret_enc: Mapped[str] = mapped_column(Text, default="")
    access_token_enc: Mapped[str] = mapped_column(Text, default="")
    refresh_token_enc: Mapped[str] = mapped_column(Text, default="")
    token_expires_at: Mapped[float] = mapped_column(Float, default=0)
    scope: Mapped[str] = mapped_column(Text, default="")

    coin_balance: Mapped[float] = mapped_column(Float, default=0)   # 0 表示未知
    max_concurrency: Mapped[int] = mapped_column(Integer, default=2)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[float] = mapped_column(Float, default=0)
    last_check_at: Mapped[float] = mapped_column(Float, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")

    # MCP 会话缓存
    mcp_session_id: Mapped[str] = mapped_column(Text, default="")
    mcp_protocol_version: Mapped[str] = mapped_column(Text, default="")
    tools_synced_at: Mapped[float] = mapped_column(Float, default=0)

    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now, onupdate=now)

    @property
    def capcut_signs(self) -> dict:
        """该账号保存的 CapCut 签名（空 = 全部按算法现签）。"""
        return JSONText.load(self.capcut_signs_text, {}) or {}

    def to_dict(self, safe=True):
        d = {
            "id": self.id,
            "name": self.name,
            "provider": self.provider or "creative-fabrica",
            "status": self.status,
            "auth_type": self.auth_type,
            "has_token": bool(self.access_token_enc or self.cookie_enc),
            "cookie_updated": bool(self.cookie_enc),
            "token_expires_at": self.token_expires_at or None,
            "coin_balance": self.coin_balance or None,
            "max_concurrency": self.max_concurrency,
            "fail_count": self.fail_count,
            "last_used_at": self.last_used_at or None,
            "last_check_at": self.last_check_at or None,
            "last_error": self.last_error,
            "mcp_session": bool(self.mcp_session_id),
            "tools_synced_at": self.tools_synced_at or None,
            "created_at": self.created_at,
        }
        if self.provider == "capcut":
            from .capcut_signs import describe_signs
            rows = describe_signs(self.capcut_signs)
            d["signs"] = rows
            d["signs_saved"] = sorted(self.capcut_signs.keys())
            d["signs_overridden"] = [r["key"] for r in rows if r["source"] == "account"]
        return d


class ApiKey(Base):
    """对外的 sk-cf- 密钥（仅存哈希）。"""
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, default="")
    key_hash: Mapped[str] = mapped_column(Text, unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(Text, default="")   # 展示用前缀
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_limit: Mapped[int] = mapped_column(Integer, default=0)  # 0=不限
    # CapCut 渠道成片链接方式: official=官链(原样返回 CapCut CDN 链接)
    #                        r2=官转(把成片转存到 R2 桶后返回 R2 链接)
    capcut_link_mode: Mapped[str] = mapped_column(Text, default="official")
    enabled_models_text: Mapped[str] = mapped_column(Text, default="[]")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=now)
    last_used_at: Mapped[float] = mapped_column(Float, default=0)

    @property
    def enabled_models(self):
        return JSONText.load(self.enabled_models_text, [])

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "key_prefix": self.key_prefix,
            "enabled": self.enabled,
            "daily_limit": self.daily_limit,
            "enabled_models": self.enabled_models,
            "capcut_link_mode": self.capcut_link_mode or "official",
            "note": self.note,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at or None,
        }


class ModelEntry(Base):
    """对外模型 → MCP Tool 映射。"""
    __tablename__ = "models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id: Mapped[str] = mapped_column(Text, unique=True, index=True)  # 如 cf-seedance-v2
    display_name: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(Text, default="creative-fabrica")
    mcp_tool: Mapped[str] = mapped_column(Text, default="")
    mtype: Mapped[str] = mapped_column(Text, default="other")  # image / video / other
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=600)
    param_template_text: Mapped[str] = mapped_column(Text, default="")  # JSON: 附加固定参数
    auto_registered: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(Text, default="")
    input_schema_text: Mapped[str] = mapped_column(Text, default="")    # 发现时保存的 inputSchema
    # CF 模型目录原始信息（displayName/modalities/pricing/options 等）
    catalog_text: Mapped[str] = mapped_column(Text, default="")
    # 参考素材上限 JSON: {"image":30,"video":10,"audio":10,"total":50}；空=按官方目录推断
    ref_limits_text: Mapped[str] = mapped_column(Text, default="")
    # 生成能力上限 JSON（按模型放开分辨率/时长）:
    # {"resolutions":[480,720,1080],"durations":[5,8,...,30],"min_duration":2,"max_duration":30}
    # 空=用内置实测覆写（如 Seedance 2.5）/内置默认（480/720p、2-15s）
    gen_limits_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=now)
    updated_at: Mapped[float] = mapped_column(Float, default=now, onupdate=now)

    @property
    def param_template(self):
        return JSONText.load(self.param_template_text, {}) or {}

    @property
    def catalog(self):
        return JSONText.load(self.catalog_text, {}) or {}

    @property
    def ref_limits(self):
        return JSONText.load(self.ref_limits_text, {}) or {}

    @property
    def gen_limits(self):
        return JSONText.load(self.gen_limits_text, {}) or {}

    @property
    def catalog_caps(self):
        """官方目录声明的分辨率/时长（仅作后台配置参考，不等同于网关放开值）。"""
        cat = self.catalog
        res = cat.get("resolutions") or []
        if res and isinstance(res[0], dict):
            res = [r.get("resolution") for r in res if isinstance(r, dict)]
        return {
            "resolutions": [str(r) for r in res if r],
            "durations": cat.get("durations") or [],
        }

    def to_dict(self):
        cat = self.catalog
        return {
            "id": self.id,
            "model_id": self.model_id,
            "display_name": self.display_name or cat.get("displayName") or self.model_id,
            "provider": self.provider,
            "mcp_tool": self.mcp_tool,
            "type": self.mtype,
            "enabled": self.enabled,
            "estimated_cost": self.estimated_cost,
            "timeout_seconds": self.timeout_seconds,
            "param_template": self.param_template,
            "ref_limits": self.ref_limits,
            "gen_limits": self.gen_limits,
            "catalog_caps": self.catalog_caps,
            "auto_registered": self.auto_registered,
            "description": self.description,
            "input_schema": JSONText.load(self.input_schema_text),
            "modalities": cat.get("modalities") or [],
            "pricing": cat.get("pricing"),
            "max_prompt_length": cat.get("maxPromptLength"),
            "created_at": self.created_at,
        }


class Generation(Base):
    """异步生成任务。"""
    __tablename__ = "generations"

    STATUS_QUEUED = "queued"        # 等待提交
    STATUS_PROCESSING = "processing"  # 提交中
    STATUS_POLLING = "polling"      # 已提交 CF，轮询 get_generation
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gen_id: Mapped[str] = mapped_column(Text, unique=True, index=True)  # gen_xxx
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"), nullable=True)
    key_name: Mapped[str] = mapped_column(Text, default="")
    model_id: Mapped[str] = mapped_column(Text, default="")
    mcp_tool: Mapped[str] = mapped_column(Text, default="")
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    account_name: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default=STATUS_QUEUED, index=True)
    prompt: Mapped[str] = mapped_column(Text, default="")
    params_text: Mapped[str] = mapped_column(Text, default="{}")
    result_text: Mapped[str] = mapped_column(Text, default="")
    result_url: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0)
    # CF 侧生成任务（提交后回填；轮询/断点续查的键）
    cf_generation_id: Mapped[str] = mapped_column(Text, default="", index=True)
    cf_status: Mapped[str] = mapped_column(Text, default="")
    # CapCut 直连通道: query token 与提交前积分快照（算实际消耗用）
    upstream_token: Mapped[str] = mapped_column(Text, default="")
    credit_before: Mapped[float] = mapped_column(Float, default=0)
    # 提交时从密钥快照的 CapCut 链接方式: official(官链) / r2(官转)
    link_mode: Mapped[str] = mapped_column(Text, default="")
    next_poll_at: Mapped[float] = mapped_column(Float, default=0, index=True)
    created_at: Mapped[float] = mapped_column(Float, default=now, index=True)
    started_at: Mapped[float] = mapped_column(Float, default=0)
    finished_at: Mapped[float] = mapped_column(Float, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def params(self):
        return JSONText.load(self.params_text, {}) or {}

    def to_dict(self, include_result=True):
        d = {
            "id": self.gen_id,
            "object": "generation",
            "status": self.status,
            "model": self.model_id,
            "prompt": self.prompt,
            "key_name": self.key_name or None,
            "account": self.account_name or None,
            "error": self.error or None,
            "retry_count": self.retry_count,
            "cf_generation_id": self.cf_generation_id or None,
            "cf_status": self.cf_status or None,
            "created": int(self.created_at),
            "started": self.started_at or None,
            "finished": self.finished_at or None,
            "duration_ms": self.duration_ms or None,
            "url": self.result_url or None,
        }
        if include_result:
            d["result"] = JSONText.load(self.result_text)
            d["params"] = self.params
        return d


class CoinTransaction(Base):
    __tablename__ = "coin_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 账号删除后保留流水快照（account_name），关联置空。
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    account_name: Mapped[str] = mapped_column(Text, default="")
    generation_id: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(Text, default="manual")  # manual / auto / adjust
    before_balance: Mapped[float] = mapped_column(Float, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0)
    after_balance: Mapped[float] = mapped_column(Float, default=0)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=now)

    def to_dict(self):
        return {
            "id": self.id,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "generation_id": self.generation_id or None,
            "kind": self.kind,
            "before_balance": self.before_balance,
            "cost": self.cost,
            "after_balance": self.after_balance,
            "note": self.note,
            "created_at": self.created_at,
        }


class SystemLog(Base):
    __tablename__ = "system_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(Text, default="info")  # info / warn / error
    event: Mapped[str] = mapped_column(Text, default="", index=True)
    request_id: Mapped[str] = mapped_column(Text, default="")
    account_id: Mapped[int] = mapped_column(Integer, default=0)
    key_prefix: Mapped[str] = mapped_column(Text, default="")
    detail_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=now, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "level": self.level,
            "event": self.event,
            "request_id": self.request_id,
            "account_id": self.account_id or None,
            "key_prefix": self.key_prefix,
            "detail": JSONText.load(self.detail_text),
            "created_at": self.created_at,
        }


class OAuthFlow(Base):
    """进行中的 OAuth 授权流程（state → 账号绑定）。"""
    __tablename__ = "oauth_flows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    state: Mapped[str] = mapped_column(Text, unique=True, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    code_verifier: Mapped[str] = mapped_column(Text)
    redirect_uri: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending")  # pending / done / error
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=now)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value_text: Mapped[str] = mapped_column(Text, default="")


def new_gen_id() -> str:
    return "gen_" + secrets.token_hex(8)


def log_event(db, level: str, event: str, detail=None, request_id: str = "",
              account_id: int = 0, key_prefix: str = ""):
    """写入系统日志（写库前对敏感字段脱敏由调用方保证：只传掩码值）。"""
    db.add(SystemLog(
        level=level, event=event, request_id=request_id[:64],
        account_id=account_id or 0, key_prefix=key_prefix[:24],
        detail_text=JSONText.dump(detail if detail is not None else {}),
    ))
    db.commit()


Index("ix_generations_status_created", Generation.status, Generation.created_at)
