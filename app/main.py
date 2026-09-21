"""CF NewAPI Gateway — 应用入口。"""
import asyncio
import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from . import config, oauth, security
from .config import get_config
from .db import SessionLocal, init_db
from .models import Account, OAuthFlow, log_event, now
from .routers import admin as admin_router
from .routers import openai as openai_router
from .worker import start_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

cfg = get_config()
os.makedirs(cfg.DATA_DIR, exist_ok=True)
config.SECRET_KEY = config.load_secret_key()
security.init_security(config.SECRET_KEY)
init_db()

app = FastAPI(title="CF NewAPI Gateway", version=cfg.CLIENT_VERSION,
              docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

app.include_router(admin_router.build_login_router())
app.include_router(admin_router.router)
app.include_router(openai_router.router)


@app.get("/health")
async def health():
    return {"ok": True, "service": "cf-gateway", "version": cfg.CLIENT_VERSION}


# ---------------- OAuth 浏览器回调 ----------------

_CALLBACK_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>授权结果</title><style>
body{{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;background:#f5f7fa;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.card{{background:#fff;border-radius:12px;box-shadow:0 2px 16px rgba(0,0,0,.08);
padding:36px 44px;max-width:460px;text-align:center}}
h2{{margin:0 0 12px;font-size:20px}}
.ok{{color:#16a34a}}.fail{{color:#dc2626}}
p{{color:#4b5563;line-height:1.7;margin:8px 0}}
a{{color:#2563eb;text-decoration:none;font-weight:600}}
</style></head><body><div class="card">{icon}<h2>{title}</h2><p>{msg}</p>
<p><a href="../admin/">返回管理后台</a></p></div></body></html>"""


@app.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(request: Request):
    params = request.query_params
    error = params.get("error") or ""
    code = params.get("code") or ""
    state = params.get("state") or ""

    def render(ok: bool, title: str, msg: str):
        icon = '<div style="font-size:44px" class="ok">✓</div>' if ok else \
               '<div style="font-size:44px" class="fail">✕</div>'
        return HTMLResponse(_CALLBACK_HTML.format(icon=icon, title=title, msg=msg))

    if error:
        return render(False, "授权失败", f"授权服务器返回错误: {error} "
                     f"{params.get('error_description', '')}")
    if not code or not state:
        return render(False, "参数错误", "回调缺少 code / state 参数")

    db = SessionLocal()
    try:
        flow = db.execute(select(OAuthFlow).where(OAuthFlow.state == state)).scalar_one_or_none()
        if not flow:
            return render(False, "状态无效", "未找到对应的授权流程（state 过期或不匹配），请重新发起授权")
        if flow.status == "done":
            return render(True, "已完成", "该授权流程已处理过，无需重复操作。")
        account = db.get(Account, flow.account_id)
        if not account:
            flow.status = "error"
            flow.error = "账号不存在"
            db.commit()
            return render(False, "账号不存在", "授权流程关联的账号已被删除")

        try:
            data = await oauth.exchange_code(
                code, flow.code_verifier, flow.redirect_uri, account.client_id,
                security.decrypt(account.client_secret_enc))
        except oauth.OAuthError as e:
            flow.status = "error"
            flow.error = str(e)[:500]
            db.commit()
            return render(False, "令牌交换失败", str(e))

        account.access_token_enc = security.encrypt(data.get("access_token", ""))
        if data.get("refresh_token"):
            account.refresh_token_enc = security.encrypt(data["refresh_token"])
        account.token_expires_at = now() + int(data.get("expires_in", 3600))
        account.scope = data.get("scope", account.scope)
        account.status = Account.STATUS_ACTIVE
        account.last_error = ""
        account.mcp_session_id = ""
        flow.status = "done"
        db.commit()
        log_event(db, "info", "oauth_authorized",
                  {"account": account.name, "scope": account.scope}, account_id=account.id)
        return render(True, "授权成功",
                      f"账号「{account.name}」已完成 Creative Fabrica OAuth 授权，"
                      f"即将自动同步可用工具。返回后台点击「测试」可验证连通性。")
    finally:
        db.close()


# ---------------- 管理后台静态页 ----------------

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


@app.get("/", response_class=HTMLResponse)
@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/", response_class=HTMLResponse)
async def admin_page():
    with open(os.path.join(_STATIC_DIR, "admin.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("未处理异常 %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={
        "error": {"message": "服务内部错误，请稍后重试", "type": "api_error"}})


@app.on_event("startup")
async def on_startup():
    seed_capcut_models()
    await start_worker()
    if not cfg.ADMIN_PASSWORD:
        log.warning("未设置 ADMIN_PASSWORD，管理后台使用默认密码（仅建议测试环境）")
    log.info("CF NewAPI Gateway 已启动: base_url=%s mcp=%s", cfg.PUBLIC_BASE_URL, cfg.MCP_ENDPOINT)


def seed_capcut_models():
    """首次启动播种 CapCut 直连模型（已存在则跳过，管理员可在后台改配置/启停）。

    ⚠️ 种子清单**只允许有一份**：`capcut_channel.CAPCUT_MODEL_SEEDS`。
    本函数以前自带一份一模一样的本地列表 → 加新模型（如 Seedance 2.5）时只改了目录同步那份，
    全新部署启动后 `/v1/models` 里就查不到 2.5。现在直接复用同一常量，杜绝再次漂移。
    """
    from .capcut_channel import CAPCUT_MODEL_SEEDS
    from .models import JSONText, ModelEntry
    db = SessionLocal()
    try:
        created = 0
        for mid, upstream, cost, tpl in CAPCUT_MODEL_SEEDS:
            exists = db.execute(select(ModelEntry).where(
                ModelEntry.model_id == mid)).scalar_one_or_none()
            if exists:
                continue
            db.add(ModelEntry(
                model_id=mid, display_name=f"CapCut {upstream}", provider="capcut",
                mcp_tool=upstream, mtype="video", enabled=True, estimated_cost=cost,
                timeout_seconds=900, auto_registered=False,
                description=f"CapCut 直连通道（{upstream}，common_task 协议）",
                param_template_text=JSONText.dump(tpl)))
            created += 1
        if created:
            db.commit()
            log.info("已播种 %d 个 CapCut 直连模型", created)
    finally:
        db.close()
