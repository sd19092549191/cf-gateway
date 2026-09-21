"""轮询网关任务直到出片。

用法: python _poll_task.py <task_id> [--max-poll 900] [--interval 10] [--newapi]
  --newapi 走 New API 轮询（默认直连网关），用于验证 New API 全链路
输出: 出片后打印 url / 规格，并写 relay/_<task_id>.json
"""
import json
import os
import re
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRECT = ("http://159.198.41.147:8000", "sk-cf-702df8270d8b481e4ac2c08b8420922de2ed4ff9f05219c6")
NEWAPI = ("https://newapi.turnitin.space", "sk-twAE3QQneaEXAON7WrtVDPl0YPpy4OT8wLmBKYPycJsCSTAv")


def main():
    task_id = sys.argv[1]
    def arg(name, default):
        if name in sys.argv:
            return type(default)(sys.argv[sys.argv.index(name) + 1])
        return default
    max_poll = arg("--max-poll", 900)
    interval = arg("--interval", 10)
    use_newapi = "--newapi" in sys.argv
    base, key = NEWAPI if use_newapi else DIRECT
    s = requests.Session()
    s.trust_env = use_newapi  # 直连网关要绕开本机代理，New API 域名必须走代理
    h = {"Authorization": f"Bearer {key}"}
    print("轮询链路:", "New API" if use_newapi else "直连网关", flush=True)
    t0 = time.time()
    last = None
    while time.time() - t0 < max_poll:
        if use_newapi:
            # New API 不支持 GET /v1/generations，只能走 @query 魔法指令。
            # ⚠️ 用收费模型名轮询会被按 token 计费（实测单次 ≈¥40），生产必须用 0 价模型。
            r = s.post(f"{base}/v1/chat/completions",
                       headers={**h, "Content-Type": "application/json"},
                       json={"model": os.getenv("POLL_MODEL", "capcut-poll"),
                             "messages": [{"role": "user", "content": f"@query {task_id}"}]},
                       timeout=120)
            try:
                txt = str(((r.json().get("choices") or [{}])[0].get("message") or {})
                          .get("content") or "")
            except Exception:
                txt = r.text[:300]
            m = re.search(r"状态[:：]\s*([A-Za-z_]+)", txt)
            u = re.search(r"(https?://\S+\.mp4)", txt)
            st = m.group(1) if m else ("completed" if u else f"http{r.status_code}")
            d = {"status": st, "url": u.group(1) if u else None, "raw": txt}
        else:
            r = s.get(f"{base}/v1/generations/{task_id}", headers=h, timeout=60)
            try:
                d = r.json()
            except Exception:
                print(f"[{int(time.time()-t0):>4}s] HTTP {r.status_code} {r.text[:200]}")
                time.sleep(interval)
                continue
        st = d.get("status") or d.get("task_status")
        if st != last:
            print(f"[{int(time.time()-t0):>4}s] status={st}", flush=True)
            last = st
        if st in ("succeeded", "success", "completed", "done", "failed", "error"):
            print(json.dumps(d, ensure_ascii=False, indent=1)[:2000])
            json.dump(d, open(os.path.join(ROOT, "relay", f"_{task_id}.json"), "w"),
                      ensure_ascii=False, indent=1)
            return
        time.sleep(interval)
    print(f"超时未出片（{max_poll}s）")


if __name__ == "__main__":
    main()
