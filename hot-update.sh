#!/usr/bin/env bash
# ============================================================
# 网关「只更新代码」热更脚本（在**服务器上**执行，不停机、不动数据）
#
#   一条命令：
#     curl -fsSL https://raw.githubusercontent.com/sd19092549191/cf-gateway/main/hot-update.sh | sudo bash
#
#   常用参数（透传：... | sudo bash -s -- <参数>）：
#     --container <名>   目标容器（默认 capcut2；不存在时自动识别含 capcut/gateway 的容器）
#     --branch <名>      拉哪个分支（默认 main）
#     --only <路径,...>  只更新指定文件（默认同步整个 app/ 与 static/）
#     --dry-run          只拉代码 + 语法自检 + 打印动作，不碰容器
#     --no-restart       复制后不重启（⚠️ 不重启不生效，仅配合手工操作）
#     --rollback         用上一次备份回滚代码并重启
#
# 它做什么：拉源码包 → 语法自检 → 备份容器内 app/ → docker cp 覆盖 → restart → 健康检查 → 模型名自检
# 它不动什么：/data 数据卷（账号 Cookie、密钥、任务记录）、.env、docker 编排、New API 配置
# ⚠️ docker cp 之后**必须 restart**，否则 uvicorn 不会重新加载（历史踩坑）。
# ⚠️ 如果 requirements.txt 变了，热更不够，需在源码目录重建镜像：docker compose up -d --build
# ============================================================
set -euo pipefail

REPO="${REPO:-sd19092549191/cf-gateway}"
BRANCH="${BRANCH:-main}"
CONTAINER="${CONTAINER:-capcut2}"
RESTART=1
DRY_RUN=0
ROLLBACK=0
ONLY=""
BACKUP_DIR="${BACKUP_DIR:-$PWD/.hot-update-backup}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --container)   CONTAINER="${2:?}"; shift 2 ;;
    --branch)      BRANCH="${2:?}";    shift 2 ;;
    --only)        ONLY="${2:?}";      shift 2 ;;
    --dry-run)     DRY_RUN=1;          shift ;;
    --no-restart)  RESTART=0;          shift ;;
    --rollback)    ROLLBACK=1;         shift ;;
    -h|--help)     sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "[!] 未知参数: $1"; exit 1 ;;
  esac
done

log()  { printf '\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

need_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    [[ "$DRY_RUN" == "1" ]] && { warn "本机无 docker：dry-run 只做「下载 + 语法自检」"; return 0; }
    die "找不到 docker"
  fi
  if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    warn "容器 $CONTAINER 不存在，尝试自动识别…"
    local cand
    cand="$(docker ps --format '{{.Names}}' | grep -iE 'capcut|gateway' | head -1 || true)"
    [[ -n "$cand" ]] || die "没找到疑似网关容器，请用 --container <名字> 指定（docker ps 看）"
    CONTAINER="$cand"
  fi
  log "目标容器: $CONTAINER"
}

# 容器内 DB 路径探测（DATA_DIR=/data，勿写死 /app/relay_data）
read_models() {
  docker exec -i "$CONTAINER" python - <<'PY' 2>/dev/null || echo "(读取失败)"
import os, glob, sqlite3
cands = [os.path.join(os.environ.get("DATA_DIR") or "/data", "cf_gateway.db"), "/data/cf_gateway.db"]
cands += glob.glob("/data/**/cf_gateway.db", recursive=True)
cands += glob.glob("/app/**/cf_gateway.db", recursive=True)
p = next((c for c in cands if os.path.exists(c)), None)
if not p:
    print("(未找到 DB)")
else:
    try:
        print(",".join(r[0] for r in sqlite3.connect(p).execute("select model_id from models order by id")))
    except Exception as e:
        print("db error: %s" % e)
PY
}

restart_and_wait() {
  log "重启容器…"
  docker restart "$CONTAINER" >/dev/null
  for i in $(seq 1 30); do
    sleep 2
    if docker exec "$CONTAINER" python -c "
import urllib.request,sys
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3).status==200 else 1)" >/dev/null 2>&1; then
      log "服务已就绪 ✅（等待 $((i*2))s）"; return 0
    fi
  done
  warn "健康检查超时，看日志：docker logs --tail 60 $CONTAINER"
}

need_docker

# ---------- 回滚分支 ----------
if [[ "$ROLLBACK" == "1" ]]; then
  LATEST="$(ls -1dt "$BACKUP_DIR"/app-* 2>/dev/null | head -1 || true)"
  [[ -n "$LATEST" ]] || die "备份目录 $BACKUP_DIR 里没有可用备份"
  log "回滚自: $LATEST"
  docker cp "$LATEST/." "$CONTAINER:/app/"
  restart_and_wait
  log "更后模型名: $(read_models)"
  exit 0
fi

BEFORE="$(read_models)"
log "更前模型名: $BEFORE"

# ---------- 1. 备份容器内代码（可回滚） ----------
BAK="$BACKUP_DIR/app-$(date +%Y%m%d-%H%M%S)"
if [[ "$DRY_RUN" != "1" ]]; then
  mkdir -p "$BAK"
  docker cp "$CONTAINER:/app/app" "$BAK/app" 2>/dev/null || warn "备份 app/ 失败（继续，仅影响回滚）"
  docker cp "$CONTAINER:/app/static" "$BAK/static" 2>/dev/null || true
  log "已备份到 ${BAK}（回滚：re-run 加 --rollback）"
fi

# ---------- 2. 拉最新源码 ----------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
log "拉取源码（${REPO}@${BRANCH}）…"
command -v curl >/dev/null 2>&1 || die "缺少 curl"
curl -fsSL -o "$TMP/src.tar.gz" \
  "https://codeload.github.com/${REPO}/tar.gz/refs/heads/${BRANCH}" \
  || die "源码下载失败（检查网络 / 分支名）"
tar -xzf "$TMP/src.tar.gz" -C "$TMP" --strip-components=1
[[ -d "$TMP/app" ]] || die "源码包里没有 app/ 目录"

# ---------- 3. 语法自检 ----------
log "语法自检…"
python3 - "$TMP" <<'PY' || die "语法自检失败，已中止（容器未被修改）"
import ast, pathlib, sys
bad = []
for p in pathlib.Path(sys.argv[1], "app").rglob("*.py"):
    try:
        ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError as e:
        bad.append(f"{p}: {e}")
if bad:
    print("\n".join(bad)); sys.exit(1)
print("  OK: app/ 全部 .py 解析通过")
PY

py_sha1() { python3 -c "import hashlib,sys;print(hashlib.sha1(open(sys.argv[1],'rb').read()).hexdigest())" "$1"; }

# ---------- 4. 依赖变更检测 ----------
if [[ -f "$TMP/requirements.txt" ]]; then
  NEW_REQ="$(py_sha1 "$TMP/requirements.txt" 2>/dev/null || echo '')"
  OLD_REQ="$(docker exec "$CONTAINER" python -c "import hashlib;print(hashlib.sha1(open('/app/requirements.txt','rb').read()).hexdigest())" 2>/dev/null || echo '')"
  if [[ -n "$OLD_REQ" && -n "$NEW_REQ" && "$NEW_REQ" != "$OLD_REQ" ]]; then
    warn "requirements.txt 有变化 —— 热更**不含依赖**。请到源码目录执行："
    warn "  docker compose up -d --build    # 或 1Panel 里重建该编排"
  fi
fi

# ---------- 5. 复制进容器 ----------
if [[ -n "$ONLY" ]]; then
  IFS=',' read -r -a LIST <<< "$ONLY"
  for f in "${LIST[@]}"; do
    f="$(printf '%s' "$f" | tr -d '[:space:]')"   # 去空格
    SRC="$TMP/$f"; [[ -f "$SRC" ]] || die "源码里没有 $f"
    log "复制 $f （--dry-run=${DRY_RUN}）"
    [[ "$DRY_RUN" == "1" ]] || docker cp "$SRC" "$CONTAINER:/app/$f"
  done
else
  log "复制 app/ （--dry-run=${DRY_RUN}）"
  if [[ "$DRY_RUN" != "1" ]]; then
    docker cp "$TMP/app/." "$CONTAINER:/app/app/"
    [[ -d "$TMP/static" ]] && docker cp "$TMP/static/." "$CONTAINER:/app/static/"
  fi
fi

# ---------- 6. 重启 + 自检 ----------
if [[ "$DRY_RUN" == "1" ]]; then
  echo; log "dry-run 结束（容器未改动）。去掉 --dry-run 即真正执行。"; exit 0
fi

if [[ "$RESTART" == "1" ]]; then restart_and_wait; fi

AFTER="$(read_models)"
log "更后模型名: $AFTER"

echo
if printf '%s' "$AFTER" | grep -qi capcut; then
  echo "❌ 仍存在含品牌字样的模型名，检查：docker logs --tail 80 $CONTAINER"
  echo "   需要回滚：curl -fsSL <本脚本地址> | sudo bash -s -- --rollback"
  exit 1
fi
cat <<'EOF'
============================================================
 只更新完成 ✅（数据/账号/密钥/编排均未改动）
------------------------------------------------------------
 自检（任意能访问网关的机器）：
   curl -s http://127.0.0.1:8000/v1/models -H "Authorization: Bearer <网关Key>" \
     | grep -i capcut && echo "❌ 有残留" || echo "✅ 已脱敏"
 版本/健康：
   curl -s http://127.0.0.1:8000/health
 回滚（如需）：
   curl -fsSL <本脚本地址> | sudo bash -s -- --rollback
============================================================
EOF
