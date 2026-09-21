#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CapCut 素材「纯协议」上传（不依赖浏览器）
完整链路（全部由 Cookie 登录态驱动）：
  1) POST /lv/v1/upload_sign                 -> 拿 STS 临时凭证 (AK/SK/SessionToken, 有效期约30分钟)
  2) GET  imagex ...Action=ApplyImageUpload   -> 拿 StoreUri + Auth + SessionKey (AWS SigV4 签名)
  3) POST tos-my319-share.tiktokcdn.com/upload/v1/<StoreUri>  -> 直传文件字节 (Authorization=Auth)
  4) POST imagex ...Action=CommitImageUpload  -> 提交落库 (AWS SigV4 签名)
  5) POST /storyboard_agent/v1/ai_lab/batch_query_assets -> 登记素材, 拿官方展示 URL
  6) (可选) send_msg 用 <at> 标签把素材作为附件提交给 agent
用法:
  python3 capcut_upload_protocol.py --cookie-file cookies.json --file a.png [b.png ...] [--send] \
      [--prompt "用这些素材生成视频"] [--model seedance2_mini --resolution 480 --duration 5]
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import random
import string
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import requests

EDIT_API = "https://edit-api-sg.capcut.com"
IMAGEX = "https://imagex-normal-sg.capcutapi.com"
SERVICE_ID = "qf95cgtps6"
IMAGEX_REGION = "sg"
IMAGEX_SERVICE = "imagex"
VOD_HOST = "https://vod-normal-sg.capcutapi.com"
VOD_SPACE = "capcut_videocut_web_sg"
VOD_REGION = "ap-singapore-1"
VOD_SERVICE = "vod"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) HeadlessChrome/153.0.0.0 Safari/537.36")


def rand_s(n=10):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def load_cookie_dict(path):
    raw = json.load(open(path))
    if isinstance(raw, list):  # Netscape/curl 格式数组
        return {c["name"]: c["value"] for c in raw if "name" in c}
    if isinstance(raw, dict):
        if "cookies" in raw:
            return {c["name"]: c["value"] for c in raw["cookies"]}
        return raw
    raise ValueError("无法识别的 cookie 文件格式")


class PureUploader:
    def __init__(self, cookie_file):
        self.cookies = load_cookie_dict(cookie_file)
        self.web_id = (self.cookies.get("_v2_spipe_web_id") or self.cookies.get("webid")
                       or self.cookies.get("tt_web_id") or "")
        self.region = (self.cookies.get("store-country-code") or "SG").upper()
        self.common_params = {"aid": "348188", "device_platform": "web",
                              "region": self.region, "web_id": self.web_id}
        self.sess = requests.Session()
        self.sess.cookies.update(self.cookies)
        self.sess.headers.update({
            "User-Agent": UA,
            "Origin": "https://www.capcut.com",
            "Referer": "https://www.capcut.com/",
            "appid": "348188",
            "did": self.web_id,
            "lan": "en",
            "store-country-code": self.region.lower(),
        })
        self.sts = None  # {access_key_id, secret_access_key, session_token, expired_time}
        # upload_sign 的签名头（可被 DirectTaskClient 按账号/现签覆写）
        self.sign_headers = dict(self.SIGN_HEADERS)
        # 线程局部的 STS 覆盖（并发上传参考素材时，各线程各用自己的 STS，
        # 不会像早期那样直接改写 self.sts / self.get_sts 而互相覆盖）
        self._tls = threading.local()

    # ---------- 线程局部 STS 覆盖 ----------
    def use_sts(self, sts):
        """当前线程后续签名固定用这份 STS（覆盖优先，忽略 force/kind 参数）"""
        self._tls.sts = sts

    def clear_sts_override(self):
        self._tls.sts = None

    # ---------- edit-api 公共请求 ----------
    def _edit_headers(self):
        return {
            "Content-Type": "application/json",
            "device-time": str(int(time.time())),
            "pf": "7", "appvr": "8.4.0", "sign-ver": "1",
        }

    def edit_post(self, path, body):
        url = f"{EDIT_API}{path}?" + "&".join(f"{k}={v}" for k, v in self.common_params.items())
        r = self.sess.post(url, json=body, headers=self._edit_headers(), timeout=30)
        r.raise_for_status()
        return r.json()

    # ---------- 步骤 1: STS 凭证 ----------
    # upload_sign 的 sign = md5("9e2c|<path末7字符>|pf|appvr|device-time|tdid|11ac")，已从前端 bundle
    # 逆向出来（见 app/capcut_signs.py），所以既可现签也可沿用抓到的常量。
    # DirectTaskClient 会按账号/模式覆写 self.sign_headers；这里保留一对历史常量作兜底。
    SIGN_HEADERS = {
        "sign": "2057bffb49600bf28e31a20bd16fb202",
        "device-time": "1789313239",
        "tdid": "178931314028893759",
        "pf": "7", "appvr": "8.4.0", "sign-ver": "1",
    }

    def use_sign_headers(self, headers):
        """覆写 upload_sign 的签名头（按账号或现签）。"""
        self.sign_headers = dict(headers or {})

    def get_sts(self, force=False, kind="image"):
        """kind=image → biz=capcut_videocut_image_sg_v5; kind=vod → biz=capcut_videocut_web_sg"""
        ov = getattr(self._tls, "sts", None)
        if ov is not None:
            return ov
        if self.sts and kind in self.sts and not force:
            return self.sts[kind]
        if kind == "vod":
            body = {"key_version": "v5", "biz": "capcut_videocut_web_sg"}
        else:
            body = {"biz": "capcut_videocut_image_sg_v5"}
        h = dict(self._edit_headers())
        h.update(getattr(self, "sign_headers", None) or self.SIGN_HEADERS)
        url = f"{EDIT_API}/lv/v1/upload_sign?" + "&".join(
            f"{k}={v}" for k, v in self.common_params.items())
        r = self.sess.post(url, json=body, headers=h, timeout=30)
        resp = r.json()
        if resp.get("ret") != "0":
            raise RuntimeError(
                f"upload_sign 失败: {resp}\n"
                "→ sign/device-time 对已失效，需在浏览器重新抓一次 upload_sign 的 sign+device-time 更新脚本常量")
        d = resp["data"]
        sts = {
            "access_key_id": d["access_key_id"],
            "secret_access_key": d["secret_access_key"],
            "session_token": d["session_token"],  # STS2... 原样放 header
            "space_name": d.get("space_name", SERVICE_ID),
            "expired_time": d.get("expired_time"),
        }
        # image 与 vod 用不同 STS，分开缓存
        if self.sts is None:
            self.sts = {}
        self.sts[kind] = sts
        print(f"[STS:{kind}] ok  AK={sts['access_key_id'][:20]}...  expired={sts['expired_time']}")
        return sts

    # ---------- AWS SigV4 ----------
    @staticmethod
    def _hmac(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    def _aws4_headers(self, method, query_pairs, body=b"", kind="image"):
        sts = self.get_sts(kind=kind)
        ak, sk = sts["access_key_id"], sts["secret_access_key"]
        token = sts["session_token"]
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()

        if kind == "vod":
            region, service = VOD_REGION, VOD_SERVICE
        else:
            region, service = IMAGEX_REGION, IMAGEX_SERVICE

        # 签名头按字母序
        if method == "GET":
            signed_headers = "x-amz-date;x-amz-security-token"
            hdr_map = {"x-amz-date": amz_date, "x-amz-security-token": token}
        else:
            signed_headers = "x-amz-content-sha256;x-amz-date;x-amz-security-token"
            hdr_map = {"x-amz-content-sha256": payload_hash,
                       "x-amz-date": amz_date, "x-amz-security-token": token}

        # canonical query: 按 key 排序
        q = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}"
                     for k, v in sorted(query_pairs.items()))
        canonical_headers = "".join(f"{k}:{hdr_map[k]}\n" for k in signed_headers.split(";"))
        canonical_request = "\n".join([method, "/", q, canonical_headers, signed_headers, payload_hash])
        scope = f"{datestamp}/{region}/{service}/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ])
        k_date = self._hmac(("AWS4" + sk).encode(), datestamp)
        k_region = self._hmac(k_date, region)
        k_service = self._hmac(k_region, service)
        k_signing = self._hmac(k_service, "aws4_request")
        signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
        auth = (f"AWS4-HMAC-SHA256 Credential={ak}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}")
        headers = {k: hdr_map[k] for k in hdr_map}
        headers["Authorization"] = auth
        headers["User-Agent"] = UA
        headers["Referer"] = "https://www.capcut.com/"
        return headers

    # ---------- 步骤 2-4: imageX 上传 ----------
    def upload_image(self, filepath):
        data = open(filepath, "rb").read()
        size = len(data)
        ext = os.path.splitext(filepath)[1].lstrip(".").lower() or "png"

        # 2) Apply
        q = {"Action": "ApplyImageUpload", "Version": "2018-08-01",
             "ServiceId": SERVICE_ID, "FileSize": size,
             "s": rand_s(), "device_platform": "web"}
        url = IMAGEX + "/?" + "&".join(f"{k}={v}" for k, v in q.items())
        hdrs = self._aws4_headers("GET", q)
        r = self.sess.get(url, headers=hdrs, timeout=30)
        r.raise_for_status()
        apply_resp = r.json()
        try:
            result = apply_resp["Result"]
            store = result["UploadAddress"]["StoreInfos"][0]
            store_uri, auth = store["StoreUri"], store["Auth"]
            session_key = (result.get("SessionKey")
                           or result["UploadAddress"].get("SessionKey")
                           or "")
        except Exception:
            raise RuntimeError(f"ApplyImageUpload 响应异常: {json.dumps(apply_resp)[:400]}")
        print(f"[Apply] StoreUri={store_uri}")

        # 3) 直传文件字节到 tos（需带 CRC32 校验头）
        import zlib
        crc = format(zlib.crc32(data) & 0xFFFFFFFF, "08x")
        up_url = f"https://tos-my319-share.tiktokcdn.com/upload/v1/{store_uri}"
        up_hdrs = {"Authorization": auth, "User-Agent": UA,
                   "Content-Type": "application/octet-stream",
                   "Content-Crc32": crc}
        r = self.sess.post(up_url, data=data, headers=up_hdrs, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"TOS 直传失败 {r.status_code}: {r.text[:200]}")
        print(f"[Upload] {size} bytes ok, resp={r.text[:120]}")

        # 4) Commit
        q2 = {"Action": "CommitImageUpload", "Version": "2018-08-01", "ServiceId": SERVICE_ID}
        body = json.dumps({"SessionKey": session_key}).encode()
        url2 = IMAGEX + "/?" + "&".join(f"{k}={v}" for k, v in q2.items())
        hdrs2 = self._aws4_headers("POST", q2, body)
        hdrs2["Content-Type"] = "application/json"
        r = self.sess.post(url2, data=body, headers=hdrs2, timeout=30)
        r.raise_for_status()
        commit = r.json()
        uri = commit["Result"]["Results"][0]["Uri"]
        status = commit["Result"]["Results"][0].get("UriStatus")
        if status != 2000:
            raise RuntimeError(f"Commit 失败 UriStatus={status}")
        print(f"[Commit] ok  uri={uri}")
        return uri


    # ---------- 步骤 2-4b: VOD 上传（视频/音频） ----------
    def upload_vod(self, filepath, file_type="video"):
        """file_type: 'video' 或 'audio'。返回 (Vid, StoreUri, duration)
        注意：VOD 侧不认 FileType=audio，音频需用 'media'"""
        import zlib
        data = open(filepath, "rb").read()
        size = len(data)
        apply_file_type = "media" if file_type == "audio" else file_type

        # 1) Apply (GET, SigV4, scope ap-singapore-1/vod)
        q = {"Action": "ApplyUploadInner", "Version": "2020-11-19",
             "SpaceName": VOD_SPACE, "FileType": apply_file_type, "IsInner": "1",
             "FileSize": size, "s": rand_s(), "device_platform": "web"}
        url = VOD_HOST + "/?" + "&".join(f"{k}={v}" for k, v in q.items())
        hdrs = self._aws4_headers("GET", q, kind="vod")
        r = self.sess.get(url, headers=hdrs, timeout=30)
        r.raise_for_status()
        apply_resp = r.json()
        try:
            result = apply_resp["Result"]
            addr = result.get("UploadAddress")
            if not addr:  # IsInner=1 时走 InnerUploadAddress.UploadNodes[0]
                node = result["InnerUploadAddress"]["UploadNodes"][0]
                store = node["StoreInfos"][0]
                store_uri, auth = store["StoreUri"], store["Auth"]
                session_key = node.get("SessionKey") or ""
                upload_host = node.get("UploadHost", "tos-my16-share.vodupload.com")
            else:
                store = addr["StoreInfos"][0]
                store_uri, auth = store["StoreUri"], store["Auth"]
                session_key = addr.get("SessionKey") or result.get("SessionKey") or ""
                upload_host = addr.get("UploadHosts", ["tos-my16-share.vodupload.com"])[0]
        except Exception:
            raise RuntimeError(f"ApplyUploadInner 响应异常: {json.dumps(apply_resp)[:500]}")
        print(f"[Apply:{file_type}] StoreUri={store_uri}  host={upload_host}")

        # 2) 直传文件字节（CRC32 头必须）
        crc = format(zlib.crc32(data) & 0xFFFFFFFF, "08x")
        up_url = f"https://{upload_host}/upload/v1/{store_uri}"
        up_hdrs = {"Authorization": auth, "User-Agent": UA,
                   "Content-Type": "application/octet-stream",
                   "Content-Crc32": crc}
        r = self.sess.post(up_url, data=data, headers=up_hdrs, timeout=300)
        if r.status_code != 200:
            raise RuntimeError(f"VOD 直传失败 {r.status_code}: {r.text[:200]}")
        print(f"[Upload:{file_type}] {size} bytes ok, resp={r.text[:120]}")

        # 3) Commit (POST, SigV4 含 body hash)
        q2 = {"Action": "CommitUploadInner", "Version": "2020-11-19", "SpaceName": VOD_SPACE}
        body = json.dumps({"SessionKey": session_key}).encode()
        url2 = VOD_HOST + "/?" + "&".join(f"{k}={v}" for k, v in q2.items())
        hdrs2 = self._aws4_headers("POST", q2, body, kind="vod")
        hdrs2["Content-Type"] = "application/json"
        r = self.sess.post(url2, data=body, headers=hdrs2, timeout=60)
        r.raise_for_status()
        commit = r.json()
        res0 = commit["Result"]["Results"][0]
        vid = res0.get("Vid")
        if not vid:
            raise RuntimeError(f"Commit 失败: {json.dumps(commit)[:400]}")
        meta = res0.get("VideoMeta", {})
        print(f"[Commit:{file_type}] ok  Vid={vid}  {meta.get('Format','')} {meta.get('Duration','')}s")
        return vid, store_uri, (meta.get("Duration") or 0)

    # ---------- 步骤 5: 登记 ----------
    def batch_query_assets(self, uri, fmt="image", rtype="uri"):
        resp = self.edit_post("/storyboard_agent/v1/ai_lab/batch_query_assets", {
            "items": [{"resource": uri, "format": fmt,
                       "resource_type": rtype, "image_quality": 85}]
        })
        if resp.get("ret") != "0":
            raise RuntimeError(f"batch_query_assets 失败: {resp}")
        item = resp["data"]["items"][0]
        return item

    @staticmethod
    def build_at_tag_vod(vid, store_uri, name, material_type, duration):
        extra = {"from": "web", "scene": "chat", "reference_tag_type": "media",
                 "payload": {"uri": store_uri, "vid": vid,
                             "duration": duration, "material_type": material_type,
                             "source": "vod"}}
        extra_attr = json.dumps(extra, separators=(",", ":")).replace('"', "&quot;")
        return f'<at type="{material_type}" value="{vid}" name="{name}" extra="{extra_attr}" />'

    # ---------- 步骤 6: 构造 <at> 标签 ----------
    @staticmethod
    def build_at_tag(item, name="material.png", material_type="image"):
        uri = item["resource"]
        extra = {"from": "web", "scene": "chat", "reference_tag_type": "media",
                 "payload": {"uri": uri, "material_type": material_type, "source": "imageX"}}
        extra_json = json.dumps(extra, separators=(",", ":"))
        extra_attr = extra_json.replace("&", "&quot;") if False else extra_json.replace('"', "&quot;")
        return f'<at type="{material_type}" value="{uri}" name="{name}" extra="{extra_attr}" />'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookie-file", required=True)
    ap.add_argument("--file", nargs="+", required=True, help="要上传的图片文件")
    ap.add_argument("--send", action="store_true", help="上传后直接 send_msg 提交给 agent")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--model", default="seedance2_mini")
    ap.add_argument("--resolution", default="480")
    ap.add_argument("--duration", default="5")
    args = ap.parse_args()

    VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}
    AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
    IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

    up = PureUploader(args.cookie_file)
    tags = []
    for f in args.file:
        ext = os.path.splitext(f)[1].lower()
        name = os.path.basename(f)
        if ext in IMG_EXT:
            uri = up.upload_image(f)
            item = up.batch_query_assets(uri)
            print(f"[Asset] {name} -> {item.get('url','')[:100]}")
            tags.append(up.build_at_tag(item, name=name))
        else:
            ftype = "video" if ext in VIDEO_EXT else "audio" if ext in AUDIO_EXT else None
            if not ftype:
                print(f"跳过不认识的文件类型: {f}")
                continue
            vid, store_uri, duration = up.upload_vod(f, file_type=ftype)
            item = up.batch_query_assets(vid, fmt=ftype, rtype="vid")
            print(f"[Asset] {name} -> {item.get('url','')[:100]}")
            tags.append(up.build_at_tag_vod(vid, store_uri, name, ftype, duration))

    at_text = " ".join(tags)
    print("\n<at> 标签:\n" + at_text[:500])

    if args.send:
        from capcut_video_api import CapCutClient  # 复用现有 send_msg/SSE 逻辑
        cli = CapCutClient(up.cookies, up.web_id, up.region, "")  # 不带旧账号 project_id，否则其他账号素材同步失败
        print(f"\n[Send] 提交给 {args.model} {args.resolution}p {args.duration}s ...")
        r = cli.send_msg(args.prompt or "用这些素材生成视频",
                         args.model, args.resolution, args.duration, at_tags=at_text)
        print(json.dumps(r, ensure_ascii=False)[:400])


if __name__ == "__main__":
    main()
