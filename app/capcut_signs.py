# -*- coding: utf-8 -*-
"""CapCut 请求签名（sign / device-time）的按账号管理。

背景（2026-09-15 实测）：
  sign 由前端 SDK 生成，服务端校验「sign + device-time」的配对关系。

  ⚠️ **sign 从来不是 `common_task/new` 被拦的原因**（这一点被反复误判过）：sign 的输入里
  没有任何账号密钥，可本地按当前时间现签（HAR 87/87 样本复现）。真正让 `new` 返回
  `ret=-6 "shark block only"` 的是 URL 查询串上的反爬参数 **`X-Gnarly`**，见 `capcut_antibot.py`。

  这里保留「按账号覆写 sign」是为了兼容拿到 HAR 的场景（可贴 HAR 自动提取），
  正常情况下 auto 模式会按当前时间现签，不需要账号专属值。

数据结构（账号表 `capcut_signs_text` 存 JSON）::

    {
      "new":   {"sign": "63fce733...", "device_time": "1789453186", "tdid": "178931314028893759",
                "source": "har", "updated_at": 1789455000.0},
      "query": {"sign": "...", "device_time": "..."}
    }

优先级：账号覆写 > 内置常量。条目缺字段时逐字段回落到内置值。
"""
import json
import re
import time
from urllib.parse import urlparse

# ---------- 接口键定义 ----------
# key: (中文名, 说明)
SIGN_KEY_INFO = {
    "new": ("提交生成", "POST /lv/v1/common_task/new（风控最严，绑定账号会话）"),
    "query": ("查询任务", "POST /lv/v1/common_task/query（宽松，可跨账号）"),
    "chat_upload_sign": ("参考素材 STS", "POST /lv/v2/intelligence/file/chat_upload_sign（宽松）"),
    "upload_sign": ("图片/视频 STS", "POST /lv/v1/upload_sign（biz=capcut_videocut_*，宽松）"),
    "upload_sign_ref": ("参考视频 STS", "POST /lv/v1/upload_sign（biz=digital_cameo，宽松）"),
    "user_credit": ("积分查询", "GET commerce-api-sg /commerce/v1/benefits/user_credit（宽松）"),
}
SIGN_KEYS = tuple(SIGN_KEY_INFO)

# 需要 tdid 头的键（当前只有 upload_sign 系列会带）
TDID_KEYS = ("upload_sign", "upload_sign_ref")

# ---------- 签名算法（2026-09-15 从前端 bundle 逆向 + HAR 87/87 校验通过） ----------
# 出处: sf16-web-login-neutral.capcutstatic.com/.../lvweb/video_online/static/js/editor.<hash>.js
#       （services-<hash>.js 里同一函数的 asset 版用 pf=1/tdid=web）
#   function pi({url, pf, appvr, tdid}) {
#     let {pathname} = new URL(url);
#     let s = Math.floor(Date.now() / 1000);
#     return { sign: md5(`9e2c|${pathname.slice(-7)}|${pf}|${appvr}|${s}|${tdid}|11ac`).toLowerCase(),
#              "device-time": s };
#   }
# 即：sign = MD5("9e2c|" + 路径末 7 字符 + "|" + pf + "|" + appvr + "|" + device-time + "|" + tdid + "|11ac")
# 输入只有「请求路径 + 客户端版本三元组 + 时间戳」，**没有任何账号密钥** ——
# 所以 sign 不需要从 HAR 抓取，可以本地随时生成（HAR 里的 87 条样本全部复现）。
# pf / appvr 必须与实际发出的请求头逐字符一致，否则校验不过（客户端发什么就用什么算）。
SIGN_SALT_PREFIX = "9e2c"
SIGN_SALT_SUFFIX = "11ac"
DEFAULT_PF = "7"
DEFAULT_APPVR = "8.4.0"     # 与 capcut_direct_task / capcut_upload_protocol 发出的 appvr 一致
DEFAULT_TDID = ""

# 各接口键对应的请求路径（本地签名用；路径末 7 字符进哈希，故必须与实际请求一致）
KEY_PATHS = {
    "new": "/lv/v1/common_task/new",
    "query": "/lv/v1/common_task/query",
    "chat_upload_sign": "/lv/v2/intelligence/file/chat_upload_sign",
    "upload_sign": "/lv/v1/upload_sign",
    "upload_sign_ref": "/lv/v1/upload_sign",
    "user_credit": "/commerce/v1/benefits/user_credit",
}


def make_sign(path: str, device_time=None, pf: str = DEFAULT_PF,
              appvr: str = DEFAULT_APPVR, tdid: str = "") -> tuple:
    """本地生成一对 (sign, device-time)。path 可传完整 URL 或仅 pathname。"""
    import hashlib
    from urllib.parse import urlparse
    p = urlparse(path).path if "://" in str(path) else str(path)
    dt = str(int(device_time if device_time is not None else _now()))
    raw = f"{SIGN_SALT_PREFIX}|{p[-7:]}|{pf}|{appvr}|{dt}|{tdid or ''}|{SIGN_SALT_SUFFIX}"
    return hashlib.md5(raw.encode()).hexdigest().lower(), dt


def mint_sign(key: str, device_time=None, pf: str = DEFAULT_PF,
              appvr: str = DEFAULT_APPVR, tdid: str = "") -> dict:
    """按接口键生成可直接用的签名条目。"""
    path = KEY_PATHS.get(key)
    if not path:
        raise KeyError(f"未知接口键: {key}")
    sign, dt = make_sign(path, device_time, pf, appvr, tdid)
    e = {"sign": sign, "device_time": dt, "source": "mint"}
    if tdid:
        e["tdid"] = tdid
    return e


def verify_sign(path, sign, device_time, pf=DEFAULT_PF, appvr=DEFAULT_APPVR, tdid="") -> bool:
    return make_sign(path, device_time, pf, appvr, tdid)[0] == str(sign).lower()


# ---------- 内置兜底常量（从浏览器 HAR 抓取；现在仅作兜底，正常走本地签名） ----------
BUILTIN_SIGNS = {
    "new": {"sign": "63fce733f3498eff62428b7406974a55", "device_time": "1789453186"},
    "query": {"sign": "af7d7f60bdf9dfcacd1d8ba2a3be3531", "device_time": "1789453189"},
    "chat_upload_sign": {"sign": "8899ec553977a04f977b8b6aed4bc93e", "device_time": "1789450985"},
    "upload_sign_ref": {"sign": "5f072281d34c31d68566a14b6e80b07d", "device_time": "1789450955"},
    # 这条来自 appvr=12.4.0 的那次会话（反查确认），故显式记录 appvr
    "user_credit": {"sign": "0f178f29f69499db206efe40dde9bce4", "device_time": "1789453168",
                    "appvr": "12.4.0"},
    # PureUploader 的 upload_sign 常量（含 tdid）
    "upload_sign": {"sign": "2057bffb49600bf28e31a20bd16fb202", "device_time": "1789313239",
                    "tdid": "178931314028893759"},
}


# ---------- 归一化 ----------
def _pick(d: dict, *names):
    for n in names:
        if isinstance(d, dict) and d.get(n) not in (None, ""):
            return d[n]
    return None


def _norm_entry(v, source=None, updated_at=None) -> dict | None:
    """把多种写法归一为 {sign, device_time, tdid?, appvr?, source?, updated_at?}。"""
    sign = dt = tdid = None
    extra_pf = extra_appvr = None
    if isinstance(v, (list, tuple)):
        if len(v) >= 2:
            sign, dt = str(v[0]), str(v[1])
        if len(v) >= 3 and v[2]:
            tdid = str(v[2])
    elif isinstance(v, dict):
        sign = _pick(v, "sign", "Sign", "SIGN")
        dt = _pick(v, "device_time", "device-time", "deviceTime", "dt", "time")
        tdid = _pick(v, "tdid", "ttdid")
        extra_pf = _pick(v, "pf")
        extra_appvr = _pick(v, "appvr", "appVersion")
        source = source or v.get("source")
        updated_at = updated_at or v.get("updated_at")
    elif isinstance(v, str):
        sign = v
    if sign is not None:
        sign = str(sign).strip()
    if dt is not None:
        dt = str(dt).strip()
    if not sign or not dt or not re.fullmatch(r"\d{9,12}", dt):
        return None
    if len(sign) < 8:
        return None
    e = {"sign": sign, "device_time": dt}
    if tdid:
        e["tdid"] = str(tdid).strip()
    if extra_pf:
        e["pf"] = str(extra_pf)
    if extra_appvr:
        e["appvr"] = str(extra_appvr)
    if source:
        e["source"] = source
    if updated_at:
        e["updated_at"] = float(updated_at)
    return e


def normalize_signs(raw, default_key=None) -> dict:
    """接受多种输入形态，输出标准 dict。

    支持：
      {"new": ["sign", "dt"]}
      {"new": {"sign": ..., "device-time": ...}}
      {"sign": ..., "device-time": ...}          # 配合 default_key
      [{"key": "new", "sign": ..., "device_time": ...}, ...]
      "sign: xxx\\ndevice-time: yyy"              # 纯文本（配合 default_key）
    """
    out = {}
    if raw is None:
        return out
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return out
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            got = extract_from_text(raw, default_key=default_key)
            return got.get("signs", {})
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            k = item.get("key") or item.get("pair") or default_key
            e = _norm_entry(item, source=item.get("source"))
            if k in SIGN_KEYS and e:
                out[k] = e
        return out
    if isinstance(raw, dict):
        # 形态 A：单个条目（含 sign / device-time）
        if any(x in raw for x in ("sign", "device-time", "device_time", "deviceTime")):
            k = raw.get("key") or raw.get("pair") or default_key
            e = _norm_entry(raw)
            if k in SIGN_KEYS and e:
                out[k] = e
            return out
        # 形态 B：{key: entry}
        for k, v in raw.items():
            if k not in SIGN_KEYS or v in (None, ""):
                continue
            e = _norm_entry(v)
            if e:
                out.setdefault("_sources", {})[k] = "manual"
                out[k] = e
        out.pop("_sources", None)
        return out
    return out


def merge_signs(*layers) -> dict:
    """逐字段合并多层签名（后者优先）。空层跳过。"""
    out = {}
    for layer in layers:
        if not layer:
            continue
        for k, v in (layer or {}).items():
            if k not in SIGN_KEYS:
                continue
            e = _norm_entry(v)
            if not e:
                continue
            cur = out.get(k, {})
            cur.update({kk: vv for kk, vv in e.items() if vv not in (None, "")})
            out[k] = cur
    return out


def effective_signs(account_signs, builtin=None) -> dict:
    """账号覆写 > 内置兜底，逐字段合并；标注来源。"""
    builtin = BUILTIN_SIGNS if builtin is None else builtin
    out = {}
    for k in SIGN_KEYS:
        base = _norm_entry(builtin.get(k) or {}) or {}
        ov = _norm_entry((account_signs or {}).get(k) or {}) or {}
        if not base and not ov:
            continue
        e = dict(base)
        e.update({kk: vv for kk, vv in ov.items() if vv not in (None, "")})
        over = [kk for kk in ("sign", "device_time", "tdid") if ov.get(kk) and ov.get(kk) != base.get(kk)]
        e["_source"] = "account" if over else "builtin"
        e["_overridden"] = over
        out[k] = e
    return out


# ---------- 时效 / 展示 ----------
def _now() -> float:
    return time.time()


def sign_age_hours(entry, now=None) -> float | None:
    """device-time 距今小时数（device-time 是抓包时的 unix 秒）。"""
    try:
        dt = float((entry or {}).get("device_time"))
    except (TypeError, ValueError):
        return None
    return round(((now or _now()) - dt) / 3600.0, 2)


def mask_sign(s) -> str:
    s = str(s or "")
    if len(s) <= 12:
        return s[:4] + "…"
    return s[:8] + "…" + s[-4:]


def describe_signs(account_signs, builtin=None, now=None) -> list:
    """给后台展示的行：键 / 来源 / 掩码 / 抓取时间 / 时效。"""
    eff = effective_signs(account_signs, builtin)
    rows = []
    for k in SIGN_KEYS:
        label, hint = SIGN_KEY_INFO[k]
        e = eff.get(k)
        if not e:
            rows.append({"key": k, "label": label, "hint": hint, "source": "none",
                         "sign_masked": None, "device_time": None, "age_hours": None,
                         "tdid": False})
            continue
        age = sign_age_hours(e, now)
        rows.append({
            "key": k,
            "label": label,
            "hint": hint,
            "source": e.get("_source", "builtin"),
            "overridden": e.get("_overridden") or [],
            "sign_masked": mask_sign(e.get("sign")),
            "device_time": e.get("device_time"),
            "age_hours": age,
            "fetched_at": (float(e["device_time"]) if str(e.get("device_time", "")).isdigit() else None),
            "tdid": bool(e.get("tdid")),
            "stale": bool(age is not None and age > 24 * 7),
        })
    return rows


def is_risk_block(resp) -> bool:
    """判定上游返回是否为风控拦截（用于统一提示处置方式）。"""
    txt = json.dumps(resp, ensure_ascii=False) if not isinstance(resp, str) else resp
    return ("shark" in txt.lower()) or ('"ret":-6' in txt.replace(" ", "")) or ("ret': -6" in txt)


def risk_hint(key="new") -> str:
    return (f"该接口（{key}）的 sign 与账号会话绑定：请用**这个账号**的浏览器真跑一次生成，"
            f"导出 HAR 后在后台「编辑账号 → CapCut 签名 → 从 HAR 提取」保存；"
            f"或直接粘贴 request header 里的 sign / device-time。")


# ---------- 从 HAR / 文本提取 ----------
_PATH_RULES = (
    ("common_task/new", "new"),
    ("common_task/query", "query"),
    ("chat_upload_sign", "chat_upload_sign"),
    ("benefits/user_credit", "user_credit"),
    ("upload_sign", "upload_sign"),        # biz=digital_cameo 时改判 upload_sign_ref
)


def key_for(path: str, body_text: str = "") -> str | None:
    p = (path or "").lower()
    if "chat_upload_sign" in p:
        return "chat_upload_sign"
    if "upload_sign" in p:
        return "upload_sign_ref" if "digital_cameo" in (body_text or "") else "upload_sign"
    for frag, k in _PATH_RULES:
        if frag in p:
            return k
    if "benefits" in p and "credit" in p:
        return "user_credit"
    return None


def _headers_to_map(headers) -> dict:
    out = {}
    if isinstance(headers, dict):
        for k, v in headers.items():
            out[str(k).lower()] = str(v)
    elif isinstance(headers, list):
        for h in headers:
            if isinstance(h, dict) and h.get("name") is not None:
                out[str(h["name"]).lower()] = str(h.get("value") or "")
    return out


def extract_from_har(data, source: str = "har", default_key: str | None = "new") -> dict:
    """从 HAR（dict 或 JSON 文本）里抽取各接口的 sign / device-time / tdid。"""
    if isinstance(data, (str, bytes)):
        txt = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        try:
            data = json.loads(txt)
        except Exception:  # noqa: BLE001  超大/被截断的 HAR：退回文本扫描
            return extract_from_text(txt, default_key=default_key, source=source)
    entries = (((data or {}).get("log") or {}).get("entries")) or []
    if not entries:
        # 也许是只含 entries 的片段
        entries = data.get("entries") if isinstance(data, dict) else []
    found = {}
    for e in entries or []:
        req = (e or {}).get("request") or {}
        url = req.get("url") or ""
        h = _headers_to_map(req.get("headers"))
        sign = h.get("sign")
        dt = h.get("device-time") or h.get("device_time")
        if not sign or not dt:
            continue
        body = ((req.get("postData") or {}).get("text") or "")
        k = key_for(urlparse(url).path or url, body)
        if not k:
            continue
        entry = _norm_entry({"sign": sign, "device_time": dt, "tdid": h.get("tdid")},
                            source=source, updated_at=_now())
        if not entry:
            continue
        prev = found.get(k)
        if prev is None or int(entry["device_time"]) >= int(prev["device_time"]):
            if prev and prev.get("tdid") and not entry.get("tdid"):
                entry["tdid"] = prev["tdid"]
            found[k] = entry
    return _extract_result(found)


_SIGN_RE = re.compile(r'(?<![A-Za-z0-9_-])(?:sign|Sign|SIGN)(?![A-Za-z0-9_-])["\']?\s*[:=]\s*["\']?([0-9a-fA-F]{16,64})')
_DT_RE = re.compile(r'device[-_]?time["\']?\s*[:=]\s*["\']?(\d{9,12})')
_TDID_RE = re.compile(r'(?<![A-Za-z0-9_-])tdid["\']?\s*[:=]\s*["\']?(\d{8,24})')
_URL_RE = re.compile(r'https?://[^\s"\'<>\\]+')
_HEX_RE = re.compile(r'[0-9a-fA-F]{16,64}')
# HAR / DevTools 的头对写法: {"name": "sign", "value": "...."}
_NV_RE = re.compile(r'"name"\s*:\s*"([^"]{1,64})"\s*,\s*"value"\s*:\s*"([^"]{0,400})"')
_NV_DT_NAMES = ("device-time", "device_time", "devicetime")
# 头对之间的最大搜索距离（HAR 里按字母序排列，device-time 在 sign **之前**）
_HEADER_SCAN_CAP = 6000


def _key_from_context(txt: str, pos: int, default_key: str | None) -> str | None:
    """在 pos 之前就近找接口路径，判定属于哪个接口键。"""
    win_s = max(0, pos - 6000)
    win = txt[win_s:pos]
    for x in sorted(_URL_RE.finditer(win), key=lambda y: -y.start()):
        key = key_for(x.group(0))
        if key:
            return key
    return default_key


def _nearest_pair(pairs, i: int, names, cap: int = _HEADER_SCAN_CAP):
    """在 pairs[i] 的前后就近找名字属于 names 的头对（取距离最近的一个）。"""
    best = None
    for j in range(i - 1, -1, -1):
        if pairs[i].start() - pairs[j].start() > cap:
            break
        if pairs[j].group(1).strip().lower() in names:
            best = (pairs[i].start() - pairs[j].start(), pairs[j])
            break
    for j in range(i + 1, len(pairs)):
        if pairs[j].start() - pairs[i].start() > cap:
            break
        if pairs[j].group(1).strip().lower() in names:
            d = pairs[j].start() - pairs[i].start()
            if best is None or d < best[0]:
                best = (d, pairs[j])
            break
    return best[1] if best else None


def _scan_name_value(txt: str, default_key, source) -> dict:
    """扫描 HAR / DevTools 的 {"name":..., "value":...} 头对。"""
    pairs = list(_NV_RE.finditer(txt))
    found = {}
    for i, m in enumerate(pairs):
        if m.group(1).strip().lower() != "sign":
            continue
        sign = m.group(2).strip()
        if not _HEX_RE.fullmatch(sign):
            continue
        dtm = _nearest_pair(pairs, i, _NV_DT_NAMES)
        if dtm is None:
            continue
        dt = dtm.group(2).strip()
        if not re.fullmatch(r"\d{9,12}", dt):
            continue
        tdm = _nearest_pair(pairs, i, ("tdid",))
        tdid = (tdm.group(2).strip() if tdm and tdm.group(2).strip() else None)
        key = _key_from_context(txt, m.start(), default_key)
        if key not in SIGN_KEYS:
            continue
        entry = _norm_entry({"sign": sign, "device_time": dt, "tdid": tdid},
                            source=source, updated_at=_now())
        if not entry:
            continue
        prev = found.get(key)
        if prev is None or int(entry["device_time"]) >= int(prev["device_time"]):
            if prev and prev.get("tdid") and not entry.get("tdid"):
                entry["tdid"] = prev["tdid"]
            found[key] = entry
    return found


def _scan_loose(txt: str, default_key, source) -> dict:
    """扫描自由文本（curl / 复制的 header 块，两种头顺序都兼容）。"""
    found = {}
    for m in _SIGN_RE.finditer(txt):
        sign = m.group(1)
        win_s = max(0, m.start() - 3000)
        win_e = min(len(txt), m.end() + 3000)
        win = txt[win_s:win_e]
        rel = m.end() - win_s
        cands = sorted((abs(x.start() - rel), x.group(1)) for x in _DT_RE.finditer(win))
        if not cands:
            continue
        dt = cands[0][1]
        key = _key_from_context(txt, m.start(), default_key)
        if key not in SIGN_KEYS:
            continue
        cands_t = sorted((abs(x.start() - rel), x.group(1)) for x in _TDID_RE.finditer(win))
        entry = _norm_entry({"sign": sign, "device_time": dt,
                             "tdid": cands_t[0][1] if cands_t else None},
                            source=source, updated_at=_now())
        if not entry:
            continue
        prev = found.get(key)
        if prev is None or int(entry["device_time"]) >= int(prev["device_time"]):
            found[key] = entry
    return found


def extract_from_text(text: str, default_key: str | None = "new", source: str = "text") -> dict:
    """从自由文本提取：支持 HAR（含截断/超大）、curl、HTTP header 块、DevTools 复制内容。

    两轮扫描：先按 HAR 的 name/value 头对，再按宽松的 `sign: xxx` 写法；
    同一接口键取 device-time 最新（=抓取时间最晚）的一条。
    """
    txt = text or ""
    found = _scan_name_value(txt, default_key, source)
    for k, v in _scan_loose(txt, default_key, source).items():
        prev = found.get(k)
        if prev is None or int(v["device_time"]) >= int(prev["device_time"]):
            found.setdefault(k, {})
            found[k] = v
    return _extract_result(found)


def _extract_result(found: dict) -> dict:
    return {
        "signs": found,
        "found": sorted(found.keys()),
        "missing": [k for k in SIGN_KEYS if k not in found],
    }


def extract(text, default_key="new", source=None) -> dict:
    """统一入口：先按 HAR 结构化解析，失败再按文本扫描。"""
    src = source or "har"
    return extract_from_har(text, source=src, default_key=default_key)


def builtin_for(key: str) -> dict:
    return _norm_entry(BUILTIN_SIGNS.get(key) or {}) or {}


if __name__ == "__main__":  # python -m app.capcut_signs <har|txt> [默认键]
    import sys
    if len(sys.argv) < 2:
        print("用法: python -m app.capcut_signs <har 文件> [默认接口键]")
        print("接口键:", ", ".join(SIGN_KEYS))
        raise SystemExit(1)
    path = sys.argv[1]
    dk = sys.argv[2] if len(sys.argv) > 2 else "new"
    r = extract(open(path, encoding="utf-8", errors="replace").read(), default_key=dk)
    print(json.dumps(r, ensure_ascii=False, indent=2))
