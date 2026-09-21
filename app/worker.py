"""后台 Worker：提交（queued→polling）与轮询（polling→终态）两条通道。

状态机：
  queued ──提交 generate──▶ processing ──拿到 generationId──▶ polling ──get_generation──▶ completed/failed
                │ 无可用账号：留 queued                    │ 429：顺延 next_poll_at
                └─ 计费/鉴权类账号错误：标记账号 + 换号重排（retry_count+1）
  服务重启：polling 任务按 next_poll_at 无缝续轮询（不会重复扣费提交）。
"""
import asyncio
import logging
import time

from sqlalchemy import func, select

from .cf_client import CFClient, CFError, TERMINAL_FAIL, TERMINAL_OK, pick_result_url, shrink_for_storage
from .config import get_config
from .db import SessionLocal
from .models import Account, ApiKey, CoinTransaction, Generation, ModelEntry, JSONText, now
from . import service
from . import r2 as r2  # noqa: F401  R2 转存/清理（r2.py 只依赖 config，无循环导入）

log = logging.getLogger("worker")

MAX_REQUEUE = 3             # 换号重排上限
DEFAULT_TIMEOUT = 900       # 默认任务超时（模型级可覆盖）


def _model_timeout(model: ModelEntry | None) -> int:
    return (model.timeout_seconds if model and model.timeout_seconds else DEFAULT_TIMEOUT)


def _finish(db, gen: Generation, status: str, error: str = ""):
    gen.status = status
    gen.error = error[:1500]
    gen.finished_at = now()
    if gen.started_at:
        gen.duration_ms = int((gen.finished_at - gen.started_at) * 1000)
    db.commit()


def _requeue(db, gen: Generation, error: str):
    gen.retry_count += 1
    if gen.retry_count > MAX_REQUEUE:
        _finish(db, gen, Generation.STATUS_FAILED, f"多次尝试后失败：{error}")
        return False
    gen.status = Generation.STATUS_QUEUED
    gen.error = error[:1200]
    gen.account_id = None
    gen.account_name = ""
    gen.started_at = 0
    db.commit()
    return True


async def _submit_one(gen_db_id: int) -> None:
    """提交通道：领取 queued → 选账号 → generate → 转 polling。"""
    db = SessionLocal()
    try:
        gen = db.get(Generation, gen_db_id)
        if not gen or gen.status != Generation.STATUS_QUEUED:
            return
        model = db.execute(select(ModelEntry).where(
            ModelEntry.model_id == gen.model_id)).scalar_one_or_none()
        if not model or not model.enabled:
            _finish(db, gen, Generation.STATUS_FAILED, "模型已停用或不存在")
            return

        exclude = gen.account_id or 0  # 重排时避开上次的失败账号
        account = service.select_account(db, model, exclude_id=exclude)
        if account is None:
            account = service.select_account(db, model)  # 无其他账号时允许回退原账号
        if account is None:
            active = db.execute(select(func.count(Account.id)).where(
                Account.status == Account.STATUS_ACTIVE)).scalar() or 0
            if active == 0:
                # 池中已无任何可用账号（Cookie 失效/积分不足/禁用）：快速失败，给出明确原因
                reason = gen.error or "所有账号不可用（请检查 Cookie、积分与账号状态）"
                _finish(db, gen, Generation.STATUS_FAILED, f"无可用账号：{reason}")
            return  # 账号忙：保持排队

        gen.status = Generation.STATUS_PROCESSING
        gen.account_id = account.id
        gen.account_name = account.name
        gen.started_at = gen.started_at or now()
        db.commit()

        # ---- CapCut 直连通道: 提交后转 polling，由 _poll_one 续查 ----
        if (model.provider or "") == "capcut":
            # 快照该密钥的成片链接方式（官链 official / 官转 r2），出片时按此决定是否转存 R2
            key = db.get(ApiKey, gen.api_key_id) if gen.api_key_id else None
            gen.link_mode = (getattr(key, "capcut_link_mode", "") or "official")
            db.commit()
            await _submit_capcut(db, gen, model, account)
            return

        client = service.client_for(db, account, timeout=max(180.0, _model_timeout(model)))
        try:
            if account.auth_type == "oauth":
                await service.ensure_access_token(db, account)
            args = service.build_generate_args(model, gen.prompt or "", gen.params)
            if not args["prompt"].strip():
                _finish(db, gen, Generation.STATUS_FAILED, "prompt 不能为空")
                return
            result = await client.generate(**args, )
        finally:
            await client.aclose()

        sc = client.structured(result)
        gen_id_cf = str(sc.get("generationId") or "")
        status_cf = str(sc.get("status") or "")
        if not gen_id_cf:
            _finish(db, gen, Generation.STATUS_FAILED,
                    f"上游未返回 generationId（status={status_cf or '空'}）")
            return

        gen.cf_generation_id = gen_id_cf
        gen.cf_status = status_cf
        account.last_used_at = now()
        account.fail_count = 0

        if status_cf in TERMINAL_FAIL:
            gen.result_text = JSONText.dump(shrink_for_storage(result))
            _finish(db, gen, Generation.STATUS_FAILED, f"上游生成失败：{status_cf}")
            return
        if status_cf in TERMINAL_OK:
            await _apply_result(db, gen, account, result)
            _finish(db, gen, Generation.STATUS_COMPLETED)
            return

        gen.status = Generation.STATUS_POLLING
        gen.next_poll_at = now() + get_config().POLL_INTERVAL
        db.commit()
        log.info("任务 %s 已提交（account=%s cf=%s status=%s）",
                 gen.gen_id, account.name, gen_id_cf, status_cf)

    except CFError as e:
        try:
            gen = db.get(Generation, gen_db_id)
            if not gen or gen.status != Generation.STATUS_PROCESSING:
                return
            account = db.get(Account, gen.account_id) if gen.account_id else None
            if account:
                kind = service.note_account_error(db, account, e, str(e))
            else:
                kind = getattr(e, "kind", "upstream")
            if kind in ("billing", "auth"):
                _requeue(db, gen, f"[{kind}] {e}")
            elif kind == "rate_limited":
                gen.status = Generation.STATUS_QUEUED  # 稍后重试提交
                db.commit()
            elif e.transient and gen.retry_count < MAX_REQUEUE:
                _requeue(db, gen, f"[transient] {e}")
            else:
                _finish(db, gen, Generation.STATUS_FAILED, str(e))
        except Exception:
            log.exception("提交异常处理失败 gen_id=%s", gen_db_id)
    except Exception as e:  # noqa: BLE001
        log.exception("任务提交异常 gen_id=%s: %s", gen_db_id, e)
        try:
            gen = db.get(Generation, gen_db_id)
            if gen and gen.status == Generation.STATUS_PROCESSING:
                _finish(db, gen, Generation.STATUS_FAILED, f"内部错误: {e}")
        except Exception:
            pass
    finally:
        db.close()


async def _submit_capcut(db, gen: Generation, model: ModelEntry, account: Account) -> None:
    """CapCut 提交通道：上传参考素材（如有）→ common_task/new → 转 polling。"""
    from . import capcut_channel
    try:
        args = capcut_channel.build_capcut_args(model, gen.prompt or "", gen.params)
        if not args["prompt"].strip():
            _finish(db, gen, Generation.STATUS_FAILED, "prompt 不能为空")
            return
        task_id, token, credit_before = await capcut_channel.submit_capcut(account, args)
    except capcut_channel.CapcutError as e:
        if e.kind == "auth":
            account.status = Account.STATUS_EXPIRED
            account.last_error = str(e)[:300]
            db.commit()
        elif e.kind == "billing":
            account.status = Account.STATUS_INSUFFICIENT
            account.last_error = str(e)[:300]
            db.commit()
        if e.kind in ("auth", "billing"):
            _requeue(db, gen, f"[capcut:{e.kind}] {e}")
        else:
            _finish(db, gen, Generation.STATUS_FAILED, f"CapCut 提交失败: {e}")
        return
    except Exception as e:  # noqa: BLE001
        log.exception("CapCut 提交异常 gen_id=%s", gen.gen_id)
        _finish(db, gen, Generation.STATUS_FAILED, f"CapCut 提交失败: {e}")
        return
    gen.cf_generation_id = task_id
    gen.upstream_token = token
    gen.credit_before = credit_before
    gen.cf_status = "submitted"
    account.last_used_at = now()
    account.fail_count = 0
    gen.status = Generation.STATUS_POLLING
    gen.next_poll_at = now() + get_config().POLL_INTERVAL
    db.commit()
    log.info("CapCut 任务 %s 已提交（account=%s task=%s credit_before=%s）",
             gen.gen_id, account.name, task_id, credit_before)


async def _poll_capcut(db, gen: Generation, model: ModelEntry, account: Account) -> None:
    """CapCut 轮询通道：common_task/query → 终态（成功转 R2/直链 + 积分流水）。"""
    from . import capcut_channel
    if not gen.cf_generation_id or not gen.upstream_token:
        _finish(db, gen, Generation.STATUS_FAILED, "CapCut 任务缺少 task_id/token")
        return
    try:
        st, url, vid, err, raw = await capcut_channel.poll_capcut(
            gen.cf_generation_id, gen.upstream_token, account)
    except capcut_channel.CapcutError as e:
        if e.kind == "auth":
            account.status = Account.STATUS_EXPIRED
            account.last_error = str(e)[:300]
            db.commit()
            _finish(db, gen, Generation.STATUS_FAILED, f"CapCut 账号 Cookie 失效: {e}")
            return
        # 上游/网络抖动: 退避重试，超过 6 次判失败
        gen.retry_count = (gen.retry_count or 0) + 1
        if gen.retry_count > 6:
            _finish(db, gen, Generation.STATUS_FAILED, f"CapCut 轮询多次失败: {e}")
        else:
            gen.next_poll_at = now() + 30
            db.commit()
        return
    except Exception as e:  # noqa: BLE001
        log.exception("CapCut 轮询异常 gen_id=%s", gen.gen_id)
        gen.next_poll_at = now() + 30
        db.commit()
        return

    if st:
        gen.cf_status = str(st)
    s = str(st or "").lower()
    if s in capcut_channel.TERMINAL_OK and url:
        await _capcut_result(db, gen, account, url, vid)
        _finish(db, gen, Generation.STATUS_COMPLETED)
        log.info("CapCut 任务 %s 完成 url=%s", gen.gen_id, (gen.result_url or "")[:80])
    elif s in capcut_channel.TERMINAL_FAIL:
        gen.result_text = JSONText.dump({"status": st, "error": err})
        _finish(db, gen, Generation.STATUS_FAILED, f"CapCut 生成失败: {err or st}")
    else:
        gen.next_poll_at = now() + get_config().POLL_INTERVAL
        db.commit()


async def _capcut_result(db, gen: Generation, account: Account, url: str, vid: str):
    """成片处理: 按密钥链接方式（官链/官转）决定是否 R2 转存 → 积分实耗流水。"""
    from . import capcut_channel, r2
    mode = (gen.link_mode or "official").lower()
    result = {"outputUrl": url, "vid": vid, "status": "succeed", "linkMode": mode}
    final_url = url
    if mode == "r2":
        if not r2.R2Store.enabled():
            result["linkMode"] = "official"
            result["linkNote"] = "官转已选但 R2 未配置完整，已回退官链"
            log.warning("官转需要 R2 配置（R2_ENDPOINT/KEY/SECRET/BUCKET），gen_id=%s 回退官链", gen.gen_id)
        else:
            try:
                public_url = await r2.R2Store.transfer(url, gen.gen_id)
                if public_url:
                    result["outputUrl"] = public_url
                    result["originUrl"] = url
                    final_url = public_url
                else:
                    result["linkMode"] = "official"
                    result["linkNote"] = "R2 转存返回空，已回退官链"
            except Exception as e:  # noqa: BLE001
                result["linkMode"] = "official"
                result["linkNote"] = f"R2 转存失败，已回退官链: {e}"
                log.warning("R2 转存失败（回退源站链接）gen_id=%s: %s", gen.gen_id, e)
    gen.result_text = JSONText.dump(result)
    gen.result_url = final_url
    # 实际积分消耗
    cost = 0.0
    try:
        after = await asyncio.to_thread(capcut_channel.refresh_balance, account)
        cost = max(0.0, (gen.credit_before or 0) - after)
    except Exception as e:  # noqa: BLE001
        log.warning("CapCut 余额刷新失败 gen_id=%s: %s", gen.gen_id, e)
    if cost > 0 and gen.credit_before:
        gen.cost = cost
        db.add(CoinTransaction(
            account_id=account.id, account_name=account.name, generation_id=gen.gen_id,
            kind="auto", before_balance=gen.credit_before, cost=cost,
            after_balance=account.coin_balance or 0, note=f"CapCut {gen.model_id} 生成扣费"))
    account.last_used_at = now()
    db.commit()


async def _apply_result(db, gen: Generation, account: Account | None, result: dict):
    from . import r2
    raw_json = JSONText.dump(shrink_for_storage(result))
    origin_url = pick_result_url(result)
    final_url = origin_url
    # R2 转存：对外只返回客户 R2 桶的链接；失败时回退源站链接
    if origin_url and r2.R2Store.enabled():
        public_url = await r2.R2Store.transfer(origin_url, gen.gen_id)
        if public_url:
            final_url = public_url
            raw_json = r2.R2Store.scrub(raw_json, origin_url, public_url)
    gen.result_text = raw_json
    gen.result_url = final_url
    sc = CFClient.structured(result)
    # 尽力识别积分消耗（structuredContent 中若带 coin/credits 字段）
    for key in ("coinsSpent", "coins", "credits", "cost", "coinAmount"):
        v = sc.get(key)
        if isinstance(v, (int, float)) and v > 0:
            gen.cost = float(v)
            if account:
                service._apply_cost(db, account, gen, gen.cost)
            break


async def _poll_one(gen_db_id: int) -> None:
    """轮询通道：get_generation → 终态。始终绑定创建账号。"""
    db = SessionLocal()
    try:
        gen = db.get(Generation, gen_db_id)
        if not gen or gen.status != Generation.STATUS_POLLING or not gen.cf_generation_id:
            return
        model = db.execute(select(ModelEntry).where(
            ModelEntry.model_id == gen.model_id)).scalar_one_or_none()
        timeout = _model_timeout(model)
        if gen.started_at and time.time() - gen.started_at > timeout + 120:
            _finish(db, gen, Generation.STATUS_FAILED, f"任务超时（>{timeout}s）")
            return

        account = db.get(Account, gen.account_id) if gen.account_id else None
        if not account:
            _finish(db, gen, Generation.STATUS_FAILED, "账号已删除，无法查询生成结果")
            return
        if account.status == Account.STATUS_DISABLED:
            gen.next_poll_at = now() + 60
            db.commit()
            return

        # ---- CapCut 直连通道 ----
        if (model.provider or "") == "capcut":
            await _poll_capcut(db, gen, model, account)
            return

        client = service.client_for(db, account, timeout=120.0)
        try:
            result = await client.get_generation(gen.cf_generation_id)
        finally:
            await client.aclose()

        sc = client.structured(result)
        status_cf = str(sc.get("status") or "")
        if status_cf:
            gen.cf_status = status_cf
        retry_after = int(sc.get("retryAfterSeconds") or 0)

        if status_cf in TERMINAL_OK:
            await _apply_result(db, gen, account, result)
            _finish(db, gen, Generation.STATUS_COMPLETED)
            log.info("任务 %s 完成 url=%s", gen.gen_id, gen.result_url[:80])
        elif status_cf in TERMINAL_FAIL:
            gen.result_text = JSONText.dump(shrink_for_storage(result))
            _finish(db, gen, Generation.STATUS_FAILED, f"上游生成失败：{status_cf}")
        else:
            gen.next_poll_at = now() + max(get_config().POLL_INTERVAL, retry_after)
            db.commit()

    except CFError as e:
        try:
            gen = db.get(Generation, gen_db_id)
            if not gen or gen.status != Generation.STATUS_POLLING:
                return
            if e.kind == "rate_limited":
                gen.next_poll_at = now() + max(e.retry_after or 60, 60)
                db.commit()
            elif e.kind == "auth":
                # Cookie 过期：轮询无法换号（生成在原账号上），标记失败并提示换 Cookie
                account = db.get(Account, gen.account_id) if gen.account_id else None
                if account:
                    account.status = Account.STATUS_EXPIRED
                    account.last_error = str(e)[:300]
                _finish(db, gen, Generation.STATUS_FAILED,
                        f"账号 Cookie 过期，无法继续查询结果：{e}")
            elif e.transient:
                gen.next_poll_at = now() + 30
                db.commit()
            else:
                _finish(db, gen, Generation.STATUS_FAILED, str(e))
        except Exception:
            log.exception("轮询异常处理失败 gen_id=%s", gen_db_id)
    except Exception as e:  # noqa: BLE001
        log.exception("任务轮询异常 gen_id=%s: %s", gen_db_id, e)
        try:
            gen = db.get(Generation, gen_db_id)
            if gen and gen.status == Generation.STATUS_POLLING:
                gen.next_poll_at = now() + 30
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


async def _sync_models_loop():
    cfg = get_config()
    while True:
        await asyncio.sleep(120)
        try:
            db = SessionLocal()
            try:
                accounts = db.execute(select(Account).where(
                    Account.status == Account.STATUS_ACTIVE)).scalars().all()
                for a in accounts:
                    if (a.provider or "") == "capcut":
                        continue  # CapCut 无 MCP 模型目录，模板由启动种子维护
                    if a.tools_synced_at and time.time() - a.tools_synced_at < cfg.TOOLS_SYNC_INTERVAL:
                        continue
                    try:
                        stat = await service.sync_account_models(db, a)
                        log.info("账号 %s 模型目录同步: %s", a.name,
                                 {k: stat[k] for k in ("imported", "updated", "models")})
                    except Exception as e:  # noqa: BLE001
                        log.warning("账号 %s 模型目录同步失败: %s", a.name, e)
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001
            log.warning("模型同步循环异常: %s", e)


async def _stale_cleanup_loop():
    """超时兜底：processing/polling 超时的任务标记失败。"""
    while True:
        await asyncio.sleep(60)
        try:
            db = SessionLocal()
            try:
                rows = db.execute(select(Generation).where(Generation.status.in_(
                    [Generation.STATUS_PROCESSING, Generation.STATUS_POLLING]))).scalars().all()
                for gen in rows:
                    model = db.execute(select(ModelEntry).where(
                        ModelEntry.model_id == gen.model_id)).scalar_one_or_none()
                    limit = _model_timeout(model) * 2 + 600
                    if gen.started_at and time.time() - gen.started_at > limit:
                        _finish(db, gen, Generation.STATUS_FAILED,
                                f"任务超时（>{int(limit)}s），由系统标记失败")
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001
            log.warning("超时清理异常: %s", e)


async def _worker_loop():
    cfg = get_config()
    while True:
        try:
            db = SessionLocal()
            try:
                submit_ids = db.execute(select(Generation.id).where(
                    Generation.status == Generation.STATUS_QUEUED)
                    .order_by(Generation.created_at).limit(10)).scalars().all()
                due = time.time()
                poll_ids = db.execute(select(Generation.id).where(
                    Generation.status == Generation.STATUS_POLLING,
                    Generation.next_poll_at <= due)
                    .order_by(Generation.next_poll_at).limit(30)).scalars().all()
            finally:
                db.close()
            jobs = [asyncio.create_task(_submit_one(i)) for i in submit_ids]
            jobs += [asyncio.create_task(_poll_one(i)) for i in poll_ids]
            if jobs:
                await asyncio.gather(*jobs)
                await asyncio.sleep(1.0)
            else:
                await asyncio.sleep(cfg.WORKER_INTERVAL)
        except Exception as e:  # noqa: BLE001
            log.exception("Worker 循环异常: %s", e)
            await asyncio.sleep(5)


async def _r2_ref_cleanup_loop():
    """R2 参考素材定期清理（中转用完即弃；保留期 R2_REF_RETENTION_HOURS，默认 48h，0=关闭）。"""
    await asyncio.sleep(90)  # 启动先让位给恢复/提交
    while True:
        try:
            n = await asyncio.to_thread(r2.R2Store.cleanup_old_references)
            if n:
                log.info("R2 参考素材清理: 删除 %d 个过期对象", n)
        except Exception as e:  # noqa: BLE001
            log.warning("R2 参考素材清理失败: %s", e)
        await asyncio.sleep(6 * 3600)


async def start_worker():
    # 重启恢复：提交中断的回到队列；已在轮询的保持（按 next_poll_at 续查）
    db = SessionLocal()
    try:
        stuck = db.execute(select(Generation).where(
            Generation.status == Generation.STATUS_PROCESSING)).scalars().all()
        for g in stuck:
            if g.cf_generation_id:
                g.status = Generation.STATUS_POLLING
                g.next_poll_at = now() + 5
            else:
                g.status = Generation.STATUS_QUEUED
                g.started_at = 0
        if stuck:
            db.commit()
            log.info("重启恢复：%d 个中断任务", len(stuck))
    finally:
        db.close()
    asyncio.create_task(_worker_loop(), name="gen-worker")
    asyncio.create_task(_sync_models_loop(), name="models-sync")
    asyncio.create_task(_stale_cleanup_loop(), name="stale-cleanup")
    if r2.R2Store.reference_enabled() and get_config().R2_REF_RETENTION_HOURS > 0:
        asyncio.create_task(_r2_ref_cleanup_loop(), name="r2-ref-cleanup")
        log.info("R2 参考素材自动清理已启用（保留 %g 小时）",
                 get_config().R2_REF_RETENTION_HOURS)
    log.info("Worker 已启动（submit/poll 双通道）")
