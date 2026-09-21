"""零成本校验：按模型的生成能力上限（分辨率/时长）。

不触达 Upstream、不消耗积分：只跑纯函数（gen_limits_of / build_capcut_args）。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "relay_data"))

from app.capcut_channel import build_capcut_args, clamp_duration, normalize_resolution, split_size  # noqa: E402
from app.service import gen_limits_for_key, gen_limits_of, gen_limits_text_of, normalize_gen_limits  # noqa: E402

FAIL = []
OK = []


def check(name, got, want):
    (OK if got == want else FAIL).append(f"{name}: got={got!r} want={want!r}")


class M:
    """模型桩：只需要 param_template / mcp_tool / gen_limits。"""

    def __init__(self, key, tpl=None, gen_limits=None, mid=None, provider="capcut"):
        self.mcp_tool = key
        self.model_id = mid or f"capcut-{key}"
        self.param_template = tpl or {}
        self.gen_limits = gen_limits or {}
        self.ref_limits = {}
        self.catalog = {}
        self.provider = provider


# ---------- 1. 上限解析 ----------
d52 = gen_limits_for_key("seedance_2.5")
check("2.5 来源", d52["_from"], "override")
check("2.5 分辨率", d52["resolutions"], [480, 720])
check("2.5 时长档", d52["durations"], [5, 8, 10, 12, 15, 18, 20, 25, 30])
check("2.5 上限", d52["max_duration"], 30.0)

d20 = gen_limits_for_key("seedance_2.0")
check("2.0 来源", d20["_from"], "override")  # 2026-09-16 起 2.0 也有内置覆写（钉死 480/720）
check("2.0 分辨率", d20["resolutions"], [480, 720])
check("2.0 时长", (d20["durations"], d20["min_duration"], d20["max_duration"]), ([], 2.0, 15.0))

check("空配置=默认", gen_limits_of(M("seedance_2.0_mini"))["max_duration"], 15.0)

# 后台手工覆写优先
manual = normalize_gen_limits({"resolutions": ["480p", 720, 1080], "durations": [5, 10], "max_duration": 10})
check("手工覆写解析", (manual["resolutions"], manual["durations"], manual["max_duration"]),
      ([480, 720, 1080], [5.0, 10.0], 10.0))
check("手工覆写生效", gen_limits_of(M("seedance_2.0", gen_limits={"resolutions": [720], "max_duration": 8}))["resolutions"], [720])
check("自相矛盾配置被纠正", normalize_gen_limits({"durations": [30], "max_duration": 5})["max_duration"], 30.0)

# ---------- 2. 分辨率吸附 ----------
check("480p 默认", normalize_resolution("480p"), 480)
check("1080p 默认档", normalize_resolution("1080p"), 720)
check("1080p 放开后", normalize_resolution("1080p", [480, 720, 1080]), 1080)
check("split_size 1080p+画幅(放开)", split_size("1080p 16:9", [480, 720, 1080]), (1080, "16:9"))
check("split_size 1080p+画幅(默认)", split_size("1080p 16:9"), (720, "16:9"))
check("split_size 1920x1080(放开)", split_size("1920x1080", [480, 720, 1080]), (1080, "16:9"))
check("split_size 480p9:16(默认)", split_size("480p 9:16"), (480, "9:16"))

# ---------- 3. 时长吸附 ----------
check("时长 30s(2.5档)", clamp_duration(30, d52["durations"], 2, 30), 30.0)
check("时长 25s(2.5档)", clamp_duration(25, d52["durations"], 2, 30), 25.0)
check("时长 30s->默认上限", clamp_duration(30, [], 2, 15), 15.0)
check("时长 6s 吸附到 5 档", clamp_duration(6, [5, 8], 2, 30), 5.0)
check("时长 7s 吸附到 8 档", clamp_duration(7, [5, 8], 2, 30), 8.0)
check("时长下限", clamp_duration(0.5, [], 2, 15), 2.0)
check("时长非法值兜底", clamp_duration("abc", [], 2, 15), 5.0)

# ---------- 4. build_capcut_args（真实入口） ----------
m25 = M("seedance_2.5", {"resolution": "480p", "ratio": "9:16", "duration": 5})
a = build_capcut_args(m25, "a cat", {"size": "1080p 16:9", "duration": 30})
check("2.5 分辨率生效(1080p 请求被夹到 720p)", a["resolution"], "720p")
check("2.5 时长生效", a["duration_s"], 30.0)
check("2.5 画幅生效", a["ratio"], "16:9")

a = build_capcut_args(m25, "a cat", {"size": "1920x1080", "duration": 25})
check("2.5 像素写法生效", (a["resolution"], a["duration_s"]), ("720p", 25.0))

a = build_capcut_args(m25, "a cat", {"duration": 6})
check("2.5 吸附最近档", a["duration_s"], 5.0)
a = build_capcut_args(m25, "a cat", {"duration": 30, "options": {"duration": 30}})
check("2.5 options 传参", a["duration_s"], 30.0)

m20 = M("seedance_2.0", {"resolution": "480p", "ratio": "9:16", "duration": 5})
a = build_capcut_args(m20, "a cat", {"size": "1080p 16:9", "duration": 30})
check("2.0 分辨率仍夹取", a["resolution"], "720p")
check("2.0 时长仍夹取", a["duration_s"], 15.0)

m10 = M("seedance_1.0_fast", {"resolution": "480p", "ratio": "16:9", "duration": 5})
a = build_capcut_args(m10, "a cat", {"size": "720p 9:16", "duration": 12})
check("1.0 时长 12s 保留", a["duration_s"], 12.0)
check("1.0 分辨率保留", a["resolution"], "720p")

# 模型模板默认值也走同一套夹取（模板给 30s 但模型只到 15s）
a = build_capcut_args(M("seedance_2.0", {"duration": 30}), "x", {})
check("模板默认也夹取", a["duration_s"], 15.0)
a = build_capcut_args(M("seedance_2.5", {"duration": 30}), "x", {})
check("2.5 模板默认 30s 放行", a["duration_s"], 30.0)

# ---------- 5. 展示文本 ----------
print("gen_limits_text(2.5):", gen_limits_text_of(M("seedance_2.5")))
print("gen_limits_text(2.0):", gen_limits_text_of(M("seedance_2.0")))

print("\n--- 通过 %d 项 ---" % len(OK))
for line in OK:
    print("  PASS", line)
if FAIL:
    print("\n--- 失败 %d 项 ---" % len(FAIL))
    for line in FAIL:
        print("  FAIL", line)
    sys.exit(1)
print("\n全部通过 ✅")
