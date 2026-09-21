"""管理后台 API（JWT 鉴权）。"""
import asyncio
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import oauth, security, service
from ..config import get_config
from ..db import SessionLocal, db_session
from ..models import (Account, ApiKey, CoinTransaction, Generation, ModelEntry,
                      OAuthFlow, Setting, JSONText, log_event, new_gen_id, now)
from .deps import require_admin

router = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin)])


# ---------------- 登录（无鉴权子路由，挂在 main） ----------------

class LoginBody(BaseModel):
    username: str
    password: str


def build_login_router() -> APIRouter:
    r = APIRouter(prefix="/admin/api")

    @r.post("/login")
    def login(body: LoginBody, db: Session = Depends(db_session)):
        if not security.check_admin_password(body.username, body.password):
            log_event(db, "warn", "admin_login_failed", {"username": body.username[:32]})
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = security.issue_admin_token(body.username)
        log_event(db, "info", "admin_login", {"username": body.username[:32]})
        return {"token": token, "username": body.username}

    @r.get("/me")
    def me(_: bool = Depends(require_admin), db: Session = Depends(db_session)):
        cfg = get_config()
        return {"username": cfg.ADMIN_USER, "base_url": cfg.PUBLIC_BASE_URL,
                "mcp_endpoint": cfg.MCP_ENDPOINT, "version": cfg.CLIENT_VERSION}

    return r


# ---------------- 总览 ----------------

@router.get("/overview")
def overview(db: Session = Depends(db_session)):
    day_start = time.time() - (time.time() % 86400)  # UTC 当天零点
    accounts_total = db.execute(select(func.count(Account.id))).scalar() or 0
    accounts_active = db.execute(
        select(func.count(Account.id)).where(Account.status == Account.STATUS_ACTIVE)).scalar() or 0
    models_enabled = db.execute(
        select(func.count(ModelEntry.id)).where(ModelEntry.enabled == True)).scalar() or 0
    keys_active = db.execute(
        select(func.count(ApiKey.id)).where(ApiKey.enabled == True)).scalar() or 0
    today_total = db.execute(select(func.count(Generation.id)).where(
        Generation.created_at >= day_start)).scalar() or 0
    today_ok = db.execute(select(func.count(Generation.id)).where(
        Generation.created_at >= day_start, Generation.status == Generation.STATUS_COMPLETED)).scalar() or 0
    today_failed = db.execute(select(func.count(Generation.id)).where(
        Generation.created_at >= day_start, Generation.status == Generation.STATUS_FAILED)).scalar() or 0
    running = db.execute(select(func.count(Generation.id)).where(
        Generation.status.in_([Generation.STATUS_PROCESSING, Generation.STATUS_POLLING]))).scalar() or 0
    queued = db.execute(select(func.count(Generation.id)).where(
        Generation.status == Generation.STATUS_QUEUED)).scalar() or 0
    coins_today = db.execute(select(func.coalesce(func.sum(Generation.cost), 0.0)).where(
        Generation.created_at >= day_start)).scalar() or 0
    total_coins = db.execute(select(func.coalesce(func.sum(Account.coin_balance), 0.0))).scalar() or 0
    recent = db.execute(select(Generation).order_by(Generation.id.desc()).limit(8)).scalars().all()
    by_model = db.execute(select(Generation.model_id, Generation.status, func.count(Generation.id))
                          .where(Generation.created_at >= day_start)
                          .group_by(Generation.model_id, Generation.status)).all()
    from ..r2 import R2Store
    cfg = get_config()
    r2_mode_keys = db.execute(select(func.count(ApiKey.id)).where(
        ApiKey.enabled == True, ApiKey.capcut_link_mode == "r2")).scalar() or 0
    return {
        "accounts": {"total": accounts_total, "active": accounts_active},
        "models_enabled": models_enabled,
        "keys_active": keys_active,
        "today": {"requests": today_total, "success": today_ok, "failed": today_failed,
                  "running": running, "queued": queued, "coins": coins_today},
        "total_coins": total_coins,
        "r2": {
            "enabled": R2Store.enabled(),
            "bucket": cfg.R2_BUCKET if R2Store.enabled() else "",
            "public_base": cfg.R2_PUBLIC_BASE if R2Store.enabled() else "",
            "keys_using_r2": r2_mode_keys,
            "note": "" if R2Store.enabled() else "R2 未配置完整，「官转」密钥将回退官链",
        },
        "recent": [g.to_dict(include_result=False) for g in recent],
        "by_model": [{"model": m, "status": s, "count": c} for m, s, c in by_model],
    }


# ---------------- 账号 ----------------

class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    provider: str = Field(default="creative-fabrica",
                          description="creative-fabrica(MCP) / capcut(直连)")
    cookie: str = Field(default="", description="浏览器导出的 Cookie JSON 数组或 k=v; 头字符串")
    auth_type: str = Field(default="cookie")
    deep: bool = Field(default=True, description="深度验活：用最便宜模型真实生成一次")


class AccountPatch(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None
    max_concurrency: Optional[int] = None
    coin_balance: Optional[float] = None
    cookie: Optional[str] = None
    # 只增删改单个 cookie（如补 d_ticket），不用重新导入整份
    cookie_patch: Optional[dict] = None
    # 传 dict 覆盖（{key:{sign,device_time,tdid}}）；传 {} 清空（回到按算法现签）
    capcut_signs: Optional[dict] = None


class SignsExtract(BaseModel):
    # 原始 HAR 文本 / curl / 复制的 request header 均可
    text: str = Field(default="", description="HAR 原文或 header 片段")
    default_key: str = Field(default="new", description="文本里没有接口路径时归属到哪个接口")
    save: bool = Field(default=True, description="提取后立刻保存到该账号")
    merge: bool = Field(default=True, description="与已有签名合并（否则整体替换）")


class SignsMint(BaseModel):
    device_time: Optional[int] = Field(default=None, description="不传=当前时间")
    keys: Optional[list] = None
    # 默认只算不存：现签本来就是按当前时间算的，存下来反而会把 device-time 冻结成旧值
    save: bool = Field(default=False, description="true=顺带保存到账号")


@router.get("/accounts")
def list_accounts(db: Session = Depends(db_session)):
    rows = db.execute(select(Account).order_by(Account.id)).scalars().all()
    running = dict(db.execute(select(Generation.account_id, func.count(Generation.id)).where(
        Generation.status.in_([Generation.STATUS_PROCESSING, Generation.STATUS_POLLING]))
        .group_by(Generation.account_id)).all())
    result = []
    for a in rows:
        d = a.to_dict()
        d["running"] = running.get(a.id, 0)
        d["subscription"] = (service.cookie_subscription(security.decrypt(a.cookie_enc))
                             if a.cookie_enc else None)
        if (a.provider or "") == "capcut" and a.cookie_enc:
            from ..capcut_channel import cookie_health
            h = cookie_health(security.decrypt(a.cookie_enc))
            d["cookie_health"] = {"can_submit_likely": h["can_submit_likely"],
                                  "missing": h["missing_critical"] + h["missing_high"],
                                  "hint": h["hint"]}
        result.append(d)
    return {"items": result}


@router.post("/accounts")
async def create_account(body: AccountCreate, db: Session = Depends(db_session)):
    exists = db.execute(select(Account).where(Account.name == body.name)).scalar_one_or_none()
    if exists:
        raise HTTPException(400, "账号名称已存在")
    provider = body.provider if body.provider in ("creative-fabrica", "capcut") else "creative-fabrica"
    acc = Account(name=body.name, provider=provider,
                  auth_type=body.auth_type if body.auth_type in ("cookie", "oauth") else "cookie")
    db.add(acc)
    db.commit()
    if body.cookie:
        try:
            service.import_cookie(db, acc, body.cookie)
        except ValueError as e:
            db.delete(acc)
            db.commit()
            raise HTTPException(400, str(e))
        # 导入后自动做一次链路测试（CapCut: 积分验活；CF: 可选真实生成）
        result = await service.test_account(db, acc, deep=body.deep if provider != "capcut" else False)
        return {**acc.to_dict(), "probe": result}
    log_event(db, "info", "account_created", {"account": body.name, "provider": provider},
              account_id=acc.id)
    return acc.to_dict()


@router.patch("/accounts/{account_id}")
async def patch_account(account_id: int, body: AccountPatch, db: Session = Depends(db_session)):
    acc = db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if body.name is not None:
        acc.name = body.name
    if body.status is not None:
        if body.status not in (Account.STATUS_ACTIVE, Account.STATUS_DISABLED):
            raise HTTPException(400, "仅允许设置 active / disabled")
        acc.status = body.status
    if body.max_concurrency is not None:
        acc.max_concurrency = max(1, min(10, body.max_concurrency))
    if body.coin_balance is not None:
        old = acc.coin_balance or 0
        acc.coin_balance = max(0.0, body.coin_balance)
        if old != acc.coin_balance:
            db.add(CoinTransaction(account_id=acc.id, account_name=acc.name, kind="adjust",
                                   before_balance=old, cost=old - acc.coin_balance,
                                   after_balance=acc.coin_balance, note="管理员手工调整余额"))
    if body.cookie is not None and body.cookie.strip():
        try:
            service.import_cookie(db, acc, body.cookie)
        except ValueError as e:
            raise HTTPException(400, str(e))
    if body.cookie_patch and (acc.provider or "") == "capcut":
        from ..capcut_channel import patch_capcut_cookies
        merged = patch_capcut_cookies(security.decrypt(acc.cookie_enc), body.cookie_patch)
        acc.cookie_enc = security.encrypt(merged)
        log_event(db, "info", "account_cookie_patched",
                  {"account": acc.name, "names": sorted(body.cookie_patch)}, account_id=acc.id)
    if body.capcut_signs is not None:
        from ..capcut_signs import normalize_signs
        if not body.capcut_signs:
            acc.capcut_signs_text = ""
        else:
            kept = {k: v for k, v in normalize_signs(body.capcut_signs).items()}
            acc.capcut_signs_text = JSONText.dump(kept)
    db.commit()
    return acc.to_dict()


# ---------------- CapCut 签名（按账号） ----------------

def _capcut_account(db: Session, account_id: int) -> Account:
    acc = db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if (acc.provider or "") != "capcut":
        raise HTTPException(400, "该账号不是 CapCut 渠道")
    return acc


@router.get("/accounts/{account_id}/cookie-health")
def account_cookie_health(account_id: int, db: Session = Depends(db_session)):
    """CapCut Cookie 风控体检：哪些关键 cookie 缺失（尤其 d_ticket）。"""
    from ..capcut_channel import cookie_health
    acc = _capcut_account(db, account_id)
    return {"account_id": acc.id, "account": acc.name,
            **cookie_health(security.decrypt(acc.cookie_enc))}


@router.get("/accounts/{account_id}/signs")
def get_account_signs(account_id: int, reveal: bool = False, db: Session = Depends(db_session)):
    """查看该账号的签名现状：账号存的 / 内置兜底 / 按算法复算校验。"""
    from ..capcut_signs import (DEFAULT_APPVR, DEFAULT_PF, KEY_PATHS, SIGN_KEY_INFO,
                                describe_signs, effective_signs, mint_sign, verify_sign)
    acc = _capcut_account(db, account_id)
    stored = acc.capcut_signs
    rows = describe_signs(stored)
    eff = effective_signs(stored)
    if reveal:
        for r in rows:
            r["sign"] = (eff.get(r["key"]) or {}).get("sign")
    minted = {}
    for k in KEY_PATHS:
        e = mint_sign(k, pf=DEFAULT_PF, appvr=DEFAULT_APPVR,
                      tdid=(stored.get("upload_sign") or {}).get("tdid") or "")
        minted[k] = e
    for r in rows:
        cur = eff.get(r["key"]) or {}
        m = minted.get(r["key"])
        r["mint_sign"] = m["sign"] if m else None
        r["mint_device_time"] = m["device_time"] if m else None
        r["algorithm_verified"] = bool(
            cur.get("sign") and verify_sign(KEY_PATHS[r["key"]], cur["sign"], cur["device_time"],
                                            pf=cur.get("pf") or DEFAULT_PF,
                                            appvr=cur.get("appvr") or DEFAULT_APPVR,
                                            tdid=cur.get("tdid") or ""))
    return {
        "account_id": acc.id, "account": acc.name,
        "provider": acc.provider,
        "sign_mode": (os.environ.get("CAPCUT_SIGN_MODE") or "auto"),
        "stored_keys": sorted(stored.keys()),
        "rows": rows,
        "algorithm": "sign = md5('9e2c|' + 路径末7字符 + '|pf|appvr|device-time|tdid|11ac')",
        "hint": "默认按算法现签即可（device-time 永远是新的）；只有上游开始按账号校验签名或算法变更时，"
                "才需要在这里存账号专属签名。",
    }


@router.put("/accounts/{account_id}/signs")
def put_account_signs(account_id: int, body: dict, db: Session = Depends(db_session)):
    """直接写入签名（{"new":{"sign":..,"device_time":..}} 或 {"text": "..."} 自动识别）。"""
    from ..capcut_signs import extract, normalize_signs
    acc = _capcut_account(db, account_id)
    signs = normalize_signs(body.get("signs") if isinstance(body.get("signs"), dict) else body)
    if not signs and isinstance(body.get("text"), str) and body["text"].strip():
        signs = extract(body["text"], default_key=body.get("default_key") or "new")["signs"]
    if not signs:
        raise HTTPException(400, "没有解析到任何签名（需要 sign + device-time）")
    cur = {} if body.get("replace") else dict(acc.capcut_signs)
    cur.update(signs)
    acc.capcut_signs_text = JSONText.dump(cur)
    db.commit()
    log_event(db, "info", "account_signs_updated",
              {"account": acc.name, "keys": sorted(signs.keys())}, account_id=acc.id)
    return acc.to_dict()


@router.delete("/accounts/{account_id}/signs")
def clear_account_signs(account_id: int, db: Session = Depends(db_session)):
    """清空账号签名 → 回到「按算法现签」。"""
    acc = _capcut_account(db, account_id)
    acc.capcut_signs_text = ""
    db.commit()
    log_event(db, "info", "account_signs_cleared", {"account": acc.name}, account_id=acc.id)
    return {"ok": True, "account_id": acc.id}


@router.post("/accounts/{account_id}/signs/extract")
def extract_account_signs(account_id: int, body: SignsExtract, db: Session = Depends(db_session)):
    """从 HAR / header 片段自动提取该账号的签名（可选直接保存）。"""
    from ..capcut_signs import SIGN_KEY_INFO, extract, make_sign
    acc = _capcut_account(db, account_id)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "text 不能为空（粘贴 HAR 原文或 request header）")
    res = extract(text, default_key=body.default_key or "new")
    if not res["signs"]:
        raise HTTPException(400, "没能从这段文本里提取到 sign/device-time；"
                                 "请确认包含签名的那个请求（sign 头 + device-time 头）")
    verified = {}
    for k, e in res["signs"].items():
        from ..capcut_signs import KEY_PATHS, verify_sign
        verified[k] = verify_sign(KEY_PATHS[k], e["sign"], e["device_time"])
    if body.save:
        cur = dict(acc.capcut_signs) if body.merge else {}
        cur.update(res["signs"])
        acc.capcut_signs_text = JSONText.dump(cur)
        db.commit()
        log_event(db, "info", "account_signs_extracted",
                  {"account": acc.name, "found": res["found"]}, account_id=acc.id)
    return {"account_id": acc.id, "extracted": res["signs"], "found": res["found"],
            "missing": res["missing"], "algorithm_verified": verified,
            "saved": body.save, "keys": sorted(SIGN_KEY_INFO)}


@router.post("/accounts/{account_id}/signs/mint")
def mint_account_signs(account_id: int, body: SignsMint, db: Session = Depends(db_session)):
    """按逆向出的算法给该账号现签一套（默认保存）。device_time 不传=当前时间。"""
    from ..capcut_signs import KEY_PATHS, mint_sign
    acc = _capcut_account(db, account_id)
    keys = [k for k in (body.keys or list(KEY_PATHS)) if k in KEY_PATHS]
    if not keys:
        raise HTTPException(400, "keys 无效")
    tdid = (acc.capcut_signs.get("upload_sign") or {}).get("tdid") or ""
    signs = {}
    for k in keys:
        signs[k] = mint_sign(k, device_time=body.device_time,
                             tdid=tdid if k in ("upload_sign", "upload_sign_ref") else "")
    if body.save:
        cur = dict(acc.capcut_signs)
        cur.update(signs)
        acc.capcut_signs_text = JSONText.dump(cur)
        db.commit()
    return {"account_id": acc.id, "signs": signs, "saved": body.save}


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, db: Session = Depends(db_session)):
    acc = db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    running = db.execute(select(func.count(Generation.id)).where(
        Generation.account_id == account_id,
        Generation.status.in_([Generation.STATUS_PROCESSING, Generation.STATUS_POLLING]))).scalar() or 0
    if running:
        raise HTTPException(400, f"该账号还有 {running} 个执行中的任务，请稍后再删")

    # 历史记录保留 account_name 快照；解除外键关联后再删除账号本身。
    # 这样旧任务、成本和审计记录仍可查询，不会再被分配给其他账号。
    archived_generations = db.execute(
        update(Generation).where(Generation.account_id == account_id).values(account_id=None)
    ).rowcount or 0
    archived_transactions = db.execute(
        update(CoinTransaction).where(CoinTransaction.account_id == account_id).values(account_id=None)
    ).rowcount or 0
    db.execute(delete(OAuthFlow).where(OAuthFlow.account_id == account_id))
    name = acc.name
    db.delete(acc)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "账号仍被其他数据引用，未执行删除") from exc
    log_event(db, "info", "account_deleted", {"account": name})
    return {
        "ok": True,
        "archived_generations": archived_generations,
        "archived_transactions": archived_transactions,
    }


# ---------------- 批量导入 / 批量验活 ----------------

class BatchAccountItem(BaseModel):
    name: str = ""
    cookie: str


class BatchImportBody(BaseModel):
    accounts: list[BatchAccountItem]
    provider: str = Field(default="creative-fabrica",
                          description="creative-fabrica(MCP) / capcut(直连)")
    keep_dead: bool = False   # true 时验活失败的也保留（标记 error 状态）
    probe: bool = True        # 是否导入时验活
    deep: bool = True         # 深度验活：真实生成一次（最低价模型，失败生成官方退款）


def _unique_name(db: Session, base: str, used: set) -> str:
    name = (base or "").strip() or ("acc-" + secrets.token_hex(3))
    candidate, i = name, 2
    while candidate in used or db.execute(
            select(Account.id).where(Account.name == candidate)).scalar_one_or_none():
        candidate = f"{name}-{i}"
        i += 1
    used.add(candidate)
    return candidate


@router.post("/accounts/batch-import")
async def batch_import(body: BatchImportBody, db: Session = Depends(db_session)):
    """批量导入 Cookie 账号：逐个验活（initialize + list_models），自动命名/去重。"""
    if not body.accounts:
        raise HTTPException(400, "accounts 不能为空")
    if len(body.accounts) > 200:
        raise HTTPException(400, "单次最多导入 200 个账号")

    # ---- CapCut 直连通道批量导入 ----
    if body.provider == "capcut":
        from ..capcut_channel import (capcut_cookie_identity, normalize_capcut_cookie,
                                      refresh_balance)
        existing_fp: dict[str, str] = {}
        for a in db.execute(select(Account).where(Account.provider == "capcut")).scalars().all():
            if a.cookie_enc:
                fp = capcut_cookie_identity(security.decrypt(a.cookie_enc))
                if fp:
                    existing_fp[fp] = a.name
        used: set = set()
        created, dead, skipped, invalid = [], [], [], []
        for idx, item in enumerate(body.accounts):
            label = item.name or f"#{idx + 1}"
            try:
                norm = normalize_capcut_cookie(item.cookie)
            except ValueError as e:
                invalid.append({"name": label, "reason": str(e)})
                continue
            fp = capcut_cookie_identity(norm)
            if fp and fp in existing_fp:
                skipped.append({"name": label, "reason": f"与已有账号「{existing_fp[fp]}」重复"})
                continue
            probe = {"ok": True, "detail": "未验活", "balance": None}
            if body.probe:
                tmp_acc = Account(name=f"__probe_{secrets.token_hex(4)}", provider="capcut")
                tmp_acc.cookie_enc = security.encrypt(norm)
                try:
                    bal = await asyncio.to_thread(refresh_balance, tmp_acc)
                    probe = {"ok": True, "detail": f"积分余额 {bal:g}", "balance": bal}
                except Exception as e:  # noqa: BLE001
                    from ..capcut_channel import classify_capcut_error
                    probe = {"ok": False, "detail": str(e)[:200], "balance": None,
                             "kind": classify_capcut_error(str(e))}
            if not probe["ok"] and not body.keep_dead:
                dead.append({"name": item.name or fp or label, "reason": probe["detail"]})
                continue
            name = _unique_name(db, item.name or f"capcut-{(fp or 'anon')[:8]}", used)
            kind = probe.get("kind", "")
            acc = Account(
                name=name, provider="capcut", auth_type="cookie",
                status=Account.STATUS_ACTIVE if probe["ok"] else
                       (Account.STATUS_EXPIRED if kind == "auth" else Account.STATUS_ERROR),
                coin_balance=probe.get("balance") or 0,
                last_error="" if probe["ok"] else probe["detail"],
                last_check_at=now() if body.probe else 0)
            db.add(acc)
            db.commit()
            acc.cookie_enc = security.encrypt(norm)
            db.commit()
            if fp:
                existing_fp[fp] = name
            created.append({"name": name, "ok": probe["ok"], "detail": probe["detail"],
                            "balance": probe.get("balance")})
        log_event(db, "info", "capcut_batch_import",
                  {"total": len(body.accounts), "created": len(created),
                   "dead": len(dead), "skipped": len(skipped), "invalid": len(invalid)})
        return {"created": created, "dead": dead, "skipped": skipped, "invalid": invalid,
                "summary": {"total": len(body.accounts), "created": len(created),
                            "alive_created": sum(1 for c in created if c["ok"]),
                            "dead": len(dead), "skipped": len(skipped), "invalid": len(invalid)}}

    # ---- Creative Fabrica (MCP) 批量导入 ----

    # 已有账号的 Cookie 身份指纹（用于去重）
    existing_fp: dict[str, str] = {}
    for a in db.execute(select(Account)).scalars().all():
        if a.cookie_enc:
            fp = service.cookie_identity(security.decrypt(a.cookie_enc))
            if fp:
                existing_fp[fp] = a.name

    used_names: set = set()
    created, dead, skipped, invalid = [], [], [], []
    for idx, item in enumerate(body.accounts):
        label = item.name or f"#{idx + 1}"
        try:
            header, ua = service.parse_cookie_export(item.cookie)
        except ValueError as e:
            invalid.append({"name": label, "reason": str(e)})
            continue
        fp = service.cookie_identity(header)
        if fp and fp in existing_fp:
            skipped.append({"name": label, "reason": f"与已有账号「{existing_fp[fp]}」重复"})
            continue

        probe = {"ok": True, "detail": "未验活", "models": 0}
        if body.probe:
            probe = await service.probe_cookie(header, ua, deep=body.deep)

        # 订阅检查：Cookie 明确显示无 Studio AI 订阅 → 导入但标记积分不足（不进调度池）
        sub = service.cookie_subscription(header)
        sub_active = service.subscription_active(sub)
        if probe["ok"] and sub_active is False:
            probe = {"ok": True,
                     "detail": f"验活通过，但 Studio AI 订阅未生效（{sub}）——已标记为不参与调度",
                     "models": probe.get("models", 0)}

        if not probe["ok"] and not body.keep_dead:
            dead.append({"name": item.name or fp or label, "reason": probe["detail"]})
            continue

        name = _unique_name(db, item.name or service.derive_account_name(header), used_names)
        no_sub = sub_active is False
        acc = Account(
            name=name, auth_type="cookie",
            status=Account.STATUS_INSUFFICIENT if no_sub else
                  (Account.STATUS_ACTIVE if probe["ok"] else Account.STATUS_ERROR),
            last_error=(f"Studio AI 订阅未生效（cfsub_studio_ai_status={sub}）" if no_sub
                        else ("" if probe["ok"] else probe["detail"])),
            last_check_at=now() if body.probe else 0)
        db.add(acc)
        db.commit()
        service.import_cookie(db, acc, item.cookie)
        if fp:
            existing_fp[fp] = name
        created.append({"name": name, "ok": probe["ok"], "detail": probe["detail"],
                        "models": probe["models"], "subscription": sub})

    log_event(db, "info", "batch_import",
              {"total": len(body.accounts), "created": len(created), "dead": len(dead),
               "skipped": len(skipped), "invalid": len(invalid)})
    return {
        "created": created, "dead": dead, "skipped": skipped, "invalid": invalid,
        "summary": {"total": len(body.accounts), "created": len(created),
                    "alive_created": sum(1 for c in created if c["ok"]),
                    "dead": len(dead), "skipped": len(skipped), "invalid": len(invalid)},
    }


class BatchCheckBody(BaseModel):
    deep: bool = True


@router.post("/accounts/batch-check")
async def batch_check(body: BatchCheckBody | None = None, db: Session = Depends(db_session)):
    """批量验活：对所有账号执行链路测试（默认含最低消耗真实生成），并刷新状态。"""
    deep = body.deep if body else True
    ids = db.execute(select(Account.id).order_by(Account.id)).scalars().all()
    sem = asyncio.Semaphore(3)  # 并发 3，避免对上游 IP 限流

    async def check_one(acc_id: int) -> dict:
        async with sem:
            s = SessionLocal()
            try:
                a = s.get(Account, acc_id)
                if not a:
                    return {"name": f"#{acc_id}", "ok": False, "detail": "已删除"}
                if not a.cookie_enc and not a.access_token_enc:
                    return {"name": a.name, "ok": False, "detail": "未导入凭据"}
                r = await service.test_account(s, a, deep=deep)
                steps = "；".join(
                    f"{s_['name']}:{'OK' if s_['ok'] else s_['detail']}" for s_ in r.get("steps", []))
                return {"name": a.name, "ok": r.get("ok", False),
                        "detail": steps[:250], "models": len(r.get("models") or []),
                        "subscription": r.get("subscription"),
                        "status": a.status}
            finally:
                s.close()

    results = list(await asyncio.gather(*[check_one(i) for i in ids]))
    alive = sum(1 for x in results if x["ok"])
    log_event(db, "info", "batch_check", {"total": len(results), "alive": alive})
    return {"items": results, "total": len(results), "alive": alive}


@router.post("/accounts/{account_id}/oauth/start")
async def oauth_start(account_id: int, db: Session = Depends(db_session)):
    """生成授权链接（首次使用动态客户端注册）。"""
    acc = db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    cfg = get_config()
    redirect_uri = cfg.PUBLIC_BASE_URL + "/oauth/callback"
    if not acc.client_id:
        reg = await oauth.register_client(redirect_uri)
        acc.client_id = reg["client_id"]
        acc.client_secret_enc = security.encrypt(reg["client_secret"])
        db.commit()
    meta = await oauth.discover()
    state = secrets.token_urlsafe(24)
    url, verifier = oauth.build_authorize_url(acc.client_id, state, redirect_uri, meta)
    # 复用未完成流程可省，直接新建
    db.add(OAuthFlow(state=state, account_id=acc.id, code_verifier=verifier, redirect_uri=redirect_uri))
    db.commit()
    return {"authorize_url": url, "redirect_uri": redirect_uri}


@router.post("/accounts/{account_id}/oauth/refresh")
async def oauth_refresh(account_id: int, db: Session = Depends(db_session)):
    acc = db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    try:
        await service.refresh_account_token(db, acc)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    return {"ok": True, "token_expires_at": acc.token_expires_at}


@router.post("/accounts/{account_id}/test")
async def test_account(account_id: int, deep: bool = True, db: Session = Depends(db_session)):
    acc = db.get(Account, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    result = await service.test_account(db, acc, deep=deep)
    log_event(db, "info" if result["ok"] else "warn", "account_test",
              {"account": acc.name, "ok": result["ok"]}, account_id=acc.id)
    return result


# ---------------- 模型 ----------------

class ModelBody(BaseModel):
    model_id: Optional[str] = None
    display_name: Optional[str] = None
    mcp_tool: Optional[str] = None
    mtype: Optional[str] = None
    enabled: Optional[bool] = None
    estimated_cost: Optional[float] = None
    timeout_seconds: Optional[int] = None
    param_template: Optional[dict] = None
    # 参考素材上限: {"image":30,"video":10,"audio":10}（不传则保留；传 {} 恢复官方目录默认）
    ref_limits: Optional[dict] = None
    # 生成能力上限（分辨率/时长，按模型放开）:
    # {"resolutions":[480,720,1080],"durations":[5,8,...,30],"min_duration":2,"max_duration":30}
    # 不传则保留；传 {} 恢复内置实测覆写/默认（如 Seedance 2.5 -> 1080p + 30s）
    gen_limits: Optional[dict] = None


@router.get("/models")
def list_models(db: Session = Depends(db_session)):
    from ..service import gen_limits_of, gen_limits_text_of
    rows = db.execute(select(ModelEntry).order_by(ModelEntry.enabled.desc(), ModelEntry.model_id)).scalars().all()
    items = []
    for m in rows:
        d = m.to_dict()
        d["gen_limits_text"] = gen_limits_text_of(m)
        d["gen_limits_effective"] = gen_limits_of(m)
        items.append(d)
    return {"items": items}


@router.post("/models")
def create_model(body: ModelBody, db: Session = Depends(db_session)):
    if not body.model_id or not body.mcp_tool:
        raise HTTPException(400, "model_id 与 mcp_tool 必填")
    exists = db.execute(select(ModelEntry).where(ModelEntry.model_id == body.model_id)).scalar_one_or_none()
    if exists:
        raise HTTPException(400, "model_id 已存在")
    m = ModelEntry(model_id=body.model_id, mcp_tool=body.mcp_tool,
                   display_name=body.display_name or body.model_id,
                   mtype=body.mtype if body.mtype in ("image", "video", "other") else "other",
                   enabled=bool(body.enabled), estimated_cost=body.estimated_cost or 0,
                   timeout_seconds=max(30, body.timeout_seconds or 600))
    if body.param_template is not None:
        m.param_template_text = JSONText.dump(body.param_template)
    if body.ref_limits:
        from ..service import normalize_ref_limits
        m.ref_limits_text = JSONText.dump(normalize_ref_limits(body.ref_limits))
    if body.gen_limits:
        from ..service import normalize_gen_limits
        lim = normalize_gen_limits(body.gen_limits)
        lim["_from"] = "manual"
        m.gen_limits_text = JSONText.dump(lim)
    db.add(m)
    db.commit()
    from ..service import gen_limits_of, gen_limits_text_of
    d = m.to_dict()
    d["gen_limits_text"] = gen_limits_text_of(m)
    d["gen_limits_effective"] = gen_limits_of(m)
    return d


@router.patch("/models/{model_id_num}")
def patch_model(model_id_num: int, body: ModelBody, db: Session = Depends(db_session)):
    m = db.get(ModelEntry, model_id_num)
    if not m:
        raise HTTPException(404, "模型不存在")
    if body.model_id is not None:
        m.model_id = body.model_id
    if body.display_name is not None:
        m.display_name = body.display_name
    if body.mcp_tool is not None:
        m.mcp_tool = body.mcp_tool
    if body.mtype is not None and body.mtype in ("image", "video", "other"):
        m.mtype = body.mtype
    if body.enabled is not None:
        m.enabled = body.enabled
    if body.estimated_cost is not None:
        m.estimated_cost = max(0.0, body.estimated_cost)
    if body.timeout_seconds is not None:
        m.timeout_seconds = max(30, body.timeout_seconds)
    if body.param_template is not None:
        m.param_template_text = JSONText.dump(body.param_template)
    if body.ref_limits is not None:
        # 传空对象 => 清空覆写，回到官方目录/内置默认
        from ..service import normalize_ref_limits
        if not body.ref_limits:
            m.ref_limits_text = ""
        else:
            cur = {k: v for k, v in (m.ref_limits or {}).items()
                   if k in ("image", "video", "audio")}
            cur.update({k: v for k, v in body.ref_limits.items()
                        if k in ("image", "video", "audio", "total")})
            m.ref_limits_text = JSONText.dump(normalize_ref_limits(cur))
    if body.gen_limits is not None:
        # 传空对象 => 清空覆写，回到内置实测覆写/默认
        from ..service import normalize_gen_limits
        if not body.gen_limits:
            m.gen_limits_text = ""
        else:
            cur = {k: v for k, v in (m.gen_limits or {}).items()
                   if k in ("resolutions", "durations", "min_duration", "max_duration")}
            cur.update({k: v for k, v in body.gen_limits.items()
                        if k in ("resolutions", "durations", "min_duration", "max_duration")})
            lim = normalize_gen_limits(cur)
            lim["_from"] = "manual"
            m.gen_limits_text = JSONText.dump(lim)
    db.commit()
    from ..service import gen_limits_of, gen_limits_text_of
    d = m.to_dict()
    d["gen_limits_text"] = gen_limits_text_of(m)
    d["gen_limits_effective"] = gen_limits_of(m)
    return d


@router.delete("/models/{model_id_num}")
def delete_model(model_id_num: int, db: Session = Depends(db_session)):
    m = db.get(ModelEntry, model_id_num)
    if not m:
        raise HTTPException(404, "模型不存在")
    db.delete(m)
    db.commit()
    return {"ok": True}


@router.post("/models/sync")
async def sync_models(account_id: Optional[int] = None, db: Session = Depends(db_session)):
    """同步模型目录：CF 账号走 MCP list_models；CapCut 账号刷新内置模型种子。"""
    from .. import capcut_channel
    acc = None
    if account_id:
        acc = db.get(Account, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
    else:
        # 未指定账号时优先选 CF 账号（模型目录是 CF 概念），没有 CF 再用 CapCut
        acc = db.execute(select(Account).where(Account.status == Account.STATUS_ACTIVE,
                                               Account.provider == "creative-fabrica")
                         .order_by(Account.id)).scalars().first()
        if not acc:
            acc = db.execute(select(Account).where(Account.status == Account.STATUS_ACTIVE)
                             .order_by(Account.id)).scalars().first()
    if not acc:
        raise HTTPException(400, "没有可用账号，请先导入 Cookie 并测试通过")
    try:
        if (acc.provider or "") == "capcut":
            # CapCut：实时拉官方模型目录；失败时回退内置种子
            try:
                catalog = await capcut_channel.fetch_capcut_models(acc)
                stat = capcut_channel.upsert_capcut_catalog(db, catalog)
                stat["note"] = (f"已从 CapCut 官方同步 {stat['models']} 个模型"
                                f"（账号 {acc.name}）；新发现模型默认停用，请在模型页启用")
            except Exception as fe:  # noqa: BLE001
                stat = capcut_channel.sync_capcut_models(db)
                stat["note"] = f"官方目录拉取失败({fe})，已回退内置种子（账号 {acc.name}）"
        else:
            stat = await service.sync_account_models(db, acc)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"同步失败: {e}")
    log_event(db, "info", "models_sync",
              {"account": acc.name, **{k: stat[k] for k in ("imported", "updated", "models")}},
              account_id=acc.id)
    return stat


# ---------------- API Key ----------------

class KeyBody(BaseModel):
    name: str = ""
    daily_limit: int = 0
    enabled_models: list[str] = []
    capcut_link_mode: str = Field(default="official",
                                  description="CapCut 渠道成片链接: official=官链(不改成片下载链接) / r2=官转(转存 R2 桶返回 R2 链接)")
    note: str = ""


class KeyPatch(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    daily_limit: Optional[int] = None
    enabled_models: Optional[list[str]] = None
    capcut_link_mode: Optional[str] = None
    note: Optional[str] = None


def _norm_link_mode(v: str | None) -> str:
    """CapCut 成片链接方式归一: 官链 official / 官转 r2。"""
    v = (v or "").strip().lower()
    if v in ("official", "origin", "direct", "官链", "原始", "原链"):
        return "official"
    if v in ("r2", "transfer", "mirror", "官转", "转存"):
        return "r2"
    return "official"


@router.get("/keys")
def list_keys(db: Session = Depends(db_session)):
    rows = db.execute(select(ApiKey).order_by(ApiKey.id.desc())).scalars().all()
    day_start = time.time() - (time.time() % 86400)
    today_usage = dict(db.execute(select(Generation.api_key_id, func.count(Generation.id)).where(
        Generation.created_at >= day_start).group_by(Generation.api_key_id)).all())
    total_usage = dict(db.execute(select(Generation.api_key_id, func.count(Generation.id))
                                  .group_by(Generation.api_key_id)).all())
    return {"items": [{**k.to_dict(), "today_used": today_usage.get(k.id, 0),
                       "total_used": total_usage.get(k.id, 0)} for k in rows]}


@router.post("/keys")
def create_key(body: KeyBody, db: Session = Depends(db_session)):
    full_key, key_hash, key_prefix, _ = security.generate_api_key()
    k = ApiKey(name=body.name or "未命名", key_hash=key_hash, key_prefix=key_prefix,
               daily_limit=max(0, body.daily_limit),
               enabled_models_text=JSONText.dump(body.enabled_models or []), note=body.note,
               capcut_link_mode=_norm_link_mode(body.capcut_link_mode))
    db.add(k)
    db.commit()
    log_event(db, "info", "key_created",
              {"name": k.name, "prefix": key_prefix, "capcut_link_mode": k.capcut_link_mode},
              key_prefix=key_prefix)
    # 完整 Key 仅此一次返回
    return {**k.to_dict(), "key": full_key}


@router.patch("/keys/{key_id}")
def patch_key(key_id: int, body: KeyPatch, db: Session = Depends(db_session)):
    k = db.get(ApiKey, key_id)
    if not k:
        raise HTTPException(404, "密钥不存在")
    if body.name is not None:
        k.name = body.name
    if body.enabled is not None:
        k.enabled = body.enabled
    if body.daily_limit is not None:
        k.daily_limit = max(0, body.daily_limit)
    if body.enabled_models is not None:
        k.enabled_models_text = JSONText.dump(body.enabled_models)
    if body.capcut_link_mode is not None:
        k.capcut_link_mode = _norm_link_mode(body.capcut_link_mode)
    if body.note is not None:
        k.note = body.note
    db.commit()
    return k.to_dict()


@router.delete("/keys/{key_id}")
def delete_key(key_id: int, db: Session = Depends(db_session)):
    k = db.get(ApiKey, key_id)
    if not k:
        raise HTTPException(404, "密钥不存在")
    prefix = k.key_prefix
    db.delete(k)
    db.commit()
    log_event(db, "info", "key_deleted", {"prefix": prefix}, key_prefix=prefix)
    return {"ok": True}


# ---------------- 任务 ----------------

@router.get("/generations")
def list_generations(status: str = "", model: str = "", key_name: str = "", gen_id: str = "",
                     page: int = 1, size: int = 20, db: Session = Depends(db_session)):
    q = select(Generation).order_by(Generation.id.desc())
    if status:
        q = q.where(Generation.status == status)
    if model:
        q = q.where(Generation.model_id == model)
    if key_name:
        q = q.where(Generation.key_name.contains(key_name))
    if gen_id:
        q = q.where(Generation.gen_id == gen_id.strip())
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar() or 0
    rows = db.execute(q.offset((page - 1) * size).limit(min(size, 100))).scalars().all()
    return {"total": total, "items": [g.to_dict() for g in rows]}


@router.get("/generations/{gen_id}")
def generation_detail(gen_id: str, db: Session = Depends(db_session)):
    g = db.execute(select(Generation).where(Generation.gen_id == gen_id)).scalar_one_or_none()
    if not g:
        raise HTTPException(404, "任务不存在")
    return g.to_dict()


@router.post("/generations/{gen_id}/cancel")
def cancel_generation(gen_id: str, db: Session = Depends(db_session)):
    g = db.execute(select(Generation).where(Generation.gen_id == gen_id)).scalar_one_or_none()
    if not g:
        raise HTTPException(404, "任务不存在")
    if g.status != Generation.STATUS_QUEUED:
        raise HTTPException(400, "仅排队中的任务可以取消")
    g.status = Generation.STATUS_CANCELLED
    g.finished_at = now()
    db.commit()
    return g.to_dict(include_result=False)


@router.post("/generations/{gen_id}/retry")
def retry_generation(gen_id: str, db: Session = Depends(db_session)):
    g = db.execute(select(Generation).where(Generation.gen_id == gen_id)).scalar_one_or_none()
    if not g:
        raise HTTPException(404, "任务不存在")
    if g.status not in (Generation.STATUS_FAILED, Generation.STATUS_CANCELLED):
        raise HTTPException(400, "仅失败/已取消的任务可以重试")
    g.status = Generation.STATUS_QUEUED
    g.error = ""
    g.retry_count = 0
    g.account_id = None
    g.account_name = ""
    g.started_at = 0
    g.finished_at = 0
    db.commit()
    return g.to_dict(include_result=False)


# ---------------- 积分流水 / 日志 ----------------

@router.get("/transactions")
def list_transactions(account_id: int = 0, page: int = 1, size: int = 20,
                      db: Session = Depends(db_session)):
    q = select(CoinTransaction).order_by(CoinTransaction.id.desc())
    if account_id:
        q = q.where(CoinTransaction.account_id == account_id)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar() or 0
    rows = db.execute(q.offset((page - 1) * size).limit(min(size, 100))).scalars().all()
    return {"total": total, "items": [t.to_dict() for t in rows]}


@router.get("/logs")
def list_logs(level: str = "", event: str = "", page: int = 1, size: int = 50,
              db: Session = Depends(db_session)):
    from ..models import SystemLog
    q = select(SystemLog).order_by(SystemLog.id.desc())
    if level:
        q = q.where(SystemLog.level == level)
    if event:
        q = q.where(SystemLog.event.contains(event))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar() or 0
    rows = db.execute(q.offset((page - 1) * size).limit(min(size, 200))).scalars().all()
    return {"total": total, "items": [l.to_dict() for l in rows]}


# ---------------- 设置 ----------------

DEFAULT_SETTINGS = {
    "default_timeout": 600,
    "max_retries": 2,
    "notes": "任务默认超时与重试次数；MCP/OAuth 端点由服务配置，可在「工具同步」中验证连通性。",
}


@router.get("/settings")
def get_settings(db: Session = Depends(db_session)):
    result = dict(DEFAULT_SETTINGS)
    for s in db.execute(select(Setting)).scalars().all():
        if s.key in result:
            try:
                result[s.key] = int(s.value_text)
            except ValueError:
                pass
    return result


class SettingsBody(BaseModel):
    default_timeout: Optional[int] = None
    max_retries: Optional[int] = None


@router.put("/settings")
def put_settings(body: SettingsBody, db: Session = Depends(db_session)):
    if body.default_timeout is not None:
        db.merge(Setting(key="default_timeout", value_text=str(max(30, body.default_timeout))))
    if body.max_retries is not None:
        db.merge(Setting(key="max_retries", value_text=str(max(0, min(5, body.max_retries)))))
    db.commit()
    return get_settings(db)
