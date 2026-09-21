"""OpenAI / New API 兼容对外接口（sk-cf- 密钥鉴权）。"""
import asyncio
import json
import logging
import os
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import db_session
from ..models import ApiKey, Generation, ModelEntry, JSONText, log_event, new_gen_id, now
from .. import security
from ..r2 import R2Store

router = APIRouter(prefix="/v1")

log = logging.getLogger("cf_gateway.openai")

_UPLOAD_KINDS = {
    ".jpg": ("image", "image/jpeg", 20 * 1024 * 1024),
    ".jpeg": ("image", "image/jpeg", 20 * 1024 * 1024),
    ".png": ("image", "image/png", 20 * 1024 * 1024),
    ".webp": ("image", "image/webp", 20 * 1024 * 1024),
    ".mp4": ("video", "video/mp4", 50 * 1024 * 1024),
    ".mov": ("video", "video/quicktime", 50 * 1024 * 1024),
    ".mp3": ("audio", "audio/mpeg", 15 * 1024 * 1024),
    ".wav": ("audio", "audio/wav", 15 * 1024 * 1024),
}
# 单次上传的素材数量上限：按最大模型能力给（Seedance 2.5 参考模式 30 图 / 10 视频 / 10 音频）。
# 真正生效的上限仍由**具体模型**在提交时校验（service.ref_limits_of），这里只是批量上传的闸门。
_UPLOAD_TYPE_LIMITS = {"image": 30, "video": 10, "audio": 10}
_MAX_UPLOAD_FILES = sum(_UPLOAD_TYPE_LIMITS.values())

# 文档 §23：错误类型映射
ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "insufficient_balance",
    403: "permission_denied",
    404: "not_found_error",
    408: "timeout",
    429: "rate_limit_error",
    500: "upstream_error",
    503: "service_unavailable",
}


def openai_error(status: int, message: str, err_type: str = "", code: str = ""):
    return JSONResponse(status_code=status, content={
        "error": {
            "message": message,
            "type": err_type or ERROR_TYPE_BY_STATUS.get(status, "api_error"),
            "code": code or None,
        }
    })


def _get_key(db: Session, authorization: str):
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if not token:
        return None, openai_error(401, "缺少 API Key。请使用: Authorization: Bearer sk-cf-xxxx")
    key = db.execute(select(ApiKey).where(ApiKey.key_hash == security.hash_api_key(token))).scalar_one_or_none()
    if not key:
        return None, openai_error(401, "API Key 无效")
    if not key.enabled:
        return None, openai_error(403, "API Key 已被禁用", "permission_denied")
    return key, None


def _check_quota(db: Session, key: ApiKey):
    if key.daily_limit and key.daily_limit > 0:
        day_start = time.time() - (time.time() % 86400)
        used = db.execute(select(func.count(Generation.id)).where(
            Generation.api_key_id == key.id, Generation.created_at >= day_start)).scalar() or 0
        if used >= key.daily_limit:
            return openai_error(429, f"已达今日请求上限（{key.daily_limit} 次/天）", "rate_limit_error")
    return None


@router.post("/files")
async def upload_files(request: Request, authorization: str = Header(default=""),
                       db: Session = Depends(db_session)):
    """本地参考素材 → R2；返回供 ``/videos/generations`` 使用的 24 小时签名 URL。"""
    key, err = _get_key(db, authorization)
    if err:
        return err
    if not R2Store.reference_enabled():
        return openai_error(503, "服务器未配置 R2，无法上传参考素材", "service_unavailable")
    try:
        form = await request.form()
    except Exception:
        return openai_error(400, "请求必须为 multipart/form-data；服务端还需安装 python-multipart")
    uploads = form.getlist("file")
    if not uploads:
        return openai_error(400, "至少上传一个 file 字段")
    if len(uploads) > _MAX_UPLOAD_FILES:
        return openai_error(400, f"一次最多上传 {_MAX_UPLOAD_FILES} 个素材（图 30 / 视频 10 / 音频 10，"
                                 f"当前 {len(uploads)} 个）")

    counts = {kind: 0 for kind in _UPLOAD_TYPE_LIMITS}
    result = []
    for upload in uploads:
        filename = getattr(upload, "filename", "") or ""
        ext = os.path.splitext(filename)[1].lower()
        spec = _UPLOAD_KINDS.get(ext)
        if not spec:
            return openai_error(400, f"不支持的参考素材类型: {filename or '未命名文件'}")
        kind, content_type, max_size = spec
        counts[kind] += 1
        if counts[kind] > _UPLOAD_TYPE_LIMITS[kind]:
            return openai_error(400, f"一次上传的{kind}最多 {_UPLOAD_TYPE_LIMITS[kind]} 个")
        try:
            body = await upload.read(max_size + 1)
        except Exception:
            return openai_error(400, f"无法读取上传文件: {filename}")
        if not body:
            return openai_error(400, f"上传文件为空: {filename}")
        if len(body) > max_size:
            return openai_error(400, f"文件过大: {filename}（{kind} 上限 {max_size // 1024 // 1024}MB）")
        try:
            object_key, url = await R2Store.upload_reference(body, filename, content_type)
        except Exception as e:  # noqa: BLE001
            return openai_error(503, f"R2 上传失败: {str(e)[:240]}", "service_unavailable")
        result.append({
            "id": object_key,
            "object": "file",
            "type": kind,
            "filename": filename,
            "bytes": len(body),
            "url": url,
            "expires_in": 86400,
        })
    return {"object": "list", "data": result}


# sd-seedance-* 对外别名：New API 渠道按「模型名=分辨率」暴露（如 sd-seedance-2.0-720p），
# 网关解析成基础模型 + 强制分辨率（覆盖请求里的 size/resolution，别名即产品承诺）。
_SD_ALIAS_RE = re.compile(r"^sd-seedance[-_](2\.0|2\.5)[-_](480|720)p$", re.IGNORECASE)
_SD_ALIAS_BASE = {"2.0": "capcut-seedance-2.0", "2.5": "capcut-seedance_2.5"}


def resolve_model_alias(model_id: str) -> Optional[tuple]:
    """返回 (基础模型id, 强制分辨率如 '720p')；非别名返回 None。"""
    m = _SD_ALIAS_RE.match(str(model_id or "").strip())
    if not m:
        return None
    return _SD_ALIAS_BASE[m.group(1)], f"{m.group(2)}p"


def _check_model(db: Session, key: ApiKey, model_id: str, alias_name: str = ""):
    model = db.execute(select(ModelEntry).where(ModelEntry.model_id == model_id)).scalar_one_or_none()
    if not model or not model.enabled:
        return None, openai_error(404, f"模型不存在或未启用: {alias_name or model_id}", "invalid_request_error", "model_not_found")
    allowed = key.enabled_models or []
    if allowed:
        names = {model_id, alias_name} - {""}
        if not (names & set(allowed)):
            return None, openai_error(403, f"该 API Key 无权使用模型: {alias_name or model_id}", "permission_denied")
    if alias_name:
        log.info("模型别名 %s -> %s（强制分辨率）", alias_name, model_id)
    return model, None


def _normalize_seconds(params: dict) -> dict:
    """OpenAI Sora 用 ``seconds`` 表示时长，内部统一成 ``duration``。"""
    if "seconds" in params and params["seconds"] is not None:
        if params.get("duration") is None and params.get("duration_seconds") is None:
            params["duration"] = params["seconds"]
    return params


def _note_capcut_clamp(model: ModelEntry, params: dict) -> None:
    """CapCut 渠道：分辨率/时长超出该模型上限时留痕（夹取仍在 build_capcut_args）。

    分辨率/时长是**按模型放开**的（service.gen_limits_of）：默认 480/720p、2-15s，
    Seedance 2.5 放开到 1080p 与 30s。超出即静默夹取会让调用方以为生效，故记日志。
    """
    if (model.provider or "") != "capcut":
        return
    try:
        from ..capcut_channel import clamp_duration, split_size
        from ..service import gen_limits_of
        lim = gen_limits_of(model)
        secs = params.get("duration")
        if secs is None:
            secs = params.get("duration_seconds")
        if secs is not None:
            eff = clamp_duration(secs, lim["durations"], lim["min_duration"], lim["max_duration"])
            if abs(eff - float(secs)) > 1e-6:
                log.warning("模型 %s 时长上限 %.3gs，请求 %ss -> 生效 %.3gs",
                            model.model_id, lim["max_duration"], secs, eff)
        size = params.get("size") or params.get("resolution")
        if size:
            res, _ = split_size(size, lim["resolutions"])
            wanted = "".join(c for c in str(size) if c.isdigit())
            if res and wanted and str(res) not in wanted:
                log.warning("模型 %s 分辨率档 %s，请求 %s -> 生效 %sp",
                            model.model_id, lim["resolutions"], size, res)
    except Exception as e:  # noqa: BLE001 —— 仅日志，绝不影响主流程
        log.debug("生成上限留痕失败: %s", e)


async def _create_generation(db: Session, request: Request, key: ApiKey, model: ModelEntry,
                             prompt: str, params: dict):
    if not prompt or not str(prompt).strip():
        return openai_error(400, "prompt 不能为空")
    params = _normalize_seconds(dict(params))
    # 文档 §22：基础参数校验（交由 schema 转换进一步校验必填项）
    if "duration" in params and params["duration"] is not None:
        try:
            d = float(params["duration"])
            if not (0 < d <= 600):
                return openai_error(400, "Invalid duration（允许范围 0-600 秒）")
        except (TypeError, ValueError):
            return openai_error(400, "Invalid duration")
    if "duration_seconds" in params and params["duration_seconds"] is not None:
        try:
            d = float(params["duration_seconds"])
            if not (0 < d <= 600):
                return openai_error(400, "Invalid duration_seconds（允许范围 0-600 秒）")
        except (TypeError, ValueError):
            return openai_error(400, "Invalid duration_seconds")
    if "size" in params and params["size"] is not None and not isinstance(params["size"], str):
        return openai_error(400, "Invalid size（应为字符串，如 1280x720）")
    try:
        # 在排队前给调用方返回明确的 400，而不是让后台 worker 失败。
        from ..service import build_reference_files, ref_limits_of
        build_reference_files(str(prompt or ""), params, limits=ref_limits_of(model))
    except ValueError as e:
        return openai_error(400, str(e))
    _note_capcut_clamp(model, params)

    gen = Generation(
        gen_id=new_gen_id(), api_key_id=key.id, key_name=key.name or key.key_prefix,
        model_id=model.model_id, mcp_tool=model.mcp_tool,
        prompt=str(prompt)[:8000],
        params_text=JSONText.dump({k: v for k, v in params.items()
                                   if k not in ("model", "prompt", "stream", "user", "n")}),
        status=Generation.STATUS_QUEUED,
    )
    db.add(gen)
    key.last_used_at = now()
    db.commit()
    log_event(db, "info", "generation_created",
              {"gen_id": gen.gen_id, "model": model.model_id, "key": key.key_prefix},
              request_id=gen.gen_id, key_prefix=key.key_prefix)
    return gen


def _gen_response(gen: Generation, detailed: bool = True):
    # polling 是内部状态（已提交 CF、等待生成），对外统一呈现为 processing
    status = "processing" if gen.status == "polling" else gen.status
    data = {
        "id": gen.gen_id,
        "task_id": gen.gen_id,
        "object": "generation",
        "status": status,
        "model": gen.model_id,
        "created": int(gen.created_at),
    }
    if detailed:
        data.update({
            "url": gen.result_url or None,
            "error": gen.error or None,
            "cf_status": gen.cf_status or None,
            "link_mode": (gen.link_mode or None) if (gen.link_mode or gen.model_id.startswith("capcut-")) else None,
            "created_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(gen.created_at)),
        })
        if gen.finished_at:
            data["finished"] = int(gen.finished_at)
            data["duration_ms"] = gen.duration_ms
    return data


async def _find_generation(db: Session, key: ApiKey, gen_id: str):
    g = db.execute(select(Generation).where(Generation.gen_id == gen_id)).scalar_one_or_none()
    if not g or (g.api_key_id != key.id):
        return None, openai_error(404, "任务不存在", "invalid_request_error", "task_not_found")
    return g, None


# ---------------- OpenAI Sora / New API「Sora」渠道兼容 ----------------
# New API 的 Sora 渠道按 OpenAI 官方约定调用：
#   POST /v1/videos                 创建任务
#   GET  /v1/videos/{id}            查询状态
#   GET  /v1/videos/{id}/content    取视频（302 跳到成片直链）
# 状态词表用 OpenAI 的 queued / in_progress / completed / failed
# （内部 processing、polling 都归并为 in_progress）。

_SORA_STATUS = {
    "queued": "queued",
    "processing": "in_progress",
    "polling": "in_progress",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _video_response(gen: Generation, detailed: bool = True):
    params = JSONText.load(gen.params_text) or {}
    secs = params.get("duration") or params.get("duration_seconds") or params.get("seconds")
    size = params.get("size")
    if not size and params.get("resolution") and params.get("aspect_ratio"):
        size = f"{params['resolution']} {params['aspect_ratio']}"
    else:
        size = size or params.get("resolution")
    try:
        seconds = str(int(float(secs))) if secs else None
    except (TypeError, ValueError):
        seconds = None
    data = {
        "id": gen.gen_id,
        "object": "video",
        "model": gen.model_id,
        "status": _SORA_STATUS.get(gen.status, gen.status),
        "created_at": int(gen.created_at),
        "seconds": seconds,
        "size": size,
        "progress": None,          # 上游不暴露进度，由调用方按需自行估算
        "error": {"message": gen.error} if gen.error else None,
    }
    if detailed:
        data["url"] = gen.result_url or None
        data["completed_at"] = int(gen.finished_at) if gen.finished_at else None
        result = JSONText.load(gen.result_text) or {}
        if gen.link_mode:
            data["link_mode"] = result.get("linkMode") or gen.link_mode
        if result.get("originUrl"):
            data["origin_url"] = result["originUrl"]
    return data


@router.post("/videos")
async def sora_create_video(request: Request, authorization: str = Header(default=""),
                            db: Session = Depends(db_session)):
    """OpenAI Sora 风格创建任务（New API「Sora」渠道用这个）。"""
    return await _generations_endpoint(request, "video", authorization, db, fmt="sora")


async def _sora_query(video_id: str, authorization: str, db: Session):
    key, err = _get_key(db, authorization)
    if err:
        return err
    g, err = await _find_generation(db, key, video_id)
    if err:
        return err
    return _video_response(g, detailed=True)


@router.get("/videos/{video_id}/content")
async def sora_video_content(video_id: str, authorization: str = Header(default=""),
                             db: Session = Depends(db_session)):
    """302 跳到成片直链（官链=CapCut CDN；官转/CF 通道=R2 链接）。"""
    key, err = _get_key(db, authorization)
    if err:
        return err
    g, err = await _find_generation(db, key, video_id)
    if err:
        return err
    if not g.result_url:
        if g.status == Generation.STATUS_FAILED:
            return openai_error(500, f"生成失败: {g.error}", "upstream_error")
        return openai_error(409, f"视频尚未生成完成（当前状态 {g.status}）", "invalid_request_error")
    return RedirectResponse(url=g.result_url, status_code=302)


@router.get("/videos/{video_id}")
async def sora_query_video(video_id: str, authorization: str = Header(default=""),
                           db: Session = Depends(db_session)):
    return await _sora_query(video_id, authorization, db)


@router.post("/video/generations")
async def sora_video_generations_alias(request: Request, authorization: str = Header(default=""),
                                      db: Session = Depends(db_session)):
    """部分聚合器用单数 /v1/video/generations，一并兼容。"""
    return await _generations_endpoint(request, "video", authorization, db, fmt="sora")


@router.get("/video/generations/{video_id}")
async def sora_video_query_alias(video_id: str, authorization: str = Header(default=""),
                                 db: Session = Depends(db_session)):
    return await _sora_query(video_id, authorization, db)


@router.get("/models")
def list_models(authorization: str = Header(default=""), db: Session = Depends(db_session)):
    key, err = _get_key(db, authorization)
    if err:
        return err
    rows = db.execute(select(ModelEntry).where(ModelEntry.enabled == True)
                      .order_by(ModelEntry.model_id)).scalars().all()
    allowed = key.enabled_models or []
    data = []
    for m in rows:
        if allowed and m.model_id not in allowed:
            continue
        item = {"id": m.model_id, "object": "model", "owned_by": m.provider,
                "created": int(m.created_at)}
        # CapCut 渠道额外回带该模型实际生效的生成上限（分辨率/时长/参考素材数量）
        if (m.provider or "") == "capcut":
            try:
                from ..service import gen_limits_of, ref_limits_of
                gl, rl = gen_limits_of(m), ref_limits_of(m)
                item["capcut_limits"] = {
                    "resolutions": [f"{r}p" for r in gl["resolutions"]],
                    "durations": gl["durations"] or [gl["min_duration"], gl["max_duration"]],
                    "min_duration": gl["min_duration"],
                    "max_duration": gl["max_duration"],
                    "max_reference": {k: rl[k] for k in ("image", "video", "audio", "total")},
                }
            except Exception:  # noqa: BLE001 —— 附带信息，失败不影响模型列表
                pass
        data.append(item)
    # sd-seedance-* 别名也回带（客户端按分辨率选模型时直接可见）
    for m in rows:
        if (m.provider or "") != "capcut":
            continue
        for ver, base in _SD_ALIAS_BASE.items():
            if base != m.model_id:
                continue
            for res in (gen_resolutions(m)):
                alias_id = f"sd-seedance-{ver}-{res}"
                if allowed and alias_id not in allowed and m.model_id not in allowed:
                    continue
                data.append({"id": alias_id, "object": "model", "owned_by": m.provider,
                             "created": int(m.created_at), "alias_of": m.model_id})
    return {"object": "list", "data": data}


def gen_resolutions(m: ModelEntry):
    """别名列表用的分辨率档（480/720 整数），失败时给默认。"""
    try:
        from ..service import gen_limits_of
        return [f"{r}p" for r in gen_limits_of(m)["resolutions"] if r in (480, 720)]
    except Exception:  # noqa: BLE001
        return ["480p", "720p"]


async def _generations_endpoint(request: Request, expect_type: str,
                                authorization: str, db: Session, fmt: str = "legacy"):
    key, err = _get_key(db, authorization)
    if err:
        return err
    err = _check_quota(db, key)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return openai_error(400, "请求体必须为 JSON")
    model_id = str(body.get("model") or "").strip()
    if not model_id:
        return openai_error(400, "缺少 model 参数")
    alias = resolve_model_alias(model_id)
    model, err = _check_model(db, key, alias[0] if alias else model_id,
                              alias_name=model_id if alias else "")
    if err:
        return err
    if expect_type in ("image", "video") and model.mtype not in (expect_type, "other", "audio"):
        return openai_error(400, f"模型 {model_id} 类型为 {model.mtype}，请调用对应接口或 /v1/generations")
    prompt = body.get("prompt") or (body.get("input") if isinstance(body.get("input"), str) else "")
    params = {k: v for k, v in body.items()
              if k in ("modality", "options", "duration", "duration_seconds", "seconds", "size",
                       "resolution", "aspect_ratio", "negative_prompt", "seed", "n", "user",
                       "image", "video", "audio", "reference_files", "keep_prompt",
                       "generate_audio")}
    if alias:
        params["size"] = alias[1]
    result = await _create_generation(db, request, key, model, prompt, params)
    if isinstance(result, JSONResponse):
        return result
    body_fn = _video_response if fmt == "sora" else _gen_response
    return JSONResponse(status_code=200, content=body_fn(result, detailed=False))


@router.post("/images/generations")
async def images_generations(request: Request, authorization: str = Header(default=""),
                             db: Session = Depends(db_session)):
    return await _generations_endpoint(request, "image", authorization, db)


@router.post("/videos/generations")
async def videos_generations(request: Request, authorization: str = Header(default=""),
                             db: Session = Depends(db_session)):
    return await _generations_endpoint(request, "video", authorization, db)


@router.post("/generations")
async def generations_generic(request: Request, authorization: str = Header(default=""),
                              db: Session = Depends(db_session)):
    return await _generations_endpoint(request, "other", authorization, db)


async def _query_generation(gen_id: str, authorization: str, db: Session, key_: Session = None):
    key, err = _get_key(db, authorization)
    if err:
        return err
    g, err = await _find_generation(db, key, gen_id)
    if err:
        return err
    data = _gen_response(g, detailed=True)
    data["result"] = JSONText.load(g.result_text)
    return data


@router.get("/generations/{gen_id}")
async def query_generation(gen_id: str, authorization: str = Header(default=""),
                           db: Session = Depends(db_session)):
    return await _query_generation(gen_id, authorization, db)


@router.get("/videos/generations/{gen_id}")
async def query_video(gen_id: str, authorization: str = Header(default=""),
                      db: Session = Depends(db_session)):
    return await _query_generation(gen_id, authorization, db)


@router.get("/images/generations/{gen_id}")
async def query_image(gen_id: str, authorization: str = Header(default=""),
                      db: Session = Depends(db_session)):
    return await _query_generation(gen_id, authorization, db)


@router.delete("/generations/{gen_id}")
async def cancel_generation(gen_id: str, authorization: str = Header(default=""),
                            db: Session = Depends(db_session)):
    key, err = _get_key(db, authorization)
    if err:
        return err
    g, err = await _find_generation(db, key, gen_id)
    if err:
        return err
    if g.status != Generation.STATUS_QUEUED:
        return openai_error(400, "仅排队中(queued)的任务可取消")
    g.status = Generation.STATUS_CANCELLED
    g.finished_at = now()
    db.commit()
    return _gen_response(g, detailed=True)


# ---------------- 经 chat 入口轮询任务 ----------------
# New API 只转发 OpenAI 标准路由，不转发 GET /v1/generations/{id}；
# 客户端（尤其经 New API 接入的）改用 chat/completions 魔法指令轮询：
#   messages 最后一条 user content 为 "@query <task_id>"（兼容 @poll / @查询 / @查单）
#   或顶层传 task_query 字段（直连网关时可用）。
_POLL_RE = re.compile(
    r"^\s*@(?:query|poll|查询|查单)\s+([A-Za-z0-9_\-]+)\s*$", re.IGNORECASE)


def _poll_target(body: dict) -> str:
    tq = body.get("task_query")
    if isinstance(tq, str) and tq.strip():
        return tq.strip()
    for m in reversed(body.get("messages") or []):
        if isinstance(m, dict) and m.get("role") == "user" and m.get("content"):
            c = m["content"]
            text = c if isinstance(c, str) else " ".join(
                p.get("text", "") for p in c if isinstance(p, dict))
            mt = _POLL_RE.match(text or "")
            return mt.group(1) if mt else ""
    return ""


def _ext_status(gen: Generation) -> str:
    """对外状态词表：queued/polling 统一呈现 processing。"""
    return "processing" if gen.status in ("queued", "polling") else gen.status


def _poll_chat_payload(gen: Generation) -> dict:
    status = _ext_status(gen)
    if gen.status == Generation.STATUS_COMPLETED:
        from ..mcp_client import extract_text
        raw = JSONText.load(gen.result_text) or {}
        text = extract_text(raw).strip()
        if gen.result_url:
            text = (text + "\n" + gen.result_url).strip()
        text = text or gen.result_url or "(完成)"
    elif gen.status == Generation.STATUS_FAILED:
        text = f"生成失败: {gen.error}"
    elif gen.status == Generation.STATUS_CANCELLED:
        text = "任务已取消"
    else:
        text = (f"任务处理中。task_id: {gen.gen_id}，状态: {status}。"
                f"继续发送 @query {gen.gen_id} 轮询。")
    return {
        "id": "chatcmpl-" + gen.gen_id,
        "object": "chat.completion",
        "created": int(gen.created_at),
        "model": gen.model_id,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "task_id": gen.gen_id,
        "task_status": status,
        "url": gen.result_url or None,
    }


# ---------------- 计费秒数（供 New API 按秒计费） ----------------
# New API 只认「按 token 倍率」与「按次固定价」，没有视频按秒维度；
# 按秒的做法：网关把「计费秒数」当 completion_tokens 上报，New API 侧按 token 定价即等价按秒。
#   USAGE_BILLING_SECONDS=1        → 生成响应的 usage.completion_tokens = 计费秒数
#   BILLING_SECONDS_MODE=total     → 计费秒 = ceil(输出秒 + Σ参考视频秒)（默认，与上游口径一致）
#                         output   → 只算输出秒
# 轮询（@query）响应固定 usage=0。建议 New API 侧给轮询单独用一个 0 价模型名（如 capcut-poll），
# 网关对轮询请求不校验模型名，因此可自由取用。
_ref_sec_cache: dict[str, float] = {}


def _mvhd_seconds(data: bytes) -> float:
    """从字节流里找 mvhd 读时长（秒）；moov 常位于文件尾，故头尾都要试。"""
    i = data.find(b"mvhd")
    if i < 0 or len(data) < i + 24:
        return 0.0
    ver = data[i + 4]
    try:
        if ver == 1:
            ts = int.from_bytes(data[i + 24:i + 28], "big")
            dur = int.from_bytes(data[i + 28:i + 36], "big")
        else:
            ts = int.from_bytes(data[i + 16:i + 20], "big")
            dur = int.from_bytes(data[i + 20:i + 24], "big")
    except Exception:  # noqa: BLE001
        return 0.0
    return float(dur) / float(ts) if ts else 0.0


def _remote_video_seconds(url: str) -> float:
    """远程 mp4 时长（秒）：头尾各取 1MB 找 mvhd，失败返回 0（带缓存）。"""
    if url in _ref_sec_cache:
        return _ref_sec_cache[url]
    secs = 0.0
    try:
        import requests
        for rng in ("bytes=0-1048575", "bytes=-1048576"):
            try:
                r = requests.get(url, headers={"Range": rng}, timeout=20)
                if r.status_code not in (200, 206):
                    continue
                secs = _mvhd_seconds(r.content)
                if secs:
                    break
            except Exception as e:  # noqa: BLE001
                log.debug("参考视频时长读取失败 %s: %s", url, e)
    except Exception as e:  # noqa: BLE001
        log.debug("参考视频时长探测异常: %s", e)
    if not secs:
        log.warning("参考视频时长探测失败（计 0）：%s", url)
    _ref_sec_cache[url] = secs
    return secs


def _ref_video_urls(params: dict) -> list[str]:
    supplied = params.get("reference_files")
    if isinstance(supplied, list):
        return [it["url"] for it in supplied
                if isinstance(it, dict) and it.get("role") == "referenceVideo"
                and isinstance(it.get("url"), str)]
    raw = params.get("video")
    if isinstance(raw, str):
        raw = [raw]
    return [x for x in (raw or []) if isinstance(x, str)]


def billing_seconds(model: ModelEntry, params: dict) -> dict:
    """计费秒数拆解：ceil(输出秒 + Σ参考视频秒)——与 CapCut 上游计费口径一致。"""
    import math
    mode = (os.getenv("BILLING_SECONDS_MODE", "total") or "total").strip().lower()
    out_s = 0.0
    try:
        secs = params.get("duration")
        if secs is None:
            secs = params.get("duration_seconds")
        if secs is None:
            secs = params.get("seconds")
        if secs is None:
            secs = (model.param_template or {}).get("duration", 5)
        if (model.provider or "") == "capcut":
            from ..capcut_channel import clamp_duration
            from ..service import gen_limits_of
            lim = gen_limits_of(model)
            out_s = float(clamp_duration(secs, lim["durations"],
                                         lim["min_duration"], lim["max_duration"]))
        else:
            out_s = float(secs)
    except Exception as e:  # noqa: BLE001
        log.debug("计费输出秒解析失败: %s", e)
    ref_s = 0.0
    if mode != "output":
        for u in _ref_video_urls(params):
            ref_s += _remote_video_seconds(u)
    total = out_s + ref_s
    return {
        "billable_seconds": int(math.ceil(total)) if total > 0 else 0,
        "output_seconds": round(out_s, 3),
        "reference_video_seconds": round(ref_s, 3),
        "mode": mode,
    }


@router.post("/chat/completions")
async def chat_completions(request: Request, authorization: str = Header(default=""),
                           db: Session = Depends(db_session)):
    """统一兼容入口：把对话消息转成生成任务；最多等待 CHAT_WAIT_SECONDS（默认 55）秒，超时返回任务号。"""
    key, err = _get_key(db, authorization)
    if err:
        return err
    err = _check_quota(db, key)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return openai_error(400, "请求体必须为 JSON")

    # 任务查询（魔法指令），不占用生成配额
    poll_id = _poll_target(body)
    if poll_id:
        g, qerr = await _find_generation(db, key, poll_id)
        if qerr:
            return qerr
        payload = _poll_chat_payload(g)
        if body.get("stream"):
            async def _sse():
                chunk = dict(payload)
                chunk["object"] = "chat.completion.chunk"
                chunk["choices"] = [{
                    "index": 0,
                    "delta": {"role": "assistant",
                              "content": payload["choices"][0]["message"]["content"]},
                    "finish_reason": "stop",
                }]
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"
            return StreamingResponse(_sse(), media_type="text/event-stream")
        return payload

    model_id = str(body.get("model") or "").strip()
    if not model_id:
        return openai_error(400, "缺少 model 参数")
    alias = resolve_model_alias(model_id)
    model, err = _check_model(db, key, alias[0] if alias else model_id,
                              alias_name=model_id if alias else "")
    if err:
        return err
    messages = body.get("messages") or []
    prompt = ""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user" and m.get("content"):
            c = m["content"]
            prompt = c if isinstance(c, str) else " ".join(
                p.get("text", "") for p in c if isinstance(p, dict))
            break
    extra = {k: v for k, v in body.items()
             if k in (
                 "duration",
                 "size",
                 "negative_prompt",
                 "image", "video", "audio", "reference_files", "keep_prompt", "duration_seconds",
                 "seconds",
                 "aspect_ratio",
                 "resolution",
                 "options",
                 "modality",
                 "seed",
                 "generate_audio",
             )}

    # 兼容 New API extra_body
    extra_body = body.get("extra_body")
    if isinstance(extra_body, dict):
        extra.update(extra_body)
    if alias:
        extra["size"] = alias[1]  # 别名强制分辨率，覆盖请求里的 size/resolution

    result = await _create_generation(db, request, key, model, prompt, extra)
    if isinstance(result, JSONResponse):
        return result
    gen: Generation = result

    try:
        wait_s = int(os.getenv("CHAT_WAIT_SECONDS", "55") or 55)
    except ValueError:
        wait_s = 55
    deadline = time.time() + max(5, min(wait_s, 600))
    while time.time() < deadline:
        db.expire(gen)
        if gen.status in (Generation.STATUS_COMPLETED, Generation.STATUS_FAILED,
                          Generation.STATUS_CANCELLED):
            break
        await asyncio.sleep(2)

    def content_text():
        if gen.status == Generation.STATUS_COMPLETED:
            from ..mcp_client import extract_text
            raw = JSONText.load(gen.result_text) or {}
            text = extract_text(raw).strip()
            if gen.result_url:
                text = (text + "\n" + gen.result_url).strip()
            return text or gen.result_url or "(完成)"
        if gen.status == Generation.STATUS_FAILED:
            return f"生成失败: {gen.error}"
        return (f"任务已提交，正在生成中。task_id: {gen.gen_id}，"
                f"状态: {gen.status}。请通过 GET /v1/generations/{gen.gen_id} 查询，"
                f"或再次发送 @query {gen.gen_id} 轮询。")

    # 按秒计费：把计费秒数伪装成 completion_tokens 上报（New API 侧按 token 定价即等价按秒）
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    bill = None
    if (os.getenv("USAGE_BILLING_SECONDS", "0") or "0").strip().lower() not in ("0", "", "false", "off", "no"):
        bill = await asyncio.to_thread(billing_seconds, model, extra)
        usage = {"prompt_tokens": 0,
                 "completion_tokens": bill["billable_seconds"],
                 "total_tokens": bill["billable_seconds"]}

    return {
        "id": "chatcmpl-" + gen.gen_id,
        "object": "chat.completion",
        "created": int(gen.created_at),
        "model": gen.model_id,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content_text()},
            "finish_reason": "stop",
        }],
        "usage": usage,
        "task_id": gen.gen_id,
        "task_status": gen.status,
        "billing_seconds": bill["billable_seconds"] if bill else None,
        "billing": bill,
    }
