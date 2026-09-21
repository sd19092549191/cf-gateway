"""零成本自检：本地/远端网关的对外模型名是否已彻底去掉上游品牌。

用法:
  PY=/Users/sun/.workbuddy/binaries/python/envs/default/bin/python
  $PY _check_model_names.py                       # 本地 127.0.0.1:8001
  $PY _check_model_names.py <base_url>            # 指定网关
"""
import json
import os
import re
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "_test_state.json")
FORBIDDEN = re.compile(r"capcut", re.IGNORECASE)


def main():
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8001").rstrip("/")
    keys = json.load(open(STATE, encoding="utf-8"))
    s = requests.Session()
    s.trust_env = False
    bad, seen = [], 0
    for name, key in keys.items():
        if not str(key).startswith("sk-"):
            continue
        r = s.get(base + "/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=60)
        try:
            d = r.json()
        except Exception:  # noqa: BLE001
            print(f"[{name}] HTTP {r.status_code} 非 JSON")
            continue
        for m in d.get("data") or []:
            seen += 1
            blob = json.dumps(m, ensure_ascii=False)
            if FORBIDDEN.search(blob):
                bad.append((name, m))
    print(f"网关 {base}：共检 {seen} 个模型条目")
    if bad:
        print(f"❌ 仍有 {len(bad)} 条含品牌字样：")
        for n, m in bad:
            print("   ", n, json.dumps(m, ensure_ascii=False)[:200])
        sys.exit(1)
    print("✅ 对外模型名 / owned_by / 上限字段均无品牌字样")


if __name__ == "__main__":
    main()
