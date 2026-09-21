"""核心服务层：账号（Cookie 主推 / OAuth 备用）、模型目录同步、参数转换、任务执行。"""
import re
import time
import urllib.parse as _up

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import oauth, security
from .cf_client import (CFClient, CFError, TERMINAL_FAIL, TERMINAL_OK,
                        classify_error_text, parse_cookie_export, pick_result_url,
                        shrink_for_storage, walk_urls)
from .config import get_config
from .models import Account, CoinTransaction, Generation, ModelEntry, JSONText, now

ACCOUNT_STATUS_BY_KIND = {
    "auth": Account.STATUS_EXPIRED,
    "billing": Account.STATUS_INSUFFICIENT,
    "rate_limited": Account.STATUS_RATE_LIMITED,
}


# ---------------- 客户端构造 ----------------

def account_auth(db: Session, account: Account) -> tuple:
    """返回 CFClient 可用的鉴权三元组。"""
    if account.auth_type == "oauth":
        token = security.decrypt(account.access_token_enc)
        if not token:
            raise CFError("账号尚未完成 OAuth 授权", "auth", 401)
        return ("bearer", token, "")
    cookie = security.decrypt(account.cookie_enc)
    if not cookie:
        raise CFError("账号尚未导入 Cookie", "auth", 401)
    return ("cookie", cookie, account.user_agent or "")


def client_for(db: Session, account: Account, timeout: float = 120.0) -> CFClient:
    return CFClient(f"acc-{account.id}", account_auth(db, account), timeout)


async def ensure_access_token(db: Session, account: Account) -> None:
    """OAuth 账号令牌保障（Cookie 账号无操作）。"""
    if account.auth_type != "oauth":
        return
    token = security.decrypt(account.access_token_enc)
    if token and account.token_expires_at and account.token_expires_at - time.time() > 60:
        return
    await refresh_account_token(db, account)


async def refresh_account_token(db: Session, account: Account) -> None:
    if account.auth_type != "oauth":
        raise CFError("Cookie 账号无需刷新令牌，请重新导出 Cookie", "invalid")
    refresh_tok = security.decrypt(account.refresh_token_enc)
    if not refresh_tok:
        account.status = Account.STATUS_EXPIRED
        account.last_error = "无 refresh_token，请重新授权"
        db.commit()
        raise CFError("账号无 refresh_token，请重新授权", "auth", 401)
    try:
        data = await oauth.refresh_token(
            refresh_tok, account.client_id, security.decrypt(account.client_secret_enc))
    except oauth.OAuthError as e:
        account.status = Account.STATUS_EXPIRED
        account.last_error = f"令牌刷新失败: {e}"
        db.commit()
        raise CFError(str(e), "auth", 401) from e
    account.access_token_enc = security.encrypt(data.get("access_token", ""))
    if data.get("refresh_token"):
        account.refresh_token_enc = security.encrypt(data["refresh_token"])
    account.token_expires_at = time.time() + int(data.get("expires_in", 3600))
    account.scope = data.get("scope", account.scope)
    account.last_error = ""
    if account.status in (Account.STATUS_EXPIRED, Account.STATUS_ERROR,
                          Account.STATUS_RATE_LIMITED):
        account.status = Account.STATUS_ACTIVE
    db.commit()


def import_cookie(db: Session, account: Account, raw_cookie: str) -> None:
    """解析并加密保存 Cookie 导入内容（按账号 provider 分流）。"""
    if (account.provider or "") == "capcut":
        from .capcut_channel import normalize_capcut_cookie
        account.cookie_enc = security.encrypt(normalize_capcut_cookie(raw_cookie))
        account.auth_type = "cookie"
        account.last_error = ""
        db.commit()
        return
    header, ua = parse_cookie_export(raw_cookie)
    account.cookie_enc = security.encrypt(header)
    if ua:
        account.user_agent = ua
    account.auth_type = "cookie"
    account.last_error = ""
    db.commit()


def account_fingerprint(account: Account) -> str:
    """账号凭据身份指纹（去重用），按 provider 分流。"""
    if (account.provider or "") == "capcut":
        from .capcut_channel import capcut_cookie_identity
        return capcut_cookie_identity(security.decrypt(account.cookie_enc))
    return cookie_identity(security.decrypt(account.cookie_enc))


# ---------------- 批量导入 / 验活 ----------------

def cookie_identity(cookie_header: str) -> str:
    """从 Cookie 头提取账号身份标识（cfauth_uid 或 wordpress 登录名），用于命名与去重。"""
    jar = _cookie_jar(cookie_header)
    uid = jar.get("cfauth_uid")
    if uid:
        return uid
    for k, v in jar.items():
        if k.startswith("wordpress_logged_in_") and v:
            try:
                return _up.unquote(v).split("|")[0].strip()
            except Exception:
                return ""
    return ""


def _cookie_jar(cookie_header: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    for pair in (cookie_header or "").split(";"):
        if "=" in pair:
            k, v = pair.strip().split("=", 1)
            jar[k.strip()] = v.strip()
    return jar


def cookie_subscription(cookie_header: str) -> str | None:
    """读取 Cookie 缓存的 Studio AI 订阅状态（cfsub_studio_ai_status）。

    返回原始值（如 "trial:active:yearly" / "not_subscribed"）；Cookie 未携带该值时返回 None（无法判断）。
    """
    return _cookie_jar(cookie_header).get("cfsub_studio_ai_status")


def subscription_active(sub_status: str | None) -> bool | None:
    """订阅是否生效。True=生效；False=明确未生效（含空值）；None=Cookie 未携带该键，无法判断。"""
    if sub_status is None:
        return None
    return "active" in (sub_status or "").lower()


def derive_account_name(cookie_header: str) -> str:
    ident = cookie_identity(cookie_header)
    if not ident:
        return ""
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", ident)[:48]


async def probe_cookie(cookie_header: str, ua: str, deep: bool = True) -> dict:
    """验活：initialize + list_models；deep=True 时再用最便宜的标准定价模型真实生成一次。

    官方规则：失败的生成会退款，因此验活失败不产生费用；只有账号真正可用时才消耗
    最低价模型的费用。返回 {ok, detail, models, deep}。
    """
    ident = cookie_identity(cookie_header) or "anon"
    try:
        client = CFClient(f"probe-{ident}", ("cookie", cookie_header, ua))
        try:
            await client.initialize()
            models = await client.list_models()
            result = {"ok": True, "detail": f"验活通过，{len(models)} 个模型可用", "models": len(models)}
            if deep:
                deep_info = await deep_generate_check(client, models)
                result["deep"] = deep_info
                result["detail"] = deep_info["detail"] if deep_info["ok"] else deep_info["detail"]
                if not deep_info["ok"]:
                    result["ok"] = False
            return result
        finally:
            await client.aclose()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)[:200], "models": 0}


def cheapest_model(models: list[dict]) -> dict | None:
    """目录中 coinAmount 最低的标准定价模型（用于最低消耗的真实生成验证）。"""
    cands = []
    for m in models:
        p = m.get("pricing") or {}
        amt = p.get("coinAmount")
        if p.get("type") == "standard" and isinstance(amt, (int, float)) and amt > 0:
            cands.append((amt, m))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1]


async def deep_generate_check(client: CFClient, models: list[dict], timeout: float = 150.0) -> dict:
    """真实生成验证：用最便宜的标准定价模型提交一次生成并轮询到终态。"""
    import asyncio
    model = cheapest_model(models)
    if not model:
        return {"ok": True, "detail": "目录中无标准定价模型，跳过真实生成验证", "cost": 0}
    name = model.get("name")
    price = int(model.get("pricing", {}).get("coinAmount") or 0)
    mods = model.get("modalities") or ["textToImage"]
    modality = next((m for m in mods if str(m).startswith("text")), mods[0])
    try:
        result = await client.generate(name, str(modality), "liveness check", {})
        sc = client.structured(result)
        gid = str(sc.get("generationId") or "")
        if not gid:
            return {"ok": False, "detail": f"真实生成未返回任务号（{name}）", "cost": 0}
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(min(8, max(3, int(sc.get("retryAfterSeconds") or 4))))
            r2 = await client.get_generation(gid)
            sc = client.structured(r2)
            st = str(sc.get("status") or "")
            if st in TERMINAL_OK:
                url = pick_result_url(r2) or str(sc.get("outputUrl") or "")
                return {"ok": True, "cost": price, "model": name, "generation_id": gid,
                        "url": url[:200],
                        "detail": f"真实生成成功（{name}，{price} 币）：模型计费链路可用"}
            if st in TERMINAL_FAIL:
                return {"ok": False, "cost": 0, "model": name, "generation_id": gid,
                        "detail": f"真实生成失败（{name}，{st}）：失败生成按官方规则退款"}
        return {"ok": False, "cost": 0, "model": name, "generation_id": gid,
                "detail": f"真实生成超时（{name}，>{int(timeout)}s）"}
    except CFError as e:
        return {"ok": False, "cost": 0, "model": name,
                "detail": f"真实生成失败（{name}，{price} 币档）：{e}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "cost": 0, "model": name, "detail": f"真实生成异常（{name}）：{e}"}


# ---------------- 账号链路测试 ----------------

async def test_account(db: Session, account: Account, deep: bool = True) -> dict:
    """链路测试：鉴权 → initialize → list_models → 订阅检查 →（deep）最低消耗真实生成。"""
    if (account.provider or "") == "capcut":
        return await _test_capcut_account(db, account)
    result: dict = {"steps": []}

    def step(ok, name, detail=""):
        result["steps"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})
        return bool(ok)

    try:
        await ensure_access_token(db, account)
        step(True, "authentication", f"{account.auth_type} 凭据存在")
        client = client_for(db, account)
        try:
            info = await client.initialize()
            step(True, "mcp_initialize",
                 f"server={(info.get('serverInfo') or {}).get('name', '')} "
                 f"{(info.get('serverInfo') or {}).get('version', '')}")
            models = await client.list_models()
            step(True, "list_models", f"可用模型 {len(models)} 个")
            result["models"] = [m.get("name") for m in models][:60]

            # 订阅检查：Cookie 明确显示无 Studio AI 订阅的账号不进入调度池
            sub = None
            if account.auth_type == "cookie":
                sub = cookie_subscription(security.decrypt(account.cookie_enc))
            result["subscription"] = sub
            if sub is not None and not subscription_active(sub):
                step(False, "subscription",
                     f"Studio AI 订阅未生效（{sub}），该账号不会参与生成调度")
                if account.status != Account.STATUS_DISABLED:
                    account.status = Account.STATUS_INSUFFICIENT
                account.last_error = f"Studio AI 订阅未生效（cfsub_studio_ai_status={sub})"
                db.commit()
                result["ok"] = False
                return result
            step(True, "subscription", sub or "Cookie 未携带订阅状态（按有效处理）")

            # 深度验活：最便宜的标准定价模型真实生成一次（失败生成按官方规则退款）
            if deep:
                deep_info = await deep_generate_check(client, models)
                result["deep"] = deep_info
                step(deep_info["ok"], "generate_check", deep_info["detail"])
                if not deep_info["ok"]:
                    if account.status != Account.STATUS_DISABLED:
                        account.status = Account.STATUS_INSUFFICIENT
                    account.last_error = deep_info["detail"][:800]
                    account.last_check_at = now()
                    db.commit()
                    result["ok"] = False
                    return result
        finally:
            await client.aclose()
        if account.status != Account.STATUS_DISABLED:
            account.status = Account.STATUS_ACTIVE
        account.last_check_at = now()
        account.last_error = ""
        db.commit()
        result["ok"] = True
        return result
    except Exception as e:  # noqa: BLE001
        kind = getattr(e, "kind", "")
        step(False, "connection", e)
        account.last_error = str(e)[:300]
        account.last_check_at = now()
        if kind == "auth":
            account.status = Account.STATUS_EXPIRED
        elif account.status not in (Account.STATUS_DISABLED,):
            account.status = Account.STATUS_ERROR
        db.commit()
        result["ok"] = False
        return result


# ---------------- CapCut 账号链路测试 ----------------

def capcut_kind(text: str) -> str:
    from .capcut_channel import classify_capcut_error
    return classify_capcut_error(text)


async def _test_capcut_account(db: Session, account: Account) -> dict:
    """CapCut 账号链路测试：Cookie 解密 -> user_credit 验活并刷新余额。"""
    import asyncio
    from .capcut_channel import refresh_balance
    result: dict = {"steps": []}

    def step(ok, name, detail=""):
        result["steps"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})
        return bool(ok)

    if not account.cookie_enc:
        step(False, "authentication", "尚未导入 CapCut Cookie")
        account.status = Account.STATUS_ERROR
        account.last_check_at = now()
        db.commit()
        result["ok"] = False
        return result
    step(True, "authentication", "capcut cookie 已导入")
    try:
        total = await asyncio.to_thread(refresh_balance, account)
        step(True, "user_credit", f"积分余额 {total:g}")
        if account.status != Account.STATUS_DISABLED:
            account.status = Account.STATUS_ACTIVE
        account.last_check_at = now()
        account.last_error = ""
        db.commit()
        result["ok"] = True
        result["balance"] = total
        return result
    except Exception as e:  # noqa: BLE001
        kind = capcut_kind(str(e))
        step(False, "user_credit", e)
        account.last_error = str(e)[:300]
        account.last_check_at = now()
        if kind == "auth":
            account.status = Account.STATUS_EXPIRED
        elif account.status != Account.STATUS_DISABLED:
            account.status = Account.STATUS_ERROR
        db.commit()
        result["ok"] = False
        return result


# ---------------- 模型目录同步（list_models） ----------------

def model_type_from(modalities: list) -> str:
    mods = " ".join(modalities or []).lower()
    if "video" in mods:
        return "video"
    if "image" in mods:
        return "image"
    if "audio" in mods:
        return "audio"
    return "other"


async def sync_account_models(db: Session, account: Account) -> dict:
    """从 list_models 同步模型目录到 ModelEntry（保留已启用状态与手工配置）。"""
    client = client_for(db, account)
    try:
        await client.initialize()
        models = await client.list_models()
    finally:
        await client.aclose()
    imported, updated, disabled_gone = 0, 0, 0
    seen = set()
    for m in models:
        name = m.get("name")
        if not name:
            continue
        seen.add(name)
        pricing = m.get("pricing") or {}
        est = float(pricing.get("coinAmount") or 0)
        entry = db.execute(
            select(ModelEntry).where(ModelEntry.model_id == name)).scalar_one_or_none()
        cat = {k: m.get(k) for k in ("displayName", "modalities", "pricing",
                                     "maxPromptLength", "provider", "voiceCatalog") if k in m}
        if entry:
            entry.catalog_text = JSONText.dump(cat)
            entry.description = (m.get("displayName") or "")[:500]
            mt = model_type_from(m.get("modalities"))
            if not entry.auto_registered or True:
                # 类型随目录刷新，但保留手工编辑过的超时/成本/参数模板
                entry.mtype = mt
            if entry.estimated_cost in (0, None) and est:
                entry.estimated_cost = est
            entry.auto_registered = True
            updated += 1
        else:
            db.add(ModelEntry(
                model_id=name,
                display_name=m.get("displayName") or name,
                mcp_tool="generate",
                mtype=model_type_from(m.get("modalities")),
                enabled=False,
                estimated_cost=est,
                auto_registered=True,
                description=(m.get("displayName") or "")[:500],
                catalog_text=JSONText.dump(cat),
            ))
            imported += 1
    # 目录中已消失的模型：停用（不删除，保留历史任务引用）
    for entry in db.execute(select(ModelEntry).where(ModelEntry.auto_registered == True)).scalars().all():
        if entry.model_id not in seen and entry.enabled:
            entry.enabled = False
            disabled_gone += 1
    account.tools_synced_at = now()
    account.last_error = ""
    db.commit()
    return {"imported": imported, "updated": updated, "models": len(models),
            "disabled_gone": disabled_gone,
            "model_names": [m.get("name") for m in models]}


# ---------------- 账号选择 ----------------

def select_account(db: Session, model: ModelEntry | None, exclude_id: int = 0) -> Account | None:
    q = select(Account).where(Account.status == Account.STATUS_ACTIVE)
    accounts = db.execute(q).scalars().all()
    if exclude_id:
        accounts = [a for a in accounts if a.id != exclude_id]
    if not accounts:
        return None
    running = dict(db.execute(
        select(Generation.account_id, func.count(Generation.id)).where(
            Generation.status.in_([Generation.STATUS_PROCESSING, Generation.STATUS_POLLING]),
            Generation.account_id.isnot(None))
        .group_by(Generation.account_id)).all())
    est_cost = (model.estimated_cost if model else 0) or 0
    candidates = []
    for a in accounts:
        if running.get(a.id, 0) >= max(1, a.max_concurrency):
            continue
        if est_cost > 0 and (a.provider or "") == "capcut":
            # 缓存余额可能严重过期（虚高会把任务分给实际没钱的号）。
            # TTL 内不重复查；过期就懒刷新一次，失败则按缓存值保守处理。
            stale = (time.time() - (a.last_check_at or 0)) > 600
            refreshed = False
            if stale:
                try:
                    from .capcut_channel import refresh_balance
                    refresh_balance(a)
                    db.commit()
                    refreshed = True
                except Exception:  # noqa: BLE001
                    db.rollback()
            if refreshed or (a.coin_balance or 0) > 0:
                # 刷新成功（余额可信）或缓存为正数：严格按余额过滤
                if (a.coin_balance or 0) < est_cost:
                    continue
            # 缓存为 0 且刷新失败：视为未知，放行（保持旧行为）
        elif est_cost > 0 and (a.coin_balance or 0) > 0 and a.coin_balance < est_cost:
            continue
        score = running.get(a.id, 0) * 10 + (a.fail_count or 0) * 3 + (a.last_used_at or 0) / 1e10
        candidates.append((score, a))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


# ---------------- 参数构造（generate 调用） ----------------

def resolve_modality(model: ModelEntry, params: dict) -> str:
    if params.get("modality"):
        return str(params["modality"])
    tpl = model.param_template
    if tpl.get("modality"):
        return str(tpl["modality"])
    mods = model.catalog.get("modalities") or []
    for m in mods:
        if str(m).startswith("text"):
            return str(m)
    return str(mods[0]) if mods else "textToImage"


_REF_ROLES = {
    "image": ("referenceImage", "img"),
    "video": ("referenceVideo", "vid"),
    "audio": ("referenceAudio", "aud"),
}

# ---------------- 参考素材上限（按模型，可后台覆写） ----------------

_DEFAULT_REF_LIMITS = {"image": 9, "video": 3, "audio": 3}
_REF_LIMIT_KEYS = ("image", "video", "audio")


def limits_from_gen_limits(gen_limits) -> dict:
    """从官方 list_models 的 gen_limits 读取参考素材上限。

    type=10（omni_reference 参考生成）的 other_input 里带 image/audio/video 各自上限，
    能读出就用；读不出的项留空，由 normalize_ref_limits 回退默认值。
    """
    out: dict = {}
    for g in (gen_limits or []):
        if not isinstance(g, dict) or not isinstance(g.get("other_input"), dict):
            continue
        for kind in _REF_LIMIT_KEYS:
            num = (g["other_input"].get(kind) or {}).get("num")
            if isinstance(num, int) and num > 0:
                out[kind] = num
    return out


def normalize_ref_limits(raw) -> dict:
    """补齐缺项并算出允许的总数（未显式给 total 时 = 三类之和）。"""
    lim = dict(_DEFAULT_REF_LIMITS)
    if isinstance(raw, dict):
        for k in _REF_LIMIT_KEYS:
            try:
                v = int(raw[k])
            except (KeyError, TypeError, ValueError):
                continue
            if v >= 0:
                lim[k] = v
        try:
            t = int(raw["total"])
        except (KeyError, TypeError, ValueError):
            t = 0
        lim["total"] = t if t > 0 else sum(lim[k] for k in _REF_LIMIT_KEYS)
        return lim
    lim["total"] = sum(lim[k] for k in _REF_LIMIT_KEYS)
    return lim


def ref_limits_for_key(upstream_key: str) -> dict:
    """按上游 model_key 取内置实测覆写（无则返回空 dict）。"""
    ov = _REF_LIMIT_OVERRIDES.get((upstream_key or "").strip())
    return normalize_ref_limits(ov) if ov else {}


def ref_limits_of(model) -> dict:
    """模型实际生效的参考素材上限：后台覆写 > 内置实测覆写 > 官方目录推断 > 内置默认。

    返回里带 ``_from``（``manual``/``catalog``/``override``/``default``）便于日志与后台展示，
    与 ``gen_limits_of`` 保持一致的来源语义。
    内置覆写必须**压过官方目录**：官方目录对本账号常给降级值（9/3/3）。
    """
    raw = None
    try:
        raw = getattr(model, "ref_limits", None)
    except Exception:  # noqa: BLE001
        raw = None
    if raw:
        src = str(raw.get("_from") or "") if isinstance(raw, dict) else ""
        lim = normalize_ref_limits(raw)
        lim["_from"] = src or "manual"
        return lim
    ov = ref_limits_for_key(getattr(model, "mcp_tool", "") or "")
    if ov:
        ov["_from"] = "override"
        return ov
    derived = limits_from_gen_limits((getattr(model, "catalog", None) or {}).get("gen_limits"))
    lim = normalize_ref_limits(derived)
    lim["_from"] = "catalog" if derived else "default"
    return lim


# ---------------- 生成能力上限（分辨率 / 时长，按模型可配） ----------------

# 内置默认：CapCut 侧长期只放 480p/720p 两档、时长 2-15s（保持既有行为）。
_DEFAULT_GEN_LIMITS = {
    "resolutions": [480, 720],   # 分辨率档（短边像素）
    "durations": [],             # 时长档位；空=只按 min/max 连续取值
    "min_duration": 2.0,
    "max_duration": 15.0,
}

# 内置「实测能力」覆写（按上游 model_key）。官方目录对本账号常给降级值，
# 与模型真实能力不符，故按已核对的能力放开；后台可再逐模型改。
# Seedance 2.5：官方声明 480p/720p/1080p + 5/8/10/12/15/18/20/25/30s，别的一律不放。
_GEN_LIMIT_OVERRIDES = {
    # 2.0 / 2.5 只开放 480p + 720p（2026-09-16 运营决策：1080p 单价过高，不对客户端开放）
    "seedance_2.0": {
        "resolutions": [480, 720],
    },
    "seedance_2.5": {
        "resolutions": [480, 720],
        "durations": [5, 8, 10, 12, 15, 18, 20, 25, 30],
        "min_duration": 2,
        "max_duration": 30,
    },
}

_GEN_LIMIT_SOURCES = {"override": "实测覆写", "manual": "后台设置", "default": "内置默认"}

# 参考素材上限的内置「实测覆写」（同样按上游 model_key）。
# 官方目录对本账号给的是降级值（图9/视频3/音频3），与模型真实能力不符，故按实测能力覆写。
# ⚠️ 这张表**必须**在 service 里（而不是 capcut_channel）：`ref_limits_of` 是唯一的解析入口，
#    只有放在这里，**未经官方目录同步**的模型（首启播种 / 管理员手工新建）也能拿到正确上限；
#    以前它只存在于 capcut_channel.sync_catalog 内部 → 播种/手工建的 2.5 会掉回默认 9/3/3。
_REF_LIMIT_OVERRIDES = {
    # Seedance 2.0: 钉死官方目录值 9/3/3（运营决策：不给 2.0 放开大参考量）
    "seedance_2.0": {"image": 9, "video": 3, "audio": 3},
    # Seedance 2.5: 30 参考图 / 10 参考视频 / 10 参考音频
    "seedance_2.5": {"image": 30, "video": 10, "audio": 10},
}


def _num_list(raw) -> list:
    """宽松取数字数组（容忍 '1080p' / '1080' / 数字混排）。"""
    if raw is None:
        return []
    if isinstance(raw, (int, float, str)):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return []
    out = []
    for v in raw:
        if isinstance(v, bool):
            continue
        try:
            out.append(float(str(v).strip().rstrip("pP")))
        except (TypeError, ValueError):
            continue
    return out


def _num(raw):
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(str(raw).strip().rstrip("pP"))
    except (TypeError, ValueError):
        return None


def normalize_gen_limits(raw) -> dict:
    """规范化生成能力上限：补齐缺项、排序去重、兜底到内置默认。"""
    lim = dict(_DEFAULT_GEN_LIMITS)
    lim["resolutions"] = list(_DEFAULT_GEN_LIMITS["resolutions"])
    lim["durations"] = list(_DEFAULT_GEN_LIMITS["durations"])
    src = ""
    if isinstance(raw, dict):
        src = str(raw.get("_from") or "")
        res = sorted({int(v) for v in _num_list(raw.get("resolutions")) if 120 <= v <= 8640})
        if res:
            lim["resolutions"] = res
        dur = sorted({v for v in _num_list(raw.get("durations")) if 0 < v <= 600})
        if dur:
            lim["durations"] = dur
        lo, hi = _num(raw.get("min_duration")), _num(raw.get("max_duration"))
        if lo is not None and lo > 0:
            lim["min_duration"] = lo
        if hi is not None and hi > 0:
            lim["max_duration"] = hi
    # 给了档位就以档位为界（避免出现「档位 30s 但上限 15s」这类自相矛盾配置）
    if lim["durations"]:
        lim["max_duration"] = max(lim["max_duration"], lim["durations"][-1])
        lim["min_duration"] = min(lim["min_duration"], lim["durations"][0])
    if lim["max_duration"] < lim["min_duration"]:
        lim["max_duration"] = lim["min_duration"]
    lim["resolutions"] = lim["resolutions"] or list(_DEFAULT_GEN_LIMITS["resolutions"])
    if src:
        lim["_from"] = src
    return lim


def gen_limits_for_key(upstream_key: str) -> dict:
    """按上游 model_key 解析生成能力上限（内置实测覆写 > 内置默认），带来源标记。"""
    ov = _GEN_LIMIT_OVERRIDES.get((upstream_key or "").strip())
    lim = normalize_gen_limits(ov)
    lim["_from"] = "override" if ov else "default"
    return lim


def gen_limits_of(model) -> dict:
    """模型实际生效的生成能力上限：后台设置 > 内置实测覆写 > 内置默认。"""
    raw = None
    try:
        raw = getattr(model, "gen_limits", None)
    except Exception:  # noqa: BLE001
        raw = None
    if raw:
        lim = normalize_gen_limits(raw)
        lim.setdefault("_from", "manual")
        return lim
    return gen_limits_for_key(getattr(model, "mcp_tool", "") or "")


def gen_limits_text_of(model) -> str:
    """后台展示用：'时长 2-30s（档位 5/8/…/30）｜分辨率 480/720/1080p（实测覆写）'"""
    lim = gen_limits_of(model)
    dur = ("/".join(f"{d:g}" for d in lim["durations"]) if lim["durations"]
           else f"{lim['min_duration']:g}-{lim['max_duration']:g}")
    res = "/".join(f"{r}p" for r in lim["resolutions"])
    return (f"时长 {dur}s｜分辨率 {res}"
            f"（{_GEN_LIMIT_SOURCES.get(lim.get('_from', ''), lim.get('_from') or '内置默认')}）")


def build_reference_files(prompt: str, params: dict, limits=None) -> tuple[str, list[dict]]:
    """将公网参考素材转换为 CF ``files`` 参数，并按模型上限校验数量。

    上限来自模型（``ref_limits_of``）：官方目录/后台覆写优先，缺省 图9/视频3/音频3。
    例如 Seedance 2.5 为 30 图 / 10 视频 / 10 音频。

    网关不会读取调用方本地磁盘；本地素材应先由调用方上传至 R2/S3 等对象存储，
    再传入可由 CF 访问的 http(s) 签名 URL。
    """
    supplied = params.get("reference_files")
    by_kind: dict[str, list[str]] = {k: [] for k in _REF_ROLES}
    if supplied is not None:
        if not isinstance(supplied, list):
            raise ValueError("reference_files 必须为数组")
        for item in supplied:
            if not isinstance(item, dict):
                raise ValueError("reference_files 的每项必须为对象")
            role = str(item.get("role") or "").strip()
            url = item.get("url")
            kind = {v[0]: k for k, v in _REF_ROLES.items()}.get(role)
            if not kind:
                raise ValueError("reference_files.role 仅支持 referenceImage/referenceVideo/referenceAudio")
            if not isinstance(url, str):
                raise ValueError("参考素材 url 必须为字符串")
            by_kind[kind].append(url)
    else:
        for kind in _REF_ROLES:
            raw = params.get(kind, [])
            if isinstance(raw, str):
                raw = [raw]
            if raw is None:
                raw = []
            if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
                raise ValueError(f"{kind} 必须为 URL 字符串或 URL 数组")
            by_kind[kind] = raw

    lim = normalize_ref_limits(limits)
    for kind, values in by_kind.items():
        if len(values) > lim[kind]:
            raise ValueError(f"参考{kind}最多 {lim[kind]} 个（当前 {len(values)}）")
    total = sum(len(x) for x in by_kind.values())
    if total > lim["total"]:
        raise ValueError(f"三类参考文件共享总数上限 {lim['total']}（当前 {total}）")
    if by_kind["audio"] and not (by_kind["image"] or by_kind["video"]):
        raise ValueError("参考音频必须搭配至少一个参考图或参考视频")

    files: list[dict] = []
    for kind, (role, prefix) in _REF_ROLES.items():
        for i, url in enumerate(by_kind[kind], 1):
            url = url.strip()
            if not url.startswith(("https://", "http://")):
                raise ValueError("参考素材必须是 CF 可访问的 http(s) URL；本地文件请先上传至对象存储")
            files.append({"role": role, "alias": f"{prefix}{i}", "url": url})

    if files and not params.get("keep_prompt") and "{{" not in prompt:
        refs = ", ".join("{{" + f["alias"] + "}}" for f in files)
        prompt += (f" Use {refs} as reference materials: keep subject and style consistent "
                   "with images, motion and camera language with videos, and mood with audio.")
    return prompt, files


def build_generate_args(model: ModelEntry, prompt: str, params: dict) -> dict:
    """把 OpenAI 风格请求转换为 generate 的 {model, modality, prompt, options}。"""
    pricing = model.catalog.get("pricing") or {}
    inputs = pricing.get("inputs") or {}
    allowed = set(inputs.get("options") or [])

    def opt(*names, default=None):
        for n in names:
            if n in allowed:
                return n
        return default

    tpl = dict(model.param_template)
    tpl.pop("modality", None)
    # 兼容两种模板写法：平铺 {"duration_seconds":5} 或嵌套 {"options":{...}}
    inner = tpl.pop("options", None)
    if isinstance(inner, dict):
        tpl.update(inner)

    req_options: dict = {}
    if isinstance(params.get("options"), dict):
        req_options.update(params["options"])
    duration_value = params.get("duration")

    # 支持 duration_seconds
    if duration_value is None:
        duration_value = params.get("duration_seconds")

    if duration_value is not None:
        key = opt("duration_seconds", "duration", "seconds")
        if key:
            req_options[key] = duration_value
    size = params.get("size") or params.get("resolution")
    if size is not None:
        key = opt("resolution", "size")
        if key:
            req_options[key] = size
    if params.get("aspect_ratio") is not None:
        key = opt("aspect_ratio")
        if key:
            req_options[key] = params["aspect_ratio"]
    if params.get("negative_prompt") is not None:
        key = opt("negative_prompt", "negativePrompt")
        if key:
            req_options[key] = params["negative_prompt"]
    if params.get("seed") is not None:
        key = opt("seed")
        if key:
            req_options[key] = params["seed"]

    # 有选项白名单时，请求侧仅保留白名单内（或模板已含）的键。
    if allowed:
        req_options = {k: v for k, v in req_options.items()
                       if v is not None and (k in allowed or k in tpl)}
    else:
        req_options = {k: v for k, v in req_options.items() if v is not None}
    # 后台模板仅提供默认值；调用方请求中的同名参数优先。
    options = {**{k: v for k, v in tpl.items() if v is not None}, **req_options}

    prompt, files = build_reference_files(str(prompt or ""), params, limits=ref_limits_of(model))
    modality = resolve_modality(model, params)
    if files and not params.get("modality"):
        modality = "mediaToVideo"
    return {
        "model": model.catalog.get("name") or model.model_id,
        "modality": modality,
        "prompt": prompt,
        "options": options,
        "files": files or None,
    }


# ---------------- 积分 ----------------

def _apply_cost(db: Session, account: Account, gen: Generation, cost: float) -> None:
    if not cost or (account.coin_balance or 0) <= 0:
        return
    before = account.coin_balance
    account.coin_balance = max(0.0, before - cost)
    db.add(CoinTransaction(
        account_id=account.id, account_name=account.name, generation_id=gen.gen_id,
        kind="auto", before_balance=before, cost=cost,
        after_balance=account.coin_balance, note=f"模型 {gen.model_id} 生成扣费"))


def note_account_error(db: Session, account: Account, err: CFError, message: str = ""):
    kind = getattr(err, "kind", classify_error_text(str(err)))
    status = ACCOUNT_STATUS_BY_KIND.get(kind)
    if status and account.status != Account.STATUS_DISABLED:
        account.status = status
    account.last_error = (message or str(err))[:800]
    if kind == "upstream":
        account.fail_count = (account.fail_count or 0) + 1
    db.commit()
    return kind
