#!/usr/bin/env python3
"""CapCut 生成任务「零成本预算预检」。

## 计费规则（2026-09-15 实测钉死）

    计费秒数 = 输出时长(秒) + Σ(参考**视频**素材时长)
    积分     = 计费秒数 × 单价(分辨率)

- **参考图片、参考音频不计费**（只占用素材个数上限）。
- ⚠️ 只请求 5s 却扣了 8s，就是因为把参考视频的 3s 也算进去了 —— 不是"最短计费 8s"。
- Seedance 2.5 r2v 单价（积分/秒）：480p = 17、720p = 37、1080p = 72。
- 附加权益（高级模型 / ≥10s 长时长 / 720p 超清）对 vip 账号是 **0 分**，不额外收费。

## 预检接口（只读，不扣分）

    POST https://commerce-api-sg.capcut.com/commerce/v3/benefits/batch_check_withhold_deduct
    body {"strict":true,"action_type":1,"item_list":[{"amount":<计费秒数>, ...}]}
响应是**双层 JSON**（外层 ret/response，response 里再 json.loads），
`item_list[0].credits_amount` 即总积分，`status` 1=可付 / 2=不可付。
实测调用前后余额不变 ⇒ 可随意试算。

用法：
    python _capcut_price.py 720p 20 --video-seconds 6      # 20s 输出 + 6s 参考视频
    python _capcut_price.py 480p 30                        # 无参考视频
    python _capcut_price.py --video-files ../materials     # 自动累加目录里视频素材时长
    python _capcut_price.py --table                        # 打印预算表
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, "/Users/sun/WorkBuddy/2026-09-13-20-30-22/relay")

from app import security  # noqa: E402
from app.config import load_secret_key  # noqa: E402

security.init_security(load_secret_key())

from app.capcut_channel import client_from_account  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Account  # noqa: E402

DEDUCT_URL = ("https://commerce-api-sg.capcut.com/commerce/v3/benefits/"
              "batch_check_withhold_deduct?aid=348188")
RESOURCE_TYPE = "aigc"
RESOURCE_ID = "material_generation"
VID_EXT = (".mp4", ".mov", ".mkv", ".webm")


def benefit_type(model: str, resolution: str, mode: str) -> str:
    """拼计费权益键。

    ⚠️ 分辨率后缀**必须带 p**：`..._r2v_720p` 对，`..._r2v_720` 会返回 0 分（假通过）。
    """
    res = resolution.rstrip("p") + "p"
    if model.startswith("seedance_2.5") or model in ("2.5", "seedance25"):
        key = "material_gen_video_by_seedance25"
    elif model in ("2.0mini", "seedance20mini"):
        key = "material_gen_video_by_seedance20mini"
    else:
        key = f"material_gen_video_by_{model}"
    return f"{key}_{mode}_{res}"


def video_seconds(files: list[str]) -> int:
    """累加参考视频时长（秒，向上取整），即计费里真正会加钱的那部分。"""
    import imageio
    from app.capcut_direct_task import DirectTaskClient
    total = 0.0
    for p in files:
        d = DirectTaskClient.probe_mp4_seconds(p)
        if d <= 0:
            try:
                m = imageio.get_reader(p).get_meta_data()
                d = float(m.get("duration") or 0)
            except Exception:  # noqa: BLE001
                d = 0.0
        total += d
    return int(total + 0.999)


def collect_videos(path: str) -> list[str]:
    if os.path.isfile(path):
        return [path]
    return sorted(os.path.join(path, f) for f in os.listdir(path)
                  if f.lower().endswith(VID_EXT) and "_up" not in f)


def check(cli, bt: str, seconds: int) -> dict:
    body = {"strict": True, "action_type": 1,
            "item_list": [{"amount": seconds, "resource_type": RESOURCE_TYPE,
                           "resource_id": RESOURCE_ID, "benefit_type": bt}]}
    j = cli.up.sess.post(DEDUCT_URL, json=body, headers=cli._headers("query"),
                         timeout=40).json()
    try:
        return json.loads(j.get("response") or "{}")
    except Exception:  # noqa: BLE001
        return {}


def unit_price(cli, bt: str) -> int:
    """单价 = 1 秒的报价（线性，无起步价）。"""
    inner = check(cli, bt, 1)
    return (inner.get("item_list") or [{}])[0].get("credits_amount") or 0


def pick_account(sess, account_id: int | None) -> Account:
    if account_id:
        acc = sess.get(Account, account_id)
        if not acc:
            raise SystemExit(f"账号 #{account_id} 不存在")
        return acc
    rows = [a for a in sess.query(Account).all()
            if (a.provider or "") == "capcut" and a.status != "disabled"]
    if not rows:
        raise SystemExit("库里没有可用的 CapCut 账号")
    return max(rows, key=lambda a: a.coin_balance or 0)


def main() -> None:
    ap = argparse.ArgumentParser(description="CapCut 生成任务积分预检（零成本）")
    ap.add_argument("resolution", nargs="?", default="720p", help="480p / 720p / 1080p")
    ap.add_argument("duration", nargs="?", type=int, default=30, help="输出时长（秒）")
    ap.add_argument("--video-seconds", type=int, default=0,
                    help="参考**视频**素材时长之和（计费含此项）")
    ap.add_argument("--video-files", default=None,
                    help="参考视频目录或文件；给了就自动累加时长（覆盖 --video-seconds）")
    ap.add_argument("--model", default="seedance_2.5")
    ap.add_argument("--mode", default="r2v", choices=["r2v", "i2v"], help="r2v=带参考素材")
    ap.add_argument("--account-id", type=int, default=None)
    ap.add_argument("--table", action="store_true", help="打印预算表")
    a = ap.parse_args()

    sess = SessionLocal()
    acc = pick_account(sess, a.account_id)
    cli = client_from_account(acc)
    bal = cli.get_user_credit().get("total")
    print(f"账号 #{acc.id} {acc.name}  余额 {bal} 分")

    if a.table:
        print(f"\n按输出时长（无参考视频）可跑的最大值：")
        print(f"{'分辨率':<8}{'单价':>6}{'可负担计费秒':>14}{'纯输出可用':>12}")
        for res in ("480p", "720p", "1080p"):
            u = unit_price(cli, benefit_type(a.model, res, a.mode))
            if not u:
                print(f"{res:<8}{'?':>6}   （权益键不可用）")
                continue
            secs = bal / u
            print(f"{res:<8}{u:>6}{secs:>14.1f}{int(secs):>12}s")
        print("\n注：参考视频时长会占用同一份额度；参考图片/音频免费。")
        return

    vsec = a.video_seconds
    vfiles = []
    if a.video_files:
        vfiles = collect_videos(a.video_files)
        vsec = video_seconds(vfiles)

    bt = benefit_type(a.model, a.resolution, a.mode)
    amount = a.duration + vsec
    inner = check(cli, bt, amount)
    it = (inner.get("item_list") or [{}])[0]
    need = it.get("credits_amount") or 0
    status = inner.get("status")

    print(f"\n请求   : {a.model} / {a.resolution} / {a.mode}")
    print(f"计费秒 : 输出 {a.duration}s + 参考视频 {vsec}s = {amount}s"
          + (f"（{len(vfiles)} 个视频文件）" if vfiles else ""))
    print(f"权益键 : {bt}")
    print(f"预检   : status={status}（1=可付, 2=不可付）  需要 {need} 分")
    if status == 1:
        print(f"结论   : {'✅ 余额充足' if need <= bal else '❌ 差 %d 分' % (need - bal)}"
              f"（跑完剩 {bal - need} 分）")
    else:
        print("结论   : ❌ 不可付。检查分辨率后缀是否带 p / 参考视频总时长是否太长")


if __name__ == "__main__":
    main()
