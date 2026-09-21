#!/usr/bin/env python3
"""零成本验证：Seedance 2.5 在网关里的接入是否完整（不连上游、不花积分）。

复现的旧 bug（2026-09-16 修复）：
  1. `main.seed_capcut_models()` 自带一份种子列表，只含 2.0-mini/2.0/1.0-fast
     → 全新部署启动后 `/v1/models` 查不到 2.5。
  2. 参考素材上限覆写表只存在于 `capcut_channel.sync_catalog` 内部，
     `service.ref_limits_of` 不查它 → 未经官方目录同步的 2.5 掉回默认 9/3/3。

用法（在 relay 目录下）：
    <venv>/bin/python _verify_2_5_wiring.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 必须在 import app.* 之前指向一个干净的临时库，避免污染真实 DATA_DIR
_TMP = tempfile.mkdtemp(prefix="verify_2_5_")
os.environ["DATA_DIR"] = _TMP

from app.config import load_secret_key  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import ModelEntry  # noqa: E402
from app.security import init_security  # noqa: E402
from app.service import gen_limits_of, ref_limits_of  # noqa: E402

init_security(load_secret_key())
init_db()

from app.main import seed_capcut_models  # noqa: E402

MID = "sd-seedance-2.5"
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{('  ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


print(f"临时库: {_TMP}")
print("\n1) 首启播种是否包含 2.5")
seed_capcut_models()
db = SessionLocal()
rows = db.query(ModelEntry).order_by(ModelEntry.model_id).all()
ids = [m.model_id for m in rows]
print("   播种结果:", ids)
check("2.5 出现在首启播种列表", MID in ids)
check("原 3 个种子未被破坏", {"sd-seedance-2.0-mini", "sd-seedance-2.0",
                              "sd-seedance-1.0-fast"} <= set(ids))
check("对外模型名不含上游品牌（全部 sd- 前缀）",
      all(i.startswith("sd-") for i in ids), f"实得 {ids}")

print("\n2) 未经官方目录同步时，2.5 的上限解析")
m = db.query(ModelEntry).filter(ModelEntry.model_id == MID).first()
if not m:
    check("能取到 2.5 条目", False)
else:
    check("catalog 为空（模拟未同步）", not (m.catalog or {}), f"catalog={bool(m.catalog)}")
    rl, gl = ref_limits_of(m), gen_limits_of(m)
    print("   ref_limits:", rl)
    print("   gen_limits:", {k: gl[k] for k in ("resolutions", "durations", "max_duration")})
    check("参考上限 = 30/10/10（实测覆写）",
          (rl["image"], rl["video"], rl["audio"]) == (30, 10, 10),
          f"实得 {rl['image']}/{rl['video']}/{rl['audio']}")
    check("来源标记为 override", rl.get("_from") == "override", f"实得 {rl.get('_from')}")
    check("分辨率 = 480/720p（2026-09-16 运营决策：1080p 不开放）",
          gl["resolutions"] == [480, 720])
    check("最长时长 30s", gl["max_duration"] == 30)
    check("时长档位齐全", gl["durations"] == [5, 8, 10, 12, 15, 18, 20, 25, 30])

print("\n3) 手工新建的 2.5（无 catalog、无后台覆写）也应拿到正确上限")
db.query(ModelEntry).filter(ModelEntry.model_id == MID).delete()
db.commit()
manual = ModelEntry(model_id=MID, display_name="Seedance 2.5", provider="capcut",
                    mcp_tool="seedance_2.5", mtype="video", enabled=True,
                    estimated_cost=0.0, timeout_seconds=900, auto_registered=False)
db.add(manual)
db.commit()
rl2 = ref_limits_of(manual)
print("   ref_limits:", rl2)
check("手工新建的 2.5 参考上限同为 30/10/10",
      (rl2["image"], rl2["video"], rl2["audio"]) == (30, 10, 10),
      f"实得 {rl2['image']}/{rl2['video']}/{rl2['audio']}")

print("\n4) 后台显式覆写仍应压过内置覆写（优先级不被破坏）")
manual.ref_limits_text = '{"image": 12, "video": 4, "audio": 2}'
db.commit()
db.refresh(manual)
rl3 = ref_limits_of(manual)
print("   ref_limits:", rl3)
check("后台覆写生效（12/4/2）",
      (rl3["image"], rl3["video"], rl3["audio"]) == (12, 4, 2),
      f"实得 {rl3['image']}/{rl3['video']}/{rl3['audio']}")
check("来源标记为 manual", rl3.get("_from") == "manual", f"实得 {rl3.get('_from')}")

print("\n5) 无覆写模型的模型不受影响（仍走默认 9/3/3）")
other = db.query(ModelEntry).filter(
    ModelEntry.model_id == "sd-seedance-2.0").first()
if other is None:  # 理论上首启已播种，兜底建一个
    other = ModelEntry(model_id="sd-seedance-2.0", display_name="Seedance 2.0",
                       provider="capcut", mcp_tool="seedance_2.0", mtype="video",
                       enabled=True)
    db.add(other)
    db.commit()
rl4 = ref_limits_of(other)
print("   2.0 ref_limits:", rl4)
check("2.0 仍为默认 9/3/3",
      (rl4["image"], rl4["video"], rl4["audio"]) == (9, 3, 3),
      f"实得 {rl4['image']}/{rl4['video']}/{rl4['audio']}")

print("\n6) 覆写表只有一份（service），capcut_channel 不再自带")
import app.capcut_channel as cc  # noqa: E402
import app.service as sv  # noqa: E402
check("service 里有 _REF_LIMIT_OVERRIDES", hasattr(sv, "_REF_LIMIT_OVERRIDES"))
check("capcut_channel 里已无同名表（避免漂移）", not hasattr(cc, "_REF_LIMIT_OVERRIDES"))
check("capcut_channel 种子表含 2.5", any(s[0] == MID for s in cc.CAPCUT_MODEL_SEEDS))

print("\n7) 对外别名 sd-seedance-<版本>-<分辨率> 解析（客户端按分辨率选模型）")
from app.routers.openai import resolve_model_alias  # noqa: E402
for alias_name, want_ver, want_res in [
        ("sd-seedance-2.0-480p", "2.0", "480p"),
        ("sd-seedance-2.0-720p", "2.0", "720p"),
        ("sd-seedance-2.5-480p", "2.5", "480p"),
        ("sd-seedance-2.5-720p", "2.5", "720p")]:
    got = resolve_model_alias(alias_name)
    base = got[0] if got else ""
    check(f"{alias_name} -> {want_ver}/{want_res}",
          bool(got) and got[1] == want_res and base in ids,
          f"实得 {got}（基准模型须在播种列表里）")
check("别名基准名不含上游品牌",
      all((resolve_model_alias(f"sd-seedance-{v}-{r}p") or ("", ""))[0].startswith("sd-")
          for v in ("2.0", "2.5") for r in ("480", "720")))
check("非别名模型名不被误判", resolve_model_alias("sd-seedance-2.0") is None)

print("\n" + "=" * 60)
if failures:
    print(f"❌ {len(failures)} 项未通过:")
    for f in failures:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过：2.5 的首启播种 / 上限解析 / 覆写优先级 均已正确接入")
