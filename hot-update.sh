#!/usr/bin/env bash
# ============================================================
# 网关不停机热更（在**服务器上**执行）
#   一条命令：
#     curl -fsSL https://raw.githubusercontent.com/sd19092549191/cf-gateway/main/hot-update.sh | sudo bash
#   指定容器名 / 分支：
#     curl -fsSL <上述地址> | sudo bash -s -- --container capcut2 --branch main
#
# 干什么：从仓库 raw 拉最新代码 → docker cp 进容器 → docker restart → 自检模型名。
# ⚠️ docker cp 之后**必须 restart**，否则 uvicorn 不会重新加载（历史踩坑）。
# ============================================================
set -euo pipefail

REPO="${REPO:-sd19092549191/cf-gateway}"
BRANCH="${BRANCH:-main}"
CONTAINER="${CONTAINER:-capcut2}"
RESTART=1
# 运行期代码（其余文件不动）
FILES=(
  app/db.py
  app/capcut_channel.py
  app/routers/openai.py
)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --container) CONTAINER="${2:?}"; shift 2 ;;
    --branch)    BRANCH="${2:?}";    shift 2 ;;
    --no-restart) RESTART=0;         shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "[!] 未知参数: $1"; exit 1 ;;
  esac
done

log()  { printf '\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "找不到 docker"

# 容器名兜底：用户没给 / 给的容器不存在时，自动找一个像网关的
if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  warn "容器 $CONTAINER 不存在，尝试自动识别…"
  CAND="$(docker ps --format '{{.Names}}' | grep -iE 'capcut|gateway' | head -1 || true)"
  [[ -n "$CAND" ]] || die "没找到疑似网关容器，请用 --container <名字> 指定（docker ps 看）"
  CONTAINER="$CAND"
fi
log "目标容器: $CONTAINER"

# 记录更前的版本，便于回滚判断
BEFORE="$(docker exec -i "$CONTAINER" python - <<'PY' 2>/dev/null || echo "(读取失败)"
import sqlite3, os, glob
p = "/app/relay_data/cf_gateway.db"
if not os.path.exists(p):
    cand = glob.glob("/app/**/cf_gateway.db", recursive=True)
    p = cand[0] if cand else p
try:
    ids = [r[0] for r in sqlite3.connect(p).execute("select model_id from models order by id")]
    print(",".join(ids[:8]))
except Exception as e:
    print("db error:", e)
PY
)"
log "更前模型名: $BEFORE"

TMP="$(mktemp -d)"
for f in "${FILES[@]}"; do
  log "拉取 $f"
  curl -fsSL "https://raw.githubusercontent.com/${REPO}/${BRANCH}/${f}" -o "$TMP/$(basename "$f")" \
    || die "下载失败: $f"
  # 语法自检，避免把坏文件塞进容器
  python3 -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$TMP/$(basename "$f")" \
    || die "语法错误: $f"
  docker cp "$TMP/$(basename "$f")" "${CONTAINER}:/app/${f}"
done
rm -rf "$TMP"

if [[ "$RESTART" == "1" ]]; then
  log "重启容器…"
  docker restart "$CONTAINER" >/dev/null
  for i in $(seq 1 30); do
    sleep 2
    if docker exec "$CONTAINER" python -c "
import urllib.request,sys
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3).status==200 else 1)" >/dev/null 2>&1; then
      log "服务已就绪 ✅（等待 $((i*2))s）"; break
    fi
    [[ "$i" == "30" ]] && { warn "健康检查超时，看日志：docker logs --tail 60 $CONTAINER"; }
  done
fi

AFTER="$(docker exec -i "$CONTAINER" python - <<'PY' 2>/dev/null || echo "(读取失败)"
import sqlite3, os, glob
p = "/app/relay_data/cf_gateway.db"
if not os.path.exists(p):
    cand = glob.glob("/app/**/cf_gateway.db", recursive=True)
    p = cand[0] if cand else p
try:
    ids = [r[0] for r in sqlite3.connect(p).execute("select model_id from models order by id")]
    print(",".join(ids[:8]))
except Exception as e:
    print("db error:", e)
PY
)"
log "更后模型名: $AFTER"

echo
if printf '%s' "$AFTER" | grep -qi capcut; then
  echo "❌ 仍存在含品牌字样的模型名，请检查："
  echo "   docker logs --tail 80 $CONTAINER"
  exit 1
fi
cat <<'EOF'
============================================================
 热更完成 ✅
------------------------------------------------------------
 自检（在任意能访问网关的机器上）：
   curl -s http://127.0.0.1:8000/v1/models -H "Authorization: Bearer <网关Key>" \
     | grep -i capcut && echo "❌ 有残留" || echo "✅ 已脱敏"
============================================================
EOF
