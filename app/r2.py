"""R2 转存：生成完成后把结果文件从 CF 源站转存到客户自己的 Cloudflare R2 桶。

对外只返回 R2 公开链接，不暴露 video-v2.creativefabrica.com 源站链接。
通过环境变量启用（全部就位才生效）：
  R2_ENDPOINT       如 https://<account>.r2.cloudflarestorage.com
  R2_ACCESS_KEY_ID  R2 API Token 的 Access Key ID
  R2_SECRET_ACCESS_KEY
  R2_BUCKET         桶名
  R2_PUBLIC_BASE    公开访问基址（自定义域名或 https://pub-xxx.r2.dev）
  R2_KEY_PREFIX     对象键前缀（默认 cf-gateway/）
"""
import asyncio
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

import httpx

from .config import get_config

log = logging.getLogger("r2")

MAX_FILE_SIZE = 300 * 1024 * 1024  # 300MB 上限（4k 长视频留余量

_EXT_BY_CT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "video/mp4": ".mp4", "video/webm": ".webm", "audio/mpeg": ".mp3", "audio/wav": ".wav",
    "application/pdf": ".pdf", "application/zip": ".zip",
}


def _ext_from(url: str, content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _EXT_BY_CT:
        return _EXT_BY_CT[ct]
    m = re.search(r"\.([a-zA-Z0-9]{2,5})(?:$|[?#])", url or "")
    return "." + m.group(1).lower() if m else ".bin"


class R2Store:
    _client = None

    @classmethod
    def enabled(cls) -> bool:
        cfg = get_config()
        return bool(cfg.R2_ENDPOINT and cfg.R2_ACCESS_KEY_ID and cfg.R2_SECRET_ACCESS_KEY
                    and cfg.R2_BUCKET and cfg.R2_PUBLIC_BASE)

    @classmethod
    def reference_enabled(cls) -> bool:
        """本地参考素材上传所需配置（不要求结果桶具备公开域名）。"""
        cfg = get_config()
        return bool(cfg.R2_ENDPOINT and cfg.R2_ACCESS_KEY_ID and cfg.R2_SECRET_ACCESS_KEY
                    and cfg.R2_BUCKET)

    @classmethod
    def _s3(cls):
        if cls._client is None:
            import boto3  # 延迟导入，未启用时无依赖开销
            cfg = get_config()
            cls._client = boto3.client(
                "s3",
                endpoint_url=cfg.R2_ENDPOINT,
                aws_access_key_id=cfg.R2_ACCESS_KEY_ID,
                aws_secret_access_key=cfg.R2_SECRET_ACCESS_KEY,
                region_name="auto",
                config=__import__("botocore.config", fromlist=["Config"]).Config(
                    retries={"max_attempts": 2},
                    connect_timeout=15, read_timeout=600,
                ),
            )
        return cls._client

    @classmethod
    async def transfer(cls, source_url: str, gen_id: str) -> str | None:
        """下载源站文件并上传到 R2。成功返回公开 URL；失败返回 None（调用方回退源站链接）。"""
        if not cls.enabled():
            return None
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(cls._transfer_sync, source_url, gen_id), timeout=600)
        except Exception as e:  # noqa: BLE001
            log.warning("R2 转存失败（回退源站链接）gen=%s: %s", gen_id, e)
            return None

    @classmethod
    async def upload_reference(cls, body: bytes, filename: str, content_type: str) -> tuple[str, str]:
        """上传调用方本地素材并返回 ``(object_key, 24h presigned GET URL)``。"""
        if not cls.reference_enabled():
            raise RuntimeError("R2 未配置完整（需要 R2_ENDPOINT、R2_ACCESS_KEY_ID、"
                               "R2_SECRET_ACCESS_KEY、R2_BUCKET）")
        return await asyncio.to_thread(cls._upload_reference_sync, body, filename, content_type)

    @classmethod
    def _upload_reference_sync(cls, body: bytes, filename: str,
                               content_type: str) -> tuple[str, str]:
        cfg = get_config()
        safe_name = re.sub(r"[^0-9A-Za-z._-]+", "_", filename or "upload.bin").strip("._")
        safe_name = safe_name[:100] or "upload.bin"
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        prefix = (cfg.R2_KEY_PREFIX or "cf-gateway/").strip("/")
        key = f"{prefix}/ref/{day}/{secrets.token_hex(8)}_{safe_name}"
        cls._s3().put_object(Bucket=cfg.R2_BUCKET, Key=key, Body=body,
                             ContentType=content_type or "application/octet-stream")
        url = cls._s3().generate_presigned_url(
            "get_object", Params={"Bucket": cfg.R2_BUCKET, "Key": key}, ExpiresIn=86400)
        return key, url

    @classmethod
    def cleanup_old_references(cls, max_age_hours: float | None = None) -> int:
        """删除 ``ref/`` 前缀下超过保留期的参考素材对象，返回删除个数。

        参考素材只是中转（网关下载后立即转传 CapCut imageX/VOD），用完即弃；
        不清的话会在桶里无限堆积。批量删，每批最多 1000 个。
        """
        if not cls.reference_enabled():
            return 0
        cfg = get_config()
        hours = float(max_age_hours if max_age_hours is not None
                      else getattr(cfg, "R2_REF_RETENTION_HOURS", 48))
        if hours <= 0:
            return 0  # 0 = 关闭自动清理
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        prefix = f"{(cfg.R2_KEY_PREFIX or 'cf-gateway/').strip('/')}/ref/"
        s3 = cls._s3()
        deleted = 0
        batch: list[dict] = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=cfg.R2_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["LastModified"] < cutoff:
                    batch.append({"Key": obj["Key"]})
            while len(batch) >= 1000:
                s3.delete_objects(Bucket=cfg.R2_BUCKET,
                                  Delete={"Objects": batch[:1000], "Quiet": True})
                deleted += 1000
                batch = batch[1000:]
        if batch:
            s3.delete_objects(Bucket=cfg.R2_BUCKET,
                              Delete={"Objects": batch, "Quiet": True})
            deleted += len(batch)
        return deleted

    @classmethod
    def _transfer_sync(cls, source_url: str, gen_id: str) -> str | None:
        cfg = get_config()
        with httpx.Client(timeout=httpx.Timeout(120, read=300), follow_redirects=True) as c:
            head = c.head(source_url)
            size = int(head.headers.get("content-length") or 0)
            if size > MAX_FILE_SIZE:
                raise RuntimeError(f"文件过大（{size}）")
            resp = c.get(source_url)
            resp.raise_for_status()
            data = resp.content
            content_type = resp.headers.get("content-type", "application/octet-stream")

        ext = _ext_from(source_url, content_type)
        date_prefix = datetime.now(timezone.utc).strftime("%Y%m%d")
        prefix = (cfg.R2_KEY_PREFIX or "cf-gateway/").strip("/")
        key = f"{prefix}/{date_prefix}/{gen_id}{ext}"
        cls._s3().put_object(Bucket=cfg.R2_BUCKET, Key=key, Body=data,
                             ContentType=content_type)
        base = cfg.R2_PUBLIC_BASE.rstrip("/")
        return f"{base}/{key}"

    @classmethod
    def scrub(cls, text_json: str, origin_url: str, public_url: str) -> str:
        """把存储的结果 JSON 里的源站链接替换为 R2 链接（不对外暴露源站）。"""
        if origin_url and public_url:
            return text_json.replace(origin_url, public_url)
        return text_json
