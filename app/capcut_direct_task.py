# -*- coding: utf-8 -*-
"""
CapCut 编辑器直连生成任务（common_task/new + common_task/query）
来源: 2026-09-15 HAR 逆向 (14---视频生成www.capcut.com.har)
特点: 不经过 ai_lab agent 会话, 无确认卡/积分卡, 直接创建任务并轮询出片
"""
import json
import os
import time
import uuid
import re
from urllib.parse import urlencode

from .capcut_upload_protocol import PureUploader, EDIT_API
from .capcut_antibot import antibot_suffix, describe as antibot_describe
from .capcut_signs import (
    BUILTIN_SIGNS,
    DEFAULT_APPVR,
    DEFAULT_PF,
    KEY_PATHS,
    TDID_KEYS,
    effective_signs,
    make_sign,
    mint_sign,
    normalize_signs,
    risk_hint,
    verify_sign,
)

# HAR 观测到的公共查询参数（msToken/X-Bogus 实测可省, 风控未拦）
BABI_PARAM = '{"scenario":"video_editor","feature_key":"ai_video_generation","first_feature_entrance":"editor","feature_entrance_detail":"editor-editpage-scene"}'

# 签名算法已从前端 bundle 逆向出来（2026-09-15），见 app/capcut_signs.py：
#     sign = md5("9e2c|" + 路径末7字符 + "|" + pf + "|" + appvr + "|" + device-time + "|" + tdid + "|11ac")
# 输入不含任何账号密钥 → **可以本地随时生成**，不必再从 HAR 抓。
# HAR 里 87 条真实样本 100% 复现；仓库里的旧常量也全部可由公式复算。
# 于是「全局常量 SIGN_PAIRS」不再是硬阻塞：
#   CAPCUT_SIGN_MODE=auto  （默认）账号后台存过签名就用它，否则按当前时间现签
#   CAPCUT_SIGN_MODE=mint  永远现签（推荐：device-time 永远是新的）
#   CAPCUT_SIGN_MODE=static 只用后台/内置常量（排查用）
# 另注：不能携带 tdid 头(带旧 tdid 会 1014); did 头=web_id 必须带。
SIGN_MODE = (os.environ.get("CAPCUT_SIGN_MODE") or "auto").strip().lower()

# upload_sign 的历史 tdid（前端会带，且 tdid 参与签名，所以必须与发出的一致）
UPLOAD_SIGN_TDID = "178931314028893759"

# 兼容旧写法：{key: (sign, device_time)}
SIGN_PAIRS = {k: (v["sign"], v["device_time"]) for k, v in BUILTIN_SIGNS.items()}

# 商业化接口（积分/权益）
COMMERCE_API = "https://commerce-api-sg.capcut.com"

# 参考素材 imageX 服务（chat_upload_sign -> ccagentfilethread 桶）
REF_SERVICE_ID = "ccagentfilethread"


def _audio_duration_ms(filepath):
    """尽力探测音频时长(ms); 失败返回 0"""
    try:
        from mutagen import File as MFile
        m = MFile(filepath)
        if m is not None and m.info.length:
            return int(m.info.length * 1000)
    except Exception:
        pass
    try:
        import wave, contextlib
        with contextlib.closing(wave.open(filepath, 'rb')) as w:
            return int(w.getnframes() / w.getframerate() * 1000)
    except Exception:
        return 0


class DirectTaskClient:
    def __init__(self, cookie_file, signs=None, sign_mode=None, pf=DEFAULT_PF,
                 appvr=DEFAULT_APPVR, tdid=None):
        self.up = PureUploader(cookie_file)
        self.s = self.up.sess
        self.web_id = self.up.web_id          # did = web_id
        self.region = (self.up.region or 'PK').upper()
        self.bind_id = str(uuid.uuid4()).upper()
        # 签名来源：后台按账号存的 > 内置常量；auto/mint 模式下**现签**（device-time 永远是新的）
        self.account_signs = normalize_signs(signs or {})
        self.sign_mode = (sign_mode or SIGN_MODE or "auto").lower()
        self.pf = str(pf)
        self.appvr = str(appvr)
        self.tdid = str(tdid if tdid is not None else
                        (self.account_signs.get("upload_sign") or {}).get("tdid")
                        or UPLOAD_SIGN_TDID)
        self.sign_notes = {}                  # key -> 本次用的来源（排查用）
        # upload_sign 系列：tdid 参与签名计算（但不一定作为头发出去，沿用历史行为）
        self.up.sign_headers = self._upload_sign_headers()

    # ---------- 签名 ----------
    def sign_for(self, pair_key="new"):
        """返回 (sign, device_time, tdid_in_sign, 来源来源标记)。

        auto : 账号后台存过该接口的签名 → 用它；否则按当前时间现签
        mint : 永远现签
        static: 只用后台/内置常量（不再现签）
        """
        ov = self.account_signs.get(pair_key)
        if ov and self.sign_mode in ("auto", "static"):
            return ov["sign"], ov["device_time"], ov.get("tdid") or "", "account"
        if self.sign_mode == "static":
            b = BUILTIN_SIGNS.get(pair_key) or {}
            return b.get("sign", ""), b.get("device_time", ""), b.get("tdid") or "", "builtin"
        tdid = self.tdid if pair_key in ("upload_sign", "upload_sign_ref") else ""
        e = mint_sign(pair_key, pf=self.pf, appvr=self.appvr, tdid=tdid)
        return e["sign"], e["device_time"], e.get("tdid") or "", "mint"

    def _upload_sign_headers(self):
        sign, dt, tdid, src = self.sign_for("upload_sign")
        self.sign_notes["upload_sign"] = src
        h = {"sign": sign, "device-time": dt, "pf": self.pf, "appvr": self.appvr, "sign-ver": "1"}
        if tdid:
            h["tdid"] = tdid
        return h

    def signs_report(self):
        """当前各接口用到的签名来源 + 本地可复算校验（零成本自查）。"""
        rows = []
        for k in KEY_PATHS:
            ov = self.account_signs.get(k)
            if k == "upload_sign":
                sign, dt, tdid, src = (self.up.sign_headers["sign"], self.up.sign_headers["device-time"],
                                       self.up.sign_headers.get("tdid", ""), self.sign_notes.get(k, "?"))
            else:
                sign, dt, tdid, src = self.sign_for(k)
            rows.append({
                "key": k, "source": src, "device_time": dt,
                "sign": sign, "tdid": tdid,
                "verified": verify_sign(KEY_PATHS[k], sign, dt, self.pf, self.appvr, tdid),
                "stored": bool(ov),
            })
        return rows

    def _url(self, path):
        qs = urlencode({
            'babi_param': BABI_PARAM,
            'aid': '348188',
            'device_platform': 'web',
            'region': self.region,
            'web_id': self.web_id,
        })
        # 反爬参数（X-Gnarly 等，shark 风控前置校验）——见 app/capcut_antibot.py
        return f'{EDIT_API}{path}?{qs}{antibot_suffix()}'

    def _headers(self, pair_key="new"):
        sign, dt, tdid, src = self.sign_for(pair_key)
        self.sign_notes[pair_key] = src
        h = {
            "sign": sign,
            "device-time": dt,
            "pf": self.pf, "appvr": self.appvr, "sign-ver": "1",
            "Content-Type": "application/json",
            "did": str(self.web_id),
            "store-country-code": self.region.lower(),
        }
        # ⚠️ upload_sign / upload_sign_ref 的 sign 是「带 tdid 参与哈希」算出来的，
        # 请求头必须同时带上 tdid，否则服务端校验不过 → 回伪装的 ret=1014 "system busy"
        # （实测：不带 tdid 头 1014，带上即 ret=0 success）
        if pair_key in TDID_KEYS and tdid:
            h["tdid"] = tdid
        return h

    # ---------- 参考素材上传 ----------
    def chat_upload_sts(self):
        """chat_upload_sign(biz=thread) -> ccagentfilethread 桶的 imageX STS"""
        h = self._headers("chat_upload_sign")
        url = f'{EDIT_API}/lv/v2/intelligence/file/chat_upload_sign?' + \
              f'aid=348188&device_platform=web&region={self.region}&web_id={self.web_id}{antibot_suffix()}'
        r = self.s.post(url, json={"biz": "thread"}, headers=h, timeout=30)
        j = r.json()
        if str(j.get("ret")) != "0":
            raise RuntimeError(f'chat_upload_sign 失败: {json.dumps(j, ensure_ascii=False)[:300]}')
        d = j["data"]
        return {"access_key_id": d["access_key_id"],
                "secret_access_key": d["secret_access_key"],
                "session_token": d["session_token"],
                "space_name": d.get("service_id", REF_SERVICE_ID),
                "expired_time": d.get("expired_time")}

    def upload_reference_image(self, filepath):
        """上传参考图 -> 返回 references 条目 {uri,width,height,sizeBytes,format,name}"""
        import zlib
        from .capcut_upload_protocol import IMAGEX, rand_s
        up = self.up
        data = open(filepath, "rb").read()
        size = len(data)
        fmt = os.path.splitext(filepath)[1].lstrip(".").lower() or "png"

        # 用 thread STS 走 imageX（线程局部覆盖 STS，可并发上传）
        sts = self.chat_upload_sts()
        up.use_sts(sts)
        try:
            q = {"Action": "ApplyImageUpload", "Version": "2018-08-01",
                 "ServiceId": REF_SERVICE_ID, "FileSize": size,
                 "s": rand_s(), "device_platform": "web"}
            url = IMAGEX + "/?" + "&".join(f"{k}={v}" for k, v in q.items())
            r = up.sess.get(url, headers=up._aws4_headers("GET", q, kind="thread"), timeout=30)
            r.raise_for_status()
            result = r.json()["Result"]
            store = result["UploadAddress"]["StoreInfos"][0]
            store_uri, auth = store["StoreUri"], store["Auth"]
            session_key = (result.get("SessionKey")
                           or result["UploadAddress"].get("SessionKey") or "")
            crc = format(zlib.crc32(data) & 0xFFFFFFFF, "08x")
            up_url = f"https://tos-d-alisg16-up.byteoversea.com/upload/v1/{store_uri}"
            r = up.sess.post(up_url, data=data, headers={
                "Authorization": auth, "Content-Type": "application/octet-stream",
                "Content-Crc32": crc}, timeout=120)
            if r.status_code != 200:
                raise RuntimeError(f"TOS 直传失败 {r.status_code}: {r.text[:200]}")
            q2 = {"Action": "CommitImageUpload", "Version": "2018-08-01",
                  "ServiceId": REF_SERVICE_ID}
            body = json.dumps({"SessionKey": session_key}).encode()
            url2 = IMAGEX + "/?" + "&".join(f"{k}={v}" for k, v in q2.items())
            hdrs2 = up._aws4_headers("POST", q2, body, kind="thread")
            hdrs2["Content-Type"] = "application/json"
            commit = up.sess.post(url2, data=body, headers=hdrs2, timeout=30).json()
            res = commit["Result"]["Results"][0]
            if res.get("UriStatus") != 2000:
                raise RuntimeError(f"Commit 失败 UriStatus={res.get('UriStatus')}")
            uri = res["Uri"]
        finally:
            up.clear_sts_override()

        w = h = 0
        try:
            from PIL import Image
            with Image.open(filepath) as im:
                w, h = im.size
        except Exception:
            pass
        ref = {"id": str(uuid.uuid4()), "name": os.path.basename(filepath),
               "type": "image", "uri": uri, "resourceUrl": uri,
               "width": w, "height": h, "sizeBytes": size, "format": fmt}
        print(f"[ref-image] {uri} ({w}x{h})")
        return ref

    @staticmethod
    def probe_mp4_seconds(filepath) -> float:
        """纯 Python 读 MP4 的 mvhd 时长（秒）。读不到返回 0。

        为什么需要它：VOD Commit 回带的 `VideoMeta.Duration` 有时是 0/缺失，
        直接把 0 写进 references[].durationMs 会被上游拒：
        `31003 get input video duration failed ... invalid video duration: 0`。
        """
        import os
        import struct

        CHUNK = 4 * 1024 * 1024
        try:
            size = os.path.getsize(filepath)
            with open(filepath, "rb") as f:
                d = f.read(CHUNK)
                i = d.find(b"mvhd")
                if i < 0 and size > len(d):
                    # moov/mvhd 常被放在文件**末尾**（大文件尤其如此，如 10MB 成片
                    # mvhd 在偏移 ~10.0MB 处）。只读头部会漏 → durationMs=0 → 上游 31003。
                    tail_len = min(CHUNK, size)
                    f.seek(size - tail_len)
                    d = f.read(tail_len)
                    i = d.find(b"mvhd")
            if i < 0:
                return 0.0
            ver = d[i + 4]
            if ver == 0:
                ts = struct.unpack(">I", d[i + 16:i + 20])[0]
                du = struct.unpack(">I", d[i + 20:i + 24])[0]
            else:
                ts = struct.unpack(">I", d[i + 24:i + 28])[0]
                du = struct.unpack(">Q", d[i + 28:i + 36])[0]
            return round(du / ts, 3) if ts else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    @staticmethod
    def _ensure_video_pixels(filepath, min_pixels=407696, target=(864, 480)):
        """seedance r2v 要求参考视频像素 >= 407696; 不足时抽帧放大重编码。

        ⚠️ min_pixels 必须正好是官方门槛 407696：曾经写成 430000，而放大目标
        864x480 = 414720 < 430000，导致「放大后仍判定过小 → 无限套娃重编码」
        （会生成 video_01_up864x480_up864x480_up864x480.mp4 这类文件，且二次编码后
        元数据容易坏）。所以这里同时对已带 `_up` 的文件直接放行，杜绝套娃。
        """
        try:
            import imageio
            meta = imageio.get_reader(filepath).get_meta_data()
            w, h = meta.get("size", (0, 0))
            if w * h >= min_pixels:
                return filepath
            base = os.path.basename(filepath)
            if "_up" in base:          # 已经是放大产物，不再套娃
                return filepath
            import numpy as np
            from PIL import Image
            out = os.path.splitext(filepath)[0] + f'_up{target[0]}x{target[1]}.mp4'
            if os.path.exists(out):    # 复用已有放大产物，不重复编码
                return out
            fps = meta.get('fps') or 24
            r = imageio.get_reader(filepath)
            wr = imageio.get_writer(out, fps=fps, codec='libx264', quality=7)
            for fr in r:
                im = Image.fromarray(fr).resize(target, Image.LANCZOS)
                wr.append_data(np.array(im))
            wr.close()
            print(f"[upscale] {w}x{h} -> {target[0]}x{target[1]}: {out}")
            return out
        except Exception as e:
            print(f"[upscale] 跳过({e})")
            return filepath

    def upload_reference_video(self, filepath):
        """参考视频必须走 digital_cameo 空间(生成管线读不到 capcut_videocut_web_sg 的 vid)
        复用 PureUploader 的 VOD 三段式, 但 STS/SpaceName 换成 digital_cameo
        注意: seedance r2v 要求参考视频像素数 >= 407696, 过小自动放大到 864x480"""
        import zlib
        from .capcut_upload_protocol import VOD_HOST, rand_s, VOD_REGION, VOD_SERVICE, UA
        up = self.up

        filepath = self._ensure_video_pixels(filepath)

        # 1) STS: upload_sign biz=digital_cameo
        h = self._headers("upload_sign_ref")
        h['Content-Type'] = 'application/json'
        url = f'{EDIT_API}/lv/v1/upload_sign?' + \
              f'aid=348188&device_platform=web&region={self.region}&web_id={self.web_id}{antibot_suffix()}'
        j = up.sess.post(url, json={"key_version": "v5", "biz": "digital_cameo"},
                         headers=h, timeout=30).json()
        if str(j.get("ret")) != "0":
            raise RuntimeError(f'upload_sign(digital_cameo) 失败: {json.dumps(j)[:300]}')
        d = j["data"]
        space = d.get("space_name") or "digital_cameo"
        ref_sts = {"access_key_id": d["access_key_id"],
                   "secret_access_key": d["secret_access_key"],
                   "session_token": d["session_token"],
                   "space_name": space, "expired_time": d.get("expired_time")}
        print(f"[STS:ref-video] ok space={space}")

        data = open(filepath, "rb").read()
        size = len(data)
        # 2) Apply —— 线程局部覆盖 STS，使 _aws4_headers 用 ref STS（可并发）
        up.use_sts(ref_sts)
        try:
            q = {"Action": "ApplyUploadInner", "Version": "2020-11-19",
                 "SpaceName": space, "FileType": "video", "IsInner": "1",
                 "FileSize": size, "s": rand_s(), "device_platform": "web"}
            r = up.sess.get(VOD_HOST + "/?" + "&".join(f"{k}={v}" for k, v in q.items()),
                            headers=up._aws4_headers("GET", q, kind="vod"), timeout=30)
            r.raise_for_status()
            result = r.json()["Result"]
            node = result["InnerUploadAddress"]["UploadNodes"][0]
            store = node["StoreInfos"][0]
            store_uri, auth = store["StoreUri"], store["Auth"]
            session_key = node.get("SessionKey") or ""
            upload_host = node.get("UploadHost", "tos-my16-share.vodupload.com")
            # 3) 直传
            crc = format(zlib.crc32(data) & 0xFFFFFFFF, "08x")
            r = up.sess.post(f"https://{upload_host}/upload/v1/{store_uri}", data=data,
                             headers={"Authorization": auth, "User-Agent": UA,
                                      "Content-Type": "application/octet-stream",
                                      "Content-Crc32": crc}, timeout=300)
            if r.status_code != 200:
                raise RuntimeError(f"VOD 直传失败 {r.status_code}: {r.text[:200]}")
            # 4) Commit
            q2 = {"Action": "CommitUploadInner", "Version": "2020-11-19", "SpaceName": space}
            body = json.dumps({"SessionKey": session_key}).encode()
            hdrs2 = up._aws4_headers("POST", q2, body, kind="vod")
            hdrs2["Content-Type"] = "application/json"
            commit = up.sess.post(VOD_HOST + "/?" + "&".join(f"{k}={v}" for k, v in q2.items()),
                                  data=body, headers=hdrs2, timeout=60).json()
            res0 = commit["Result"]["Results"][0]
            vid = res0.get("Vid")
            if not vid:
                raise RuntimeError(f"Commit 失败: {json.dumps(commit)[:400]}")
            meta = res0.get("VideoMeta", {})
        finally:
            up.clear_sts_override()
        print(f"[Commit:ref-video] ok Vid={vid} {meta.get('Duration','')}s")

        fmt = os.path.splitext(filepath)[1].lstrip(".").lower() or "mp4"
        w = h = 0
        try:
            import imageio
            m = imageio.get_reader(filepath).get_meta_data()
            w, h = m.get("size", (0, 0))
        except Exception:
            pass
        # 时长优先用 VOD 的 VideoMeta；为 0/缺失时回退本地解析 mvhd
        # （0 会被上游拒：31003 invalid video duration: 0）
        dur_s = float(meta.get("Duration") or 0)
        if dur_s <= 0:
            dur_s = self.probe_mp4_seconds(filepath)
            if dur_s > 0:
                print(f"[ref-video] VideoMeta.Duration 缺失 → 本地 mvhd 读到 {dur_s}s")
        ref = {"id": str(uuid.uuid4()), "name": os.path.basename(filepath),
               "type": "video", "vid": vid, "resourceUrl": store_uri,
               "format": fmt, "sizeBytes": size,
               "durationMs": int(dur_s * 1000),
               "width": w, "height": h}
        print(f"[ref-video] {vid} ({w}x{h}) dur={dur_s}s")
        return ref

    def upload_reference_audio(self, filepath):
        """参考音频: 与参考视频同走 digital_cameo 空间(HAR 中音频 resourceUrl 也是
        tos-alisg-v-* VOD 存储), 仅 FileType=audio。
        返回 references 条目 {id,name,type:audio,vid,resourceUrl,format,sizeBytes,durationMs}"""
        import zlib
        from .capcut_upload_protocol import VOD_HOST, rand_s, UA
        up = self.up

        # 1) STS: upload_sign biz=digital_cameo (与参考视频同一签名对)
        h = self._headers("upload_sign_ref")
        h['Content-Type'] = 'application/json'
        url = f'{EDIT_API}/lv/v1/upload_sign?' + \
              f'aid=348188&device_platform=web&region={self.region}&web_id={self.web_id}{antibot_suffix()}'
        j = up.sess.post(url, json={"key_version": "v5", "biz": "digital_cameo"},
                         headers=h, timeout=30).json()
        if str(j.get("ret")) != "0":
            raise RuntimeError(f'upload_sign(digital_cameo) 失败: {json.dumps(j)[:300]}')
        d = j["data"]
        space = d.get("space_name") or "digital_cameo"
        ref_sts = {"access_key_id": d["access_key_id"],
                   "secret_access_key": d["secret_access_key"],
                   "session_token": d["session_token"],
                   "space_name": space, "expired_time": d.get("expired_time")}
        print(f"[STS:ref-audio] ok space={space}")

        data = open(filepath, "rb").read()
        size = len(data)
        # 2) Apply + 3) 直传 + 4) Commit
        # 注: digital_cameo 空间 ApplyUploadInner 不接受 FileType=audio(30402),
        # 实测用 media 可行
        up.use_sts(ref_sts)
        try:
            q = {"Action": "ApplyUploadInner", "Version": "2020-11-19",
                 "SpaceName": space, "FileType": "media", "IsInner": "1",
                 "FileSize": size, "s": rand_s(), "device_platform": "web"}
            r = up.sess.get(VOD_HOST + "/?" + "&".join(f"{k}={v}" for k, v in q.items()),
                            headers=up._aws4_headers("GET", q, kind="vod"), timeout=30)
            r.raise_for_status()
            result = r.json()["Result"]
            node = result["InnerUploadAddress"]["UploadNodes"][0]
            store = node["StoreInfos"][0]
            store_uri, auth = store["StoreUri"], store["Auth"]
            session_key = node.get("SessionKey") or ""
            upload_host = node.get("UploadHost", "tos-my16-share.vodupload.com")
            crc = format(zlib.crc32(data) & 0xFFFFFFFF, "08x")
            r = up.sess.post(f"https://{upload_host}/upload/v1/{store_uri}", data=data,
                             headers={"Authorization": auth, "User-Agent": UA,
                                      "Content-Type": "application/octet-stream",
                                      "Content-Crc32": crc}, timeout=300)
            if r.status_code != 200:
                raise RuntimeError(f"VOD 直传失败 {r.status_code}: {r.text[:200]}")
            q2 = {"Action": "CommitUploadInner", "Version": "2020-11-19", "SpaceName": space}
            body = json.dumps({"SessionKey": session_key}).encode()
            hdrs2 = up._aws4_headers("POST", q2, body, kind="vod")
            hdrs2["Content-Type"] = "application/json"
            commit = up.sess.post(VOD_HOST + "/?" + "&".join(f"{k}={v}" for k, v in q2.items()),
                                  data=body, headers=hdrs2, timeout=60).json()
            res0 = commit["Result"]["Results"][0]
            vid = res0.get("Vid")
            if not vid:
                raise RuntimeError(f"Commit 失败: {json.dumps(commit)[:400]}")
            meta = res0.get("AudioMeta") or res0.get("VideoMeta") or {}
        finally:
            up.clear_sts_override()
        dur_ms = int((meta.get("Duration") or 0) * 1000) or _audio_duration_ms(filepath)
        fmt = os.path.splitext(filepath)[1].lstrip(".").lower() or "mp3"
        ref = {"id": str(uuid.uuid4()), "name": os.path.basename(filepath),
               "type": "audio", "vid": vid, "resourceUrl": store_uri,
               "format": fmt, "sizeBytes": size, "durationMs": dur_ms}
        print(f"[ref-audio] {vid} dur={dur_ms}ms")
        return ref

    # ---------- 积分余额 ----------
    def get_user_credit(self):
        """查询当前账号积分余额 (commerce/v1/benefits/user_credit, body={})
        返回 {"total", "vip_credit", "gift_credit", "purchase_credit", "raw"}"""
        h = self._headers("user_credit")
        url = f'{COMMERCE_API}/commerce/v1/benefits/user_credit'
        r = self.s.post(url, json={}, headers=h, timeout=30)
        j = r.json()
        if str(j.get("ret")) != "0":
            raise RuntimeError(f'user_credit 失败: {json.dumps(j, ensure_ascii=False)[:300]}')
        credit = (j.get("data") or {}).get("credit") or {}
        return {
            "total": sum(v or 0 for v in credit.values() if isinstance(v, (int, float))),
            "vip_credit": credit.get("vip_credit"),
            "gift_credit": credit.get("gift_credit"),
            "purchase_credit": credit.get("purchase_credit"),
            "raw": j.get("data"),
        }

    def create_video_task(self, prompt, model='seedance_1.0_fast',
                          resolution='480p', duration_ms=5000,
                          ratio='16:9', generate_audio=True,
                          first_frame_image=None,
                          references=None,
                          dry_run=False):
        """创建任务。references 非空时走 omni_reference 参考生成模式
        (支持 image/video/audio 三种参考, 音频占位 [audio1]... HAR 实测 audio 在 image 之前)

        dry_run=True 时只组装并返回请求体，**不发请求、不消耗积分**（用于上线前预演）。
        返回 {"__dry_run__": True, "url": ..., "body": {...}}。
        """
        if references:
            vids = [r["vid"] for r in references if r["type"] == "video"]
            a_vids = [r["vid"] for r in references if r["type"] == "audio"]
            tags, vi, ii, ai = [], 0, 0, 0
            for r in references:          # HAR 顺序: audio 在 image 前
                if r["type"] == "video":
                    vi += 1; tags.append(f"[video{vi}]")
                elif r["type"] == "audio":
                    ai += 1; tags.append(f"[audio{ai}]")
                else:
                    ii += 1; tags.append(f"[image{ii}]")
            prompt = " ".join(tags) + " " + prompt
            follow = {
                "inputPrompt": {"prompt": prompt, "references": references},
                "generationMode": "omni_reference",
                "modelId": model,
                "generate_id": str(uuid.uuid4()),
                "tool_id": "ai_video",
            }
            contents = []
            if vids:
                contents.append({"type": "video_vid_list", "video_vid_list": vids,
                                 "name": "user_video"})
            if a_vids:
                contents.append({"type": "audio_vid_list", "audio_vid_list": a_vids,
                                 "name": "user_audio"})
        else:
            follow = {
                "inputPrompt": {"prompt": prompt},
                "generationMode": "first_last_frame",
                "modelId": model,
                "generate_id": str(uuid.uuid4()),
                "tool_id": "ai_video",
            }
            contents = []
        cfg = {
            "first_frame_image": first_frame_image,
            "end_frame_image": None,
            "lens_motion_type": "",
            "motion_speed": "",
            "ending_control": "",
            "frames": [],
            "frame_interval": 3000,
            "generate_video_node_type": 1,
            "prompt": prompt,
            "model": model,
            "video_aspect_ratio": ratio,
            "duration_ms": duration_ms,
            "resolution": resolution,
            # HAR 实测: config 内必须镜像图片参考, 否则服务端把音频当成唯一参考输入
            # 报 88100012 "reference_audio cannot be the only reference input"
            "images": [{"image_uri": r["uri"], "width": r.get("width", 0),
                        "height": r.get("height", 0), "format": r.get("format", "png"),
                        "name": r.get("name", "")}
                       for r in (references or []) if r.get("type") == "image"] or None,
        }
        body = {
            "bind_id": self.bind_id,
            "enter_from": "ai_material_generate",
            "request_extra": "",
            "common_payload": "{}",
            "can_queue": True,
            "tasks": [{
                "context": "",
                "payload": {
                    "submit_id": "",
                    "node_list": [1],
                    "node_config_key": "capcut_generate_video",
                    "template_id": "",
                    "template_name": "",
                    "cal_by_second": 1,
                    "func_key": "ai_video",
                    "pe_info": [],
                    "contents": contents,
                    "parameters": {
                        "generate_audio": generate_audio,
                        "follow_json": json.dumps(follow, ensure_ascii=False),
                        "sync_asset": True,
                    },
                    # HAR 观测: omni_reference 时 config 可为 null; 先带 config 便于控分辨率/时长,
                    # 若服务端拒绝由调用方回退
                    "generate_video_config_list": [cfg],
                },
                "req_key": "aigc_video_generate",
                "task_version": "v3",
            }],
        }
        url = self._url('/lv/v1/common_task/new')
        if dry_run:
            return {"__dry_run__": True, "url": url, "body": body}
        r = self.s.post(url, json=body,
                        headers=self._headers(), timeout=30)
        return r.json()

    def query_task(self, task_id, token):
        """轮询任务, 返回响应 json"""
        body = {"tasks": [{
            "id": task_id,
            "token": token,
            "req_key": "aigc_video_generate",
            "bind_id": self.bind_id,
            "algo_type": 0,
            "task_version": "v3",
        }]}
        r = self.s.post(self._url('/lv/v1/common_task/query'), json=body,
                        headers=self._headers("query"), timeout=30)
        return r.json()

    @staticmethod
    def extract_video(resp_json):
        """从 query 响应提取 (status, video_url, vid); 结果在 data.tasks[].payload(JSON字符串)"""
        try:
            tasks = (resp_json.get('data') or {}).get('tasks') or []
            t = tasks[0] if tasks else {}
            status = t.get('status')
            payload = t.get('payload')
            if isinstance(payload, str):
                payload = json.loads(payload)
            if isinstance(payload, dict):
                return status, payload.get('video_url'), payload.get('vid')
        except Exception:
            pass
        return None, None, None

    def generate(self, prompt, model='seedance_1.0_fast', resolution='480p',
                 duration_s=5, ratio='16:9', generate_audio=True,
                 poll_interval=2.5, max_poll=120, verbose=True):
        """一键生成: 创建任务 → 轮询 → 返回 (video_url, 全部响应)"""
        nr = self.create_video_task(prompt, model, resolution,
                                    int(duration_s * 1000), ratio, generate_audio)
        if verbose:
            print('[new]', json.dumps(nr, ensure_ascii=False)[:400])
        data = nr.get('data') or {}
        task = (data.get('tasks') or [{}])[0]
        task_id, token = task.get('id'), task.get('token')
        if not task_id or not token:
            # 兼容不同字段名
            raw = json.dumps(nr)
            mid = re.search(r'"id"\s*:\s*"([0-9a-z_]+)"', raw)
            mtok = re.search(r'"token"\s*:\s*"([0-9a-f]+)"', raw)
            task_id, token = mid.group(1) if mid else None, mtok.group(1) if mtok else None
        if not task_id or not token:
            raise RuntimeError(f'创建任务失败: {json.dumps(nr, ensure_ascii=False)[:600]}')
        if verbose:
            print(f'[task] id={task_id} token={token[:16]}...')

        last = None
        for i in range(max_poll):
            time.sleep(poll_interval)
            qr = self.query_task(task_id, token)
            status, video_url, vid = self.extract_video(qr)
            if verbose and (i % 4 == 0 or status != last or video_url):
                print(f'[poll {i}] status={status} vid={vid}', flush=True)
            last = status
            if video_url:
                return video_url, qr
            if status and str(status).lower() in ('failed', 'error', 'expired'):
                raise RuntimeError(f'任务失败: {json.dumps(qr, ensure_ascii=False)[:800]}')
        raise TimeoutError('轮询超时未出片')

    def download(self, url, out_path):
        r = self.s.get(url, stream=True, timeout=300)
        total = 0
        with open(out_path, 'wb') as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                total += len(chunk)
        return total


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--cookie-file', required=True)
    ap.add_argument('--prompt', required=True)
    ap.add_argument('--model', default='seedance_1.0_fast')
    ap.add_argument('--resolution', default='480p')
    ap.add_argument('--duration', type=float, default=5)
    ap.add_argument('--ratio', default='16:9')
    ap.add_argument('--no-audio', dest='audio', action='store_false')  # 默认生成配音
    ap.add_argument('--out', default='')
    a = ap.parse_args()
    cli = DirectTaskClient(a.cookie_file)
    url, _ = cli.generate(a.prompt, a.model, a.resolution, a.duration,
                          a.ratio, a.audio)
    out = a.out or f'capcut_direct_{int(time.time())}.mp4'
    n = cli.download(url, out)
    print(f'DONE {n/1e6:.2f}MB -> {out}')
