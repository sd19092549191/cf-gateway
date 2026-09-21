"""New API 后台管理助手（带重试 / 会话复用）。

用法:
  PY=/Users/sun/.workbuddy/binaries/python/envs/default/bin/python
  $PY _newapi_admin.py whoami
  $PY _newapi_admin.py channels
  $PY _newapi_admin.py options [关键字...]        # 只看含关键字的 option
  $PY _newapi_admin.py pricing [模型名]
  $PY _newapi_admin.py ch-set <渠道id> --models a,b,c      # 覆盖渠道模型列表
  $PY _newapi_admin.py ch-set <渠道id> --group g1,g2
  $PY _newapi_admin.py ch-add-model <渠道id> <模型名>
  $PY _newapi_admin.py opt-set <key> <value>

环境变量: NEWAPI_BASE(默认 https://newapi.turnitin.space) / NEWAPI_USER(admin) / NEWAPI_PASS
"""
import json
import os
import sys
import time

import requests

BASE = os.environ.get("NEWAPI_BASE", "https://newapi.turnitin.space").rstrip("/")
USER = os.environ.get("NEWAPI_USER", "admin")
PASS = os.environ.get("NEWAPI_PASS", "Dd112255@")
SESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_newapi_session.json")


def _sess() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "cf-gateway-ops/1.0"})
    return s


def login(s: requests.Session, tries: int = 4) -> dict:
    last = None
    for i in range(tries):
        try:
            r = s.post(BASE + "/api/user/login",
                       json={"username": USER, "password": PASS}, timeout=60)
            j = r.json()
            if not j.get("success"):
                raise RuntimeError(f"登录失败: {j.get('message')}")
            me = j.get("data") or {}
            s.headers["New-Api-User"] = str(me.get("id", 1))
            json.dump({"id": me.get("id", 1), "username": me.get("username")},
                      open(SESS_FILE, "w"))
            return me
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + i * 3)
    raise RuntimeError(f"登录重试 {tries} 次仍失败: {last}")


def call(s: requests.Session, method: str, path: str, tries: int = 4, **kw):
    last = None
    for i in range(tries):
        try:
            r = s.request(method, BASE + path, timeout=kw.pop("timeout", 90), **kw)
            try:
                return r.status_code, r.json()
            except Exception:  # noqa: BLE001
                return r.status_code, {"raw": r.text[:500]}
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + i * 3)
    raise RuntimeError(f"{method} {path} 重试 {tries} 次仍失败: {last}")


def jdump(v, n=4000):
    print(json.dumps(v, ensure_ascii=False, indent=1)[:n])


# ⚠️ New API 的 PUT /api/channel/ 硬性拒绝带 `status` 的请求体（controller/channel.go
# 检测到 requestData["status"] 即回 Invalid parameters）。这几个字段都是服务端维护的，
# 更新时必须剔除，否则整个渠道都改不动。
_READONLY_CH_FIELDS = ("status", "channel_info", "used_quota", "balance",
                       "balance_updated_time", "created_time", "test_time",
                       "response_time", "openai_organization", "test_model")


def _strip_readonly(ch: dict) -> dict:
    for k in _READONLY_CH_FIELDS:
        ch.pop(k, None)
    return ch


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    s = _sess()
    me = login(s)
    if cmd == "whoami":
        jdump(me)
    elif cmd == "channels":
        _c, j = call(s, "GET", "/api/channel/?p=0&page_size=100")
        items = (j.get("data") or {}).get("items") or []
        for c in items:
            ms = [m for m in (c.get("models") or "").split(",") if m]
            print(f"id={c.get('id')} name={c.get('name')} status={c.get('status')} "
                  f"group={c.get('group')} type={c.get('type')} base={c.get('base_url')} "
                  f"({len(ms)} models)")
            print("    ", ",".join(ms))
    elif cmd == "options":
        _c, j = call(s, "GET", "/api/option/")
        data = j.get("data") or []
        kws = sys.argv[2:]
        hits = [d for d in data if not kws or any(k.lower() in str(d.get("key", "")).lower() for k in kws)]
        for d in hits:
            print(f"--- {d.get('key')}")
            print("   ", str(d.get("value"))[:2500])
    elif cmd == "opt-set":
        key, val = sys.argv[2], sys.argv[3]
        c, j = call(s, "PUT", "/api/option/", json={"key": key, "value": val})
        print(c, j.get("success"), str(j.get("message"))[:200])
    elif cmd == "pricing":
        c, j = call(s, "GET", "/api/pricing")
        data = j.get("data") or []
        want = sys.argv[2] if len(sys.argv) > 2 else ""
        for d in data:
            name = d.get("model_name")
            if want and want not in str(name):
                continue
            print(f"{name:32s} quota_type={d.get('quota_type')} model_ratio={d.get('model_ratio')} "
                  f"model_price={d.get('model_price')} completion={d.get('completion_ratio')} "
                  f"enable_groups={d.get('enable_groups')}")
    elif cmd == "ch-set":
        cid = sys.argv[2]
        _c, j = call(s, "GET", f"/api/channel/{cid}")
        ch = j.get("data") or {}
        if not ch:
            print("渠道不存在:", cid)
            return
        models = ch.get("models")
        group = ch.get("group")
        mapping = ch.get("model_mapping")
        argv = sys.argv[3:]
        if "--models" in argv:
            models = argv[argv.index("--models") + 1]
        if "--group" in argv:
            group = argv[argv.index("--group") + 1]
        if "--mapping" in argv:
            mapping = argv[argv.index("--mapping") + 1]
        ch["models"] = models
        ch["group"] = group
        ch["model_mapping"] = mapping
        _strip_readonly(ch)
        c, j2 = call(s, "PUT", "/api/channel/", json=ch)
        print("HTTP", c, "success=", j2.get("success"), str(j2.get("message"))[:200])
        print("models 现在 =", models)
    elif cmd == "ch-add-model":
        cid, name = sys.argv[2], sys.argv[3]
        _c, j = call(s, "GET", f"/api/channel/{cid}")
        ch = j.get("data") or {}
        ms = [m for m in (ch.get("models") or "").split(",") if m]
        if name in ms:
            print("已存在:", name)
            return
        ms.append(name)
        ch["models"] = ",".join(ms)
        _strip_readonly(ch)
        c, j2 = call(s, "PUT", "/api/channel/", json=ch)
        print("HTTP", c, "success=", j2.get("success"), str(j2.get("message"))[:200])
        print("models 现在 =", ch["models"])
    else:
        print("未知命令:", cmd)
        print(__doc__)


if __name__ == "__main__":
    main()
