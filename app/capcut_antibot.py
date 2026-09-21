"""CapCut 反爬参数 X-Gnarly / X-Bogus（风控 shark 前的第一道门）。

## 为什么需要它（2026-09-15 实测结论）

CapCut 网页端每次调 edit-api 都会在 **URL 查询串**上带 `msToken` / `X-Bogus` / `X-Gnarly`
三个参数（不是请求头！由 webmssdk + secsdk_runtime_bundler 生成）。

`POST /lv/v1/common_task/new` 的 shark 风控会校验 **`X-Gnarly`**：

| 请求查询串 | 结果 |
|---|---|
| 不带 `X-Gnarly` | `ret=-6 shark block only` |
| `X-Gnarly=Mx`（1 字符） | `-6` |
| `X-Gnarly=MxEcb3`（6 字符） | `-6` |
| `X-Gnarly=MxEc`（4 字符） | `ret=1000`（放行） |
| `X-Gnarly=MxEcb3Ovna5uDKefmxB5hffNmh15` | `ret=1000`（放行，多次稳定复现） |
| `X-Gnarly=<完整 88 字符>（HAR 原值）` | `-6` ← 怪，但实测如此，故只用 28 字符前缀 |
| `X-Gnarly=<随机 28 字符>` | `-6`（内容要合法，不能瞎编） |

- `X-Bogus` **不是必需**：只带 `X-Bogus` 不带 `X-Gnarly` 仍被拦；`X-Gnarly` 单独带即可放行。
- 与账号无关：本次用它把「余额 1200 但一直被拦」的账号 #3 救活（同样参数在 #1/#2 上也照常通过）。
- 也**与 sign 无关**（sign 是另一套，见 `capcut_signs.py`）。

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `CAPCUT_ANTIBOT` | `on` | `off` 时不注入（排查用） |
| `CAPCUT_XGNARLY` | 内置 28 字符常量 | 换成新抓的值（会过期/换设备后失效） |
| `CAPCUT_XBOGUS` | 内置常量 | 非必需，带上更像浏览器 |

## 失效了怎么换新的

浏览器登录 capcut.com → 打开 DevTools Network → 随便触发一次 edit-api 请求
（如编辑器里点一次生成）→ Copy as cURL，然后：

    python -m app.capcut_antibot <har 或 cURL 文本>

会把该请求里的 X-Gnarly / X-Bogus 打出来。**X-Gnarly 只取前 28 字符**。
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse, parse_qsl

# 内置兜底常量：来自 2026-09-15 浏览器 HAR（Windows Chrome，edit-api-sg）
# 注意 X-Gnarly 这里刻意只保留 28 字符前缀 —— 完整 88 字符实测反而会被拦。
DEFAULT_XGNARLY = "MxEcb3Ovna5uDKefmxB5hffNmh15"
DEFAULT_XBOGUS = "DFSzswVLE21xdnt7Cvj8TWq3F/xn"

# 注入时是否连 X-Bogus 一起带（非必需，带上更接近浏览器）
SEND_XBOGUS = True


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def enabled() -> bool:
    return _env("CAPCUT_ANTIBOT", "on").lower() not in ("0", "off", "false", "no")


def xgnarly() -> str:
    return _env("CAPCUT_XGNARLY", "") or DEFAULT_XGNARLY


def xbogus() -> str:
    return _env("CAPCUT_XBOGUS", "") or DEFAULT_XBOGUS


def antibot_params() -> dict:
    """需要拼到 edit-api URL 查询串上的反爬参数（未启用时为空）。"""
    if not enabled():
        return {}
    p = {"X-Gnarly": xgnarly()}
    if SEND_XBOGUS:
        p["X-Bogus"] = xbogus()
    return p


def antibot_suffix() -> str:
    """'&X-Gnarly=...&X-Bogus=...'（直接拼在已有 '?' 查询串后面）。"""
    p = antibot_params()
    return "".join(f"&{k}={v}" for k, v in p.items())


def append_antibot(url: str) -> str:
    """给任意 URL 追加反爬参数（幂等：已有则不重复加）。"""
    if not enabled() or "X-Gnarly=" in url:
        return url
    return url + antibot_suffix()


def describe() -> dict:
    return {
        "enabled": enabled(),
        "x_gnarly": xgnarly(),
        "x_bogus": xbogus() if SEND_XBOGUS else "",
        "source": "env" if _env("CAPCUT_XGNARLY") else "builtin",
    }


# ---------------- 提取器（换新常量用） ----------------

_XG_RE = re.compile(r"X-Gnarly['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9_\-/+=]{4,})")
_XB_RE = re.compile(r"X-Bogus['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9_\-/+=]{4,})")


def extract_from_text(text: str) -> dict:
    """从 HAR / cURL / header 文本里提取 X-Gnarly 与 X-Bogus（截断到 28 字符）。"""
    out: dict = {}
    # 优先按 URL 查询串解析（最准）
    for m in re.finditer(r'"(?:url)"\s*:\s*"([^"]+)"', text):
        q = dict(parse_qsl(urlparse(m.group(1).replace("\\u0026", "&")).query))
        if q.get("X-Gnarly"):
            out["x_gnarly"] = q["X-Gnarly"][:28]
            if q.get("X-Bogus"):
                out["x_bogus"] = q["X-Bogus"]
            return out
    # 回退：正则扫描
    g = _XG_RE.search(text)
    b = _XB_RE.search(text)
    if g:
        out["x_gnarly"] = g.group(1)[:28]
    if b:
        out["x_bogus"] = b.group(1)
    return out


def extract_from_file(path: str) -> dict:
    return extract_from_text(open(path, encoding="utf-8", errors="replace").read())


if __name__ == "__main__":  # python -m app.capcut_antibot <har|txt>
    import sys

    if len(sys.argv) < 2:
        print("用法: python -m app.capcut_antibot <har 文件 | 含请求的文本文件>")
        print("当前生效:", describe())
        raise SystemExit(1)
    got = extract_from_file(sys.argv[1])
    if not got:
        print("没提取到 X-Gnarly / X-Bogus —— 换一份含 edit-api 请求的 HAR")
        raise SystemExit(2)
    for k, v in got.items():
        print(f"{k} = {v}")
    print("\n设为环境变量即可生效：")
    print(f"  CAPCUT_XGNARLY={got.get('x_gnarly','')}")
    if got.get("x_bogus"):
        print(f"  CAPCUT_XBOGUS={got['x_bogus']}")
