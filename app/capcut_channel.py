"""CapCut 直连通道（seedance / common_task）provider 胶水层。

职责:
  - CapCut Cookie 导入校验与身份指纹（账号去重）
  - 从网关账号(加密存储的 Cookie JSON)构建 DirectTaskClient
  - OpenAI 风格参数 -> create_video_task 入参（归一分辨率/画幅/时长）
  - 提交/轮询（阻塞 IO 全部在线程池执行，不阻塞事件循环）
  - 错误分类（auth / billing / upstream），复用 CF 网关的账号状态机
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor

from .config import get_config

log = logging.getLogger("cf_gateway.capcut")

# 参考素材并发上传线程数：Seedance 2.5 参考模式最多 30 图 + 10 视频 + 10 音频，
# 串行「下载 -> 上传」会把提交阶段拖到十几分钟（撞模型超时），故有界并发。
try:
    _REF_UPLOAD_WORKERS = max(1, min(8, int(os.environ.get("CAPCUT_REF_UPLOAD_WORKERS") or 4)))
except ValueError:
    _REF_UPLOAD_WORKERS = 4

PROVIDER = "capcut"

# 分辨率/时长默认档（**按模型可放开**，见 service.gen_limits_of）：
#   - 内置默认: 480p/720p 两档，时长 2-15s（保持长期既有行为）
#   - 内置实测覆写: Seedance 2.5 -> 480p/720p/1080p + 5/8/10/12/15/18/20/25/30s
#   - 后台可逐模型改（ModelEntry.gen_limits）
# ratio 枚举 16:9/4:3/1:1/3:4/9:16/21:9 + fit(按参考素材自适应)
SUPPORTED_RESOLUTIONS = (480, 720)
SUPPORTED_RATIOS = [("21:9", 21 / 9), ("16:9", 16 / 9), ("4:3", 4 / 3),
                    ("1:1", 1.0), ("3:4", 3 / 4), ("9:16", 9 / 16)]
MAX_DURATION_S = 15.0
MIN_DURATION_S = 2.0

TERMINAL_OK = ("succeed", "success", "completed")
TERMINAL_FAIL = ("failed", "error", "expired")


def normalize_resolution(res, allowed=None) -> int:
    """归一到允许的分辨率档（默认 480/720），四舍五入到最近档"""
    tiers = tuple(sorted(int(v) for v in (allowed or SUPPORTED_RESOLUTIONS))) or SUPPORTED_RESOLUTIONS
    try:
        n = int(float(str(res).strip().rstrip("pP")))
    except (ValueError, TypeError):
        return min(tiers)
    return min(tiers, key=lambda v: (abs(v - n), v))


def clamp_duration(duration, durations=None, min_s=None, max_s=None) -> float:
    """时长归一到模型允许范围：先按 min/max 夹取，再吸附到最近档位（若有档位表）。"""
    lo = MIN_DURATION_S if min_s is None else float(min_s)
    hi = MAX_DURATION_S if max_s is None else float(max_s)
    if hi < lo:
        hi = lo
    try:
        d = float(duration)
    except (TypeError, ValueError):
        d = 5.0
    d = max(lo, min(hi, d))
    tiers = sorted(float(t) for t in (durations or []))
    if tiers:
        snapped = min(tiers, key=lambda t: (abs(t - d), t))
        if abs(snapped - d) > 1e-6:
            log.info("时长 %.3gs 不在模型档位内，吸附到最近档 %.3gs", d, snapped)
        d = snapped
    return d


def normalize_ratio(ratio: str) -> str:
    """'16:9'/'9：16' 直通; 'fit' 直通; 'WxH' 像素吸附到最近比例; 失败默认 9:16"""
    r = (ratio or "9:16").strip().lower().replace("：", ":").replace("*", "x")
    if r == "fit":
        return "fit"
    try:
        if "x" in r:
            w, h = (int(v) for v in r.split("x"))
            target = w / h
        else:
            w, h = (int(v) for v in r.split(":"))
            target = w / h
        return min(SUPPORTED_RATIOS, key=lambda kv: abs(kv[1] - target))[0]
    except Exception:
        return "9:16"


# size 里可能同时携带分辨率与画幅: "480p 16:9" / "480p 16:9 横屏" / "864x496" / "1280x720"
_SIZE_PX = re.compile(r"(\d{3,4})\s*[x\*]\s*(\d{3,4})")
_SIZE_P = re.compile(r"(\d{3,4})\s*p", re.I)
_SIZE_RATIO = re.compile(r"\d{1,2}\s*[:：]\s*\d{1,2}")


def split_size(size, resolutions=None) -> tuple:
    """从 OpenAI Sora 风格 size 中拆出 (分辨率档, 画幅或 None)。

    支持: '480p' / '480p 16:9' / '480p 16:9 横屏' / '720x1280' / '1280x720'。
    分辨率档吸附到 ``resolutions``（模型允许的档位，缺省 480/720）。
    画幅无法判断时返回 None，交由调用方回退到显式参数或模型模板。
    """
    s = str(size or "").strip().lower().replace("×", "x")
    if not s:
        return None, None
    m = _SIZE_PX.search(s)
    if m:                                   # 像素写法: 比例由像素推, 分辨率取短边(高度侧)
        w, h = int(m.group(1)), int(m.group(2))
        return normalize_resolution(min(w, h), resolutions), normalize_ratio(f"{w}x{h}")
    res = (normalize_resolution(_SIZE_P.search(s).group(1), resolutions)
           if _SIZE_P.search(s) else None)
    mr = _SIZE_RATIO.search(s)
    return res, (normalize_ratio(mr.group(0)) if mr else None)


# ---------------- Cookie 导入 ----------------

# 脚本/插件导出的「外壳」字段：这些键是元数据或另一种编码，不是 cookie 名。
_COOKIE_WRAPPER_KEYS = ("cookies", "playwright_cookies", "netscape_cookies",
                        "capcut")  # capcut=账号创建脚本导出（cookie 嵌在 .capcut.cookies 下）
_COOKIE_HEADER_KEYS = ("cookie_header", "cookieHeader", "raw_cookie", "cookie")


def _strip_bom(text: str) -> str:
    """去掉 BOM / 零宽字符（Windows 记事本另存 UTF-8 with BOM 的常见坑）。"""
    return (text or "").lstrip("\ufeff\u200b \t\r\n")


def _header_to_jar(header: str) -> dict:
    """'k=v; k2=v2' -> {"k": "v", ...}（保留值里的 '='）。"""
    jar: dict[str, str] = {}
    for pair in (header or "").split(";"):
        if "=" in pair:
            k, v = pair.strip().split("=", 1)
            if k.strip():
                jar[k.strip()] = v.strip()
    return jar


def _cookie_names(items) -> set:
    """集合里出现的 cookie 名（兼容 list[dict] 与 dict 两种形态）。"""
    if isinstance(items, dict):
        return {str(k) for k in items}
    if isinstance(items, list):
        return {str(c.get("name")) for c in items if isinstance(c, dict) and c.get("name")}
    return set()


def _cookie_candidates(obj, depth: int = 0) -> list:
    """从任意结构里收集「可能的 cookie 集合」，按可信度排序。

    脚本导出的 cookie 文件常带外壳（account/exported_at/login_host + 多种编码），
    这里把真正的 cookie 集合挑出来，避免把元数据键误当成 cookie 名。
    """
    if depth > 3 or not isinstance(obj, dict):
        return []
    cands: list = []
    for key in _COOKIE_WRAPPER_KEYS:
        v = obj.get(key)
        if isinstance(v, list) and v:
            cands.append(v)                                    # 标准导出数组
        elif isinstance(v, dict) and v:
            cands.append(v)                                    # {name: value} 键值表
            cands.extend(_cookie_candidates(v, depth + 1))      # 可能还套了一层外壳
    for key in _COOKIE_HEADER_KEYS:
        v = obj.get(key)
        if isinstance(v, str) and "=" in v:
            jar = _header_to_jar(v)
            if jar:
                cands.append(jar)
    return cands


def _pick_cookie_candidate(data) -> object:
    """从候选集合里挑最佳 cookie 集合：必须含 sessionid；多份都含时取
    「风控关键 cookie 命中数」最多的那份，避免选中只有 sessionid/uid_tt 的
    精简表而丢掉 d_ticket/ttwid/msToken 等风控票据。无任何含 sessionid
    的候选时回退 cands[0]（由调用方走「缺少 sessionid」报错路径）。"""
    cands = _cookie_candidates(data)
    if not cands:
        return data
    known = {n for n, *_ in RISK_COOKIES}

    def _score(c) -> int:
        if not isinstance(c, dict):
            return -1
        try:
            names = _cookie_names(c)
        except Exception:
            return -1
        if "sessionid" not in names:
            return -1
        return len(names & known)

    best = max(cands, key=_score)
    return best if _score(best) >= 0 else cands[0]


def normalize_capcut_cookie(raw: str) -> str:
    """浏览器/脚本导出的 CapCut Cookie -> 规范化 JSON 字符串（cookie 对象数组）。

    支持（自动跳过元数据外壳，优先取含 sessionid 的那份）:
      1. 浏览器插件导出的对象数组 ``[{name,value,domain,...}]``
      2. 脚本导出外壳 ``{account,exported_at,cookies:{...},playwright_cookies:[...],
         cookie_header:"k=v; ..."}``
      3. ``{"cookies": [...]}`` / ``{"cookies": {...}}`` 包装
      4. 简单键值对象 ``{"sessionid": "...", ...}``
      5. ``k=v; k2=v2`` 头字符串
    必须包含 sessionid（登录态主凭证），否则拒绝并回显解析到的名字。
    """
    raw = _strip_bom(raw)
    if not raw:
        raise ValueError("Cookie 内容为空")
    try:
        data = json.loads(raw)
    except Exception:
        jar = _header_to_jar(raw)
        if not jar:
            raise ValueError("无法解析 Cookie（支持浏览器导出 JSON、脚本导出外壳、"
                             "键值对象或 k=v; 头字符串）")
        data = jar
    if isinstance(data, dict):
        data = _pick_cookie_candidate(data)
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        data = data["cookies"]
    if isinstance(data, dict):
        data = [{"name": k, "value": v, "domain": ".capcut.com", "path": "/"}
                for k, v in data.items() if v]
    if not isinstance(data, list) or not data:
        raise ValueError("Cookie JSON 为空或格式不支持")
    names = {c.get("name") for c in data if isinstance(c, dict)}
    if "sessionid" not in names:
        found = ", ".join(sorted(str(n) for n in names if n))[:200] or "无"
        raise ValueError("Cookie 中缺少 sessionid（请确认导出的是 www.capcut.com 登录态）；"
                         f"本次解析到的 cookie 名：{found}")
    return json.dumps(data, ensure_ascii=False)


def capcut_cookie_identity(cookie_json: str) -> str:
    """账号身份指纹（uid_tt 优先, 退回 sessionid），用于导入去重。"""
    try:
        data = json.loads(cookie_json or "[]")
    except Exception:
        return ""
    vals: dict[str, str] = {}
    for c in (data if isinstance(data, list) else []):
        if isinstance(c, dict) and c.get("name"):
            vals[c["name"]] = str(c.get("value") or "")
    return vals.get("uid_tt") or vals.get("sessionid") or ""


# ---------------- Cookie 风控体检 ----------------
# 2026-09-15 实测（两次修正后的结论）：`common_task/new`（提交生成）被 shark 风控拦截（ret=-6）
#   · 与 sign 无关（sign 可本地现签）
#   · 也**不只**取决于 Cookie：账号 #3 带着 d_ticket 仍被拦，加上 URL 查询串上的反爬参数
#     `X-Gnarly` 后同一请求立刻 ret=1000 —— 见 app/capcut_antibot.py
# 所以这里的体检是**必要条件**筛查，不是充分判据：缺关键项一定有问题，齐了也不保证过。
RISK_COOKIES = (
    ("sessionid", "critical", "登录态主凭证"),
    ("uid_tt", "critical", "账号标识（身份指纹）"),
    ("d_ticket", "high", "风控票据（passport 登录时下发；必要但不充分，见 capcut_antibot）"),
    ("ttwid", "medium", "设备/会话 ID（风控基线）"),
    ("msToken", "medium", "风控 token（请求头可带可不带）"),
    ("s_v_web_id", "medium", "Web 验证 ID"),
    ("fpk1", "medium", "浏览器指纹 1"),
    ("fpk2", "medium", "浏览器指纹 2"),
    ("x-web-secsdk-uid", "low", "secsdk 运行过留下的 uid"),
    ("odin_tt", "low", "设备令牌"),
    ("uifid", "low", "用户中间态 ID"),
    ("passport_csrf_token", "low", "登录 CSRF token"),
)


def _capcut_cookie_list(cookie_json) -> list:
    try:
        data = json.loads(cookie_json or "[]") if isinstance(cookie_json, str) else (cookie_json or [])
    except Exception:
        return []
    if isinstance(data, dict):
        data = _pick_cookie_candidate(data)
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        return data["cookies"]
    if isinstance(data, dict):
        return [{"name": k, "value": v, "domain": ".capcut.com", "path": "/"} for k, v in data.items()]
    return data if isinstance(data, list) else []


def _mask_cookie(v: str) -> str:
    v = str(v or "")
    if len(v) <= 10:
        return v[:3] + "…"
    return v[:6] + "…" + v[-4:]


def cookie_health(cookie_json) -> dict:
    """CapCut Cookie 风控体检：列出关键 cookie 的有无 + 是否能过提交风控的预判。"""
    items = _capcut_cookie_list(cookie_json)
    vals = {}
    for c in items:
        if isinstance(c, dict) and c.get("name"):
            vals[str(c["name"])] = str(c.get("value") or "")
    rows, missing_critical, missing_high = [], [], []
    for name, level, why in RISK_COOKIES:
        has = bool(vals.get(name))
        rows.append({"name": name, "level": level, "why": why, "present": has,
                     "value_masked": (_mask_cookie(vals[name]) if has else None)})
        if not has and level == "critical":
            missing_critical.append(name)
        if not has and level == "high":
            missing_high.append(name)
    can_submit = not missing_critical and not missing_high
    if missing_critical:
        hint = f"缺少 {', '.join(missing_critical)}：Cookie 已失效，需要重新导出"
    elif missing_high:
        hint = (f"缺少 {', '.join(missing_high)}：提交生成可能被 shark 风控拦截（ret=-6）"
                "——在该账号浏览器里正常使用一次（可通过人机校验），再把 d_ticket 补进来。"
                "注：补齐后仍被拦时看 X-Gnarly（app/capcut_antibot.py）")
    else:
        hint = "关键项齐全（必要条件满足）；若仍 ret=-6，检查反爬参数 X-Gnarly 是否生效"
    return {"cookie_count": len(vals), "rows": rows,
            "missing_critical": missing_critical, "missing_high": missing_high,
            "can_submit_likely": can_submit, "hint": hint,
            "names": sorted(vals.keys())}


def patch_capcut_cookies(cookie_json, patch: dict) -> str:
    """在不重新导入整份 Cookie 的前提下，增/改单个 cookie（如补 d_ticket）。"""
    items = _capcut_cookie_list(cookie_json)
    patch = {str(k): str(v) for k, v in (patch or {}).items() if v not in (None, "")}
    if not patch:
        return json.dumps(items, ensure_ascii=False)
    by_name = {}
    for c in items:
        if isinstance(c, dict) and c.get("name"):
            by_name[str(c["name"])] = c
    for k, v in patch.items():
        if k in by_name:
            by_name[k]["value"] = v
        else:
            items.append({"name": k, "value": v, "domain": ".capcut.com", "path": "/"})
    return json.dumps(items, ensure_ascii=False)


# ---------------- 客户端构建 ----------------

def _tmp_dir() -> str:
    d = os.path.join(get_config().DATA_DIR, "tmp")
    os.makedirs(d, exist_ok=True)
    return d


def client_from_account(account):
    """从账号（加密 Cookie JSON）构建 DirectTaskClient。

    PureUploader / DirectTaskClient 在 __init__ 时即读完 Cookie；临时文件**不删除**
    （按内容哈希命名、同内容复用，避免产生大量「创建+删除」churn，见 `_cookie_file`）。
    账号可携带自己的一份签名（capcut_signs_text）：auto 模式下优先用账号存的，
    没存就按当前时间现签（签名算法已逆向，见 app/capcut_signs.py）。
    """
    from . import security
    from .capcut_direct_task import DirectTaskClient
    from .capcut_signs import normalize_signs
    raw = security.decrypt(account.cookie_enc)
    if not raw:
        raise ValueError("账号尚未导入 CapCut Cookie")
    signs = normalize_signs(getattr(account, "capcut_signs", None) or {})
    path = _cookie_file(account, raw)
    return DirectTaskClient(path, signs=signs)


def _cookie_file(account, raw: str) -> str:
    """把解密后的 Cookie 落到临时文件，返回文件路径。

    ⚠️ **刻意不删除文件**（历史实现是 mkstemp + finally unlink）：
    每次建客户端都「创建+删除」一对临时文件，在批量跑脚本/并发调度时会快速累积成
    成百上千次删除，既浪费 I/O，也会撞上运行环境的批量删除保护而直接中断进程。
    现在改成**按内容哈希命名**：同一份 Cookie 永远复用同一个文件；Cookie 变了就是
    新文件名，无需覆盖、无需删除。
    """
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    path = os.path.join(_tmp_dir(), f"capcut-{account.id}-{h}.json")
    if os.path.exists(path):
        return path
    tmp = f"{path}.{os.getpid()}.part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(raw)
    os.replace(tmp, path)  # 目标此前不存在 ⇒ 纯建链，不是删除
    return path


def refresh_balance(account) -> float:
    """查积分并写入 account.coin_balance（调用方负责 commit）。阻塞。

    同时更新 ``last_check_at``——选号用它做余额新鲜度 TTL 判断，
    防止「缓存余额虚高把任务分给实际没钱的号」（2026-09-16 实测踩坑）。
    """
    total = float(client_from_account(account).get_user_credit()["total"] or 0)
    account.coin_balance = total
    try:
        import time as _t
        account.last_check_at = _t.time()
    except Exception:  # noqa: BLE001
        pass
    return total


async def refresh_balance_async(account) -> float:
    return await asyncio.to_thread(refresh_balance, account)


# ---------------- 错误分类 ----------------

class CapcutError(Exception):
    def __init__(self, message: str, kind: str = "upstream"):
        super().__init__(message)
        self.kind = kind


_AUTH_KEYS = ("34010105", "1015", "login error", "no login", "not login",
              "relogin", "unauthorized", "session")
_BILLING_KEYS = ("insufficient", "not enough credit", "credit not enough", "余额不足")


def classify_capcut_error(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in _AUTH_KEYS):
        return "auth"
    if any(k in t for k in _BILLING_KEYS):
        return "billing"
    return "upstream"


def wrap_error(e: Exception) -> CapcutError:
    return CapcutError(str(e), classify_capcut_error(str(e)))


# ---------------- 参数构造 ----------------

def build_capcut_args(model, prompt: str, params: dict) -> dict:
    """OpenAI 风格请求 -> create_video_task 入参。模型模板提供默认值，请求参数优先。

    分辨率/时长按**模型能力上限**夹取（``service.gen_limits_of``）：
    内置默认 480/720p 与 2-15s；Seedance 2.5 放开到 1080p 与 30s（档位吸附）。
    """
    from .service import build_reference_files, ref_limits_of, gen_limits_of
    tpl = model.param_template or {}
    opts = params.get("options") if isinstance(params.get("options"), dict) else {}
    lim = gen_limits_of(model)

    def pick(*names, default=None):
        for src in (params, opts):
            for n in names:
                if src.get(n) is not None:
                    return src[n]
        return default

    duration = pick("duration", "duration_seconds", default=tpl.get("duration", 5))
    duration = clamp_duration(duration, lim["durations"], lim["min_duration"], lim["max_duration"])

    # size 可能同时带分辨率与画幅（OpenAI Sora 约定: size="1280x720"、"480p 16:9"）
    size_raw = pick("size", "resolution") or tpl.get("resolution", "480p")
    res_from_size, ratio_from_size = split_size(size_raw, lim["resolutions"])
    resolution = f"{(res_from_size or normalize_resolution(size_raw, lim['resolutions']))}p"
    # 画幅优先级: 显式 aspect_ratio/ratio > size 里内嵌的比例 > 模型模板默认
    explicit_ratio = pick("aspect_ratio", "ratio")
    if explicit_ratio:
        ratio = normalize_ratio(explicit_ratio)
    elif ratio_from_size:
        ratio = ratio_from_size
    else:
        ratio = normalize_ratio(tpl.get("ratio", "16:9"))
    audio = pick("generate_audio", default=tpl.get("generate_audio", True))

    # 参考素材: 复用 CF 的 reference_files 约定（referenceImage/Video/Audio + url）
    prompt, files = build_reference_files(str(prompt or ""), params, limits=ref_limits_of(model))
    return {
        "prompt": prompt,
        "model": model.mcp_tool or "seedance_2.0_mini",
        "resolution": resolution,
        "duration_s": duration,
        "ratio": ratio,
        "generate_audio": bool(audio),
        "ref_files": files or [],     # [{role, alias, url}]
        "gen_limits": lim,            # 实际生效上限（日志/排查用）
    }


# ---------------- 提交 / 轮询 ----------------

_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
_VID_EXT = (".mp4", ".mov", ".webm", ".m4v")
_AUD_EXT = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")
_KNOWN_EXT = _IMG_EXT + _VID_EXT + _AUD_EXT


def _upload_one_ref(cli, f: dict, idx: int, workdir: str) -> dict:
    """下载单个参考素材并上传 CapCut，返回 references 条目"""
    import requests
    from urllib.parse import urlparse
    role_ext = {"referenceImage": "png", "referenceVideo": "mp4", "referenceAudio": "mp3"}
    url = f["url"]
    alias = f.get("alias") or f"ref{idx + 1}"
    r = requests.get(url, timeout=300)
    r.raise_for_status()
    # 优先沿用 URL 自身的已知扩展名（wav/mov/m4a 等），否则退回 role 默认（png/mp4/mp3）；
    # 按扩展名决定走 imageX 还是 VOD，所以这一步直接影响上传通道选择。
    url_ext = os.path.splitext(urlparse(url).path)[1].lower()
    ext = url_ext.lstrip(".") if url_ext in _KNOWN_EXT else role_ext.get(f.get("role"), "")
    path = os.path.join(workdir, f"{alias}.{ext or 'bin'}")
    with open(path, "wb") as w:
        w.write(r.content)
    e = os.path.splitext(path)[1].lower()
    if e in _IMG_EXT:
        return cli.upload_reference_image(path)
    if e in _VID_EXT:
        return cli.upload_reference_video(path)
    return cli.upload_reference_audio(path)


def _download_and_upload_refs(cli, ref_files: list[dict], workdir: str) -> list[dict]:
    """下载参考素材 URL -> 上传 CapCut（imageX / digital_cameo VOD），返回 references 列表。

    返回顺序必须与入参严格一致（提示词的 ``[image1]/[video1]/[audio1]`` 按此下标对齐），
    因此并发时按下标写回占位槽。并发整体失败时退回串行重试一次。
    """
    if not ref_files:
        return []
    workers = max(1, min(_REF_UPLOAD_WORKERS, len(ref_files)))
    ordered: list = [None] * len(ref_files)
    try:
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for idx, ref in ex.map(
                        lambda p: (p[0], _upload_one_ref(cli, p[1], p[0], workdir)),
                        list(enumerate(ref_files))):
                    ordered[idx] = ref
        else:
            for idx, f in enumerate(ref_files):
                ordered[idx] = _upload_one_ref(cli, f, idx, workdir)
    except Exception as e:  # noqa: BLE001
        log.warning("参考素材并发上传失败（%s: %s），退回串行重试", type(e).__name__, e)
        ordered = [None] * len(ref_files)
        for idx, f in enumerate(ref_files):
            ordered[idx] = _upload_one_ref(cli, f, idx, workdir)
    missing = [i for i, v in enumerate(ordered) if v is None]
    if missing:
        raise RuntimeError(f"参考素材上传缺失（下标 {missing}）")
    if workers > 1:
        log.info("参考素材上传完成 %d 个（并发 %d）", len(ordered), workers)
    return ordered


def _submit_sync(account, args: dict) -> tuple[float, dict]:
    """线程池内执行: 构建客户端 -> 查余额 -> 上传参考 -> 创建任务。"""
    cli = client_from_account(account)
    credit_before = float(cli.get_user_credit()["total"] or 0)
    workdir = tempfile.mkdtemp(prefix="capcut-refs-", dir=_tmp_dir())
    refs = _download_and_upload_refs(cli, args["ref_files"], workdir) if args["ref_files"] else []
    nr = cli.create_video_task(
        args["prompt"], model=args["model"], resolution=args["resolution"],
        duration_ms=int(args["duration_s"] * 1000), ratio=args["ratio"],
        generate_audio=args["generate_audio"], references=refs or None)
    return credit_before, nr


async def submit_capcut(account, args: dict) -> tuple[str, str, float]:
    """提交 CapCut 生成任务，返回 (task_id, token, credit_before)。"""
    try:
        credit_before, nr = await asyncio.to_thread(_submit_sync, account, args)
    except CapcutError:
        raise
    except Exception as e:  # noqa: BLE001
        raise wrap_error(e) from e
    data = nr.get("data") or {}
    task = (data.get("tasks") or [{}])[0]
    task_id, token = task.get("id"), task.get("token")
    if task_id and token:
        return task_id, token, credit_before
    ft = (data.get("failed_tasks") or [{}])[0]
    if ft.get("err_code") or ft.get("err_msg"):
        raise CapcutError(f"任务被拒: {ft.get('err_code')} {ft.get('err_msg', '')[:300]}",
                          classify_capcut_error(f"{ft.get('err_code')} {ft.get('err_msg', '')}"))
    raise CapcutError(f"创建任务失败: ret={nr.get('ret')} "
                      f"{json.dumps(nr.get('data') or nr, ensure_ascii=False)[:300]}",
                      classify_capcut_error(json.dumps(nr, ensure_ascii=False)))


def _query_sync(account, task_id: str, token: str) -> tuple:
    cli = client_from_account(account)
    qr = cli.query_task(task_id, token)
    st, url, vid = cli.extract_video(qr)
    tasks = (qr.get("data") or {}).get("tasks") or []
    t = tasks[0] if tasks else {}
    err = f"{t.get('err_code', '')} {t.get('err_msg', '')}".strip()
    return st, url, vid, err, qr


async def poll_capcut(task_id: str, token: str, account) -> tuple:
    """轮询，返回 (status, video_url, vid, err_msg, raw)。"""
    try:
        return await asyncio.to_thread(_query_sync, account, task_id, token)
    except Exception as e:  # noqa: BLE001
        raise wrap_error(e) from e


# ---------------- 模型目录（CapCut 无远端目录，用内置种子 upsert） ----------------

# ⚠️ 对外 model_id 一律用 `sd-` 前缀（**禁止出现上游品牌名**，2026-09-21 运营要求）：
# 客户端看到的模型名、响应体里的 model 字段、后台列表都不得暴露上游。
# 内部一切逻辑仍按 provider="capcut" 分流（accounts/models 表），只在对外字符串上脱敏。
MODEL_ID_PREFIX = "sd-"

CAPCUT_MODEL_SEEDS = [
    # (对外 model_id, 上游 CapCut 模型, 预估积分, 默认模板)
    ("sd-seedance-2.0-mini", "seedance_2.0_mini", 28,
     {"resolution": "480p", "ratio": "9:16", "duration": 5, "generate_audio": True}),
    ("sd-seedance-2.0", "seedance_2.0", 56,
     {"resolution": "720p", "ratio": "9:16", "duration": 5, "generate_audio": True}),
    ("sd-seedance-1.0-fast", "seedance_1.0_fast", 20,
     {"resolution": "480p", "ratio": "16:9", "duration": 5, "generate_audio": True}),
    # Seedance 2.5（r2v 多参考素材通道）。⚠️ 历史上对外 id 混用过下划线与 `capcut-` 前缀，
    # 现统一为 `sd-seedance-2.5`（见 db._migrate_rebrand_model_ids 的存量改名）。
    # estimated_cost 取**最低可跑价**（5s @ 480p = 5×17 = 85 分）当闸门——2.5 计费随时长/分辨率
    # 浮动（17/37/72 分每秒），真实值用 `relay/_capcut_price.py` 现算；设 85 能避免选到连最短片都付不起的号。
    ("sd-seedance-2.5", "seedance_2.5", 85,
     {"duration": 5, "generate_audio": True}),
]


def sync_capcut_models(db) -> dict:
    """把内置种子 upsert 进 ModelEntry：更新上游名/成本/描述，保留管理员改过的
    enabled/超时/参数模板；缺失的自动补建。"""
    from sqlalchemy import select
    from .models import JSONText, ModelEntry
    imported, updated = 0, 0
    names = []
    for mid, upstream, cost, tpl in CAPCUT_MODEL_SEEDS:
        names.append(mid)
        entry = db.execute(select(ModelEntry).where(
            ModelEntry.model_id == mid)).scalar_one_or_none()
        if entry:
            entry.mcp_tool = upstream
            entry.estimated_cost = cost
            entry.provider = PROVIDER
            entry.description = f"SD 直连通道（{upstream}，common_task 协议）"
            updated += 1
        else:
            db.add(ModelEntry(
                model_id=mid, display_name=f"SD {upstream}", provider=PROVIDER,
                mcp_tool=upstream, mtype="video", enabled=True, estimated_cost=cost,
                timeout_seconds=900, auto_registered=False,
                description=f"SD 直连通道（{upstream}，common_task 协议）",
                param_template_text=JSONText.dump(tpl)))
            imported += 1
    db.commit()
    return {"imported": imported, "updated": updated, "models": len(names),
            "disabled_gone": 0, "model_names": names}


# ---------------- 官方模型目录（list_models 实时拉取） ----------------

# 官方 model_key -> 已验证可用的种子 model_id（沿用实测过的上游名，避免重复建目）
_KEY_ALIAS = {
    "seedance2_mini": "sd-seedance-2.0-mini",
    "seedance2": "sd-seedance-2.0",
    "seedance_1.0_fast": "sd-seedance-1.0-fast",
    "seedance_2.5": "sd-seedance-2.5",
}
_SEED_IDS = {s[0] for s in CAPCUT_MODEL_SEEDS}
_META_KEYS = ("name", "model_key", "summary", "icon", "ratios", "resolutions",
              "durations", "plan_durations", "gen_limits", "estimated_time")

# 参考素材上限的「实测覆写」表已统一到 `service._REF_LIMIT_OVERRIDES`
# （单一真相源；`service.ref_limits_of` 是唯一解析入口）。本模块不再自带一份，
# 否则「未经目录同步」的模型会解析不到覆写而掉回默认 9/3/3 —— 这正是 2.5 首启播种的旧 bug。


def _fetch_catalog_sync(account) -> dict:
    cli = client_from_account(account)
    h = cli._headers("query")
    h["Content-Type"] = "application/json"
    url = (f"https://edit-api-sg.capcut.com/storyboard/v1/agent/list_models"
           f"?aid=348188&device_platform=web&region={cli.region}&web_id={cli.web_id}")
    r = cli.up.sess.post(url, json={"req_scene": "ai_lab_web"}, headers=h, timeout=30)
    j = r.json()
    if str(j.get("ret")) not in ("0", "200"):
        raise RuntimeError(f"list_models ret={j.get('ret')} {j.get('errmsg')}")
    return j.get("data", {}).get("models") or {}


async def fetch_capcut_models(account) -> dict:
    """拉取 CapCut 官方模型目录（该接口不校验 sign，账号仅需登录态）。"""
    try:
        return await asyncio.to_thread(_fetch_catalog_sync, account)
    except Exception as e:  # noqa: BLE001
        raise wrap_error(e) from e


def upsert_capcut_catalog(db, catalog: dict) -> dict:
    """把官方目录 upsert 进 ModelEntry。
    - video 模型: mcp_tool=官方 model_key（直接可用于 common_task 提交）
    - 三个已验证种子沿用原 model_id/上游名（保留 enabled 与管理员配置）
    - 新发现的模型默认 enabled=False，由管理员在后台启用
    - 目录中消失的 CapCut 条目自动停用
    """
    from sqlalchemy import select
    from .models import JSONText, ModelEntry
    from .service import (gen_limits_for_key, limits_from_gen_limits,
                          normalize_ref_limits, ref_limits_for_key)
    imported = updated = 0
    seen = set()
    for mtype, models in (catalog or {}).items():
        for m in models or []:
            key = m.get("model_key") or m.get("name")
            if not key:
                continue
            mid = _KEY_ALIAS.get(key) or f"{MODEL_ID_PREFIX}{key}"
            seen.add(mid)
            meta = {k: m.get(k) for k in _META_KEYS if m.get(k) not in (None, [], {})}
            display = m.get("name") or key
            # 参考素材上限：内置实测覆写 > 官方 gen_limits > 内置默认
            # （覆写表统一在 service._REF_LIMIT_OVERRIDES，本模块不再自带一份，避免两处漂移）
            ov = ref_limits_for_key(key)
            raw_lim = ov if ov else limits_from_gen_limits(m.get("gen_limits"))
            ref_lim = normalize_ref_limits(raw_lim)
            ref_lim["_from"] = "override" if ov else ("catalog" if raw_lim else "default")
            # 生成能力上限（分辨率/时长）：内置实测覆写 > 内置默认
            gen_lim = gen_limits_for_key(key)
            res_declared = [r.get("resolution") if isinstance(r, dict) else r
                            for r in (m.get("resolutions") or [])]
            res_declared = [r for r in res_declared if r]
            desc_parts = [
                f"CapCut 官方目录 {mtype} 模型（{key}）",
                (f"；官方声明 时长{'/'.join(str(d) for d in m['durations'])}s"
                 f"、分辨率{'/'.join(str(r) for r in res_declared)}"
                 if (m.get("durations") or res_declared) else ""),
                (f"；网关放开 时长≤{gen_lim['max_duration']:g}s"
                 f"（{'/'.join(f'{d:g}' for d in gen_lim['durations'])}）"
                 f"、分辨率{'/'.join(f'{r}p' for r in gen_lim['resolutions'])}"
                 if mtype == "video" else ""),
                (f"；参考上限 图{ref_lim['image']}/视频{ref_lim['video']}"
                 f"/音频{ref_lim['audio']}（共{ref_lim['total']}）"
                 if mtype == "video" else ""),
            ]
            desc = "".join(p for p in desc_parts if p)
            entry = db.execute(select(ModelEntry).where(
                ModelEntry.model_id == mid)).scalar_one_or_none()
            if entry:
                entry.display_name = display
                entry.mtype = mtype if mtype in ("image", "video", "audio") else "other"
                entry.catalog_text = JSONText.dump(meta)
                entry.description = desc[:500]
                entry.provider = PROVIDER
                # 后台手工改过上限的（非空）不被同步覆盖
                if not (entry.ref_limits_text or "").strip():
                    entry.ref_limits_text = JSONText.dump(ref_lim)
                # 生成上限只落「真实放开值」：内置实测覆写或后台手工设置。
                # 内置默认（480/720p + ≤15s）不落库，保持动态解析，避免默认值被固化成快照
                # （否则以后调整默认值，老模型不会跟着变）。
                cur_gen = JSONText.load(entry.gen_limits_text, {}) or {}
                if not cur_gen or cur_gen.get("_from") == "default":
                    entry.gen_limits_text = (JSONText.dump(gen_lim)
                                             if gen_lim["_from"] == "override" else "")
                updated += 1
            else:
                db.add(ModelEntry(
                    model_id=mid, display_name=display, provider=PROVIDER,
                    mcp_tool=key, mtype=mtype if mtype in ("image", "video", "audio") else "other",
                    # 仅已知种子默认启用；其他新模型默认停用，待管理员评估成本后开启
                    enabled=mid in _SEED_IDS,
                    estimated_cost=0, timeout_seconds=900, auto_registered=True,
                    description=desc[:500],
                    catalog_text=JSONText.dump(meta),
                    ref_limits_text=JSONText.dump(ref_lim),
                    gen_limits_text=(JSONText.dump(gen_lim)
                                     if gen_lim["_from"] == "override" else ""),
                    param_template_text=JSONText.dump({"duration": 5, "generate_audio": True})
                    if mtype == "video" else ""))
                imported += 1
    # 目录中已消失的 CapCut 条目停用（不删除，保留历史任务引用）
    disabled_gone = 0
    for entry in db.execute(select(ModelEntry).where(
            ModelEntry.provider == PROVIDER)).scalars().all():
        if entry.model_id not in seen and entry.enabled:
            entry.enabled = False
            disabled_gone += 1
    db.commit()
    names = sorted(seen)
    return {"imported": imported, "updated": updated, "models": len(names),
            "disabled_gone": disabled_gone, "model_names": names}
