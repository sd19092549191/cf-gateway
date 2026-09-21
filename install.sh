#!/usr/bin/env bash
# ============================================================
# CF NewAPI Gateway 一键部署（Ubuntu / Debian / 1Panel）
#   全新服务器一条命令（自动下载源码包）：
#     curl -fsSL https://raw.githubusercontent.com/sd19092549191/cf-gateway/main/install.sh | sudo bash
#   带参数：
#     curl -fsSL <上述地址> | sudo bash -s -- --port 8000 --base-url http://1.2.3.4:8000
#   源码已在本地（仓库目录内）：
#     sudo bash install.sh
# ============================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPO="sd19092549191/cf-gateway"
BRANCH="main"
RELEASE_TAG="v1.0.0"

# ---------- 0. 自举：不在源码目录时自动下载 ----------
if [[ ! -f "$DIR/docker-compose.yml" ]]; then
  printf '\033[1;36m[自举]\033[0m 下载源码（%s 分支最新）...\n' "$BRANCH"
  command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl unzip; }
  command -v tar   >/dev/null 2>&1 || apt-get install -y -qq tar
  command -v unzip >/dev/null 2>&1 || apt-get install -y -qq unzip
  rm -rf "$DIR/cf-gateway" && mkdir -p "$DIR/cf-gateway"
  # ① 公开仓库：优先拉 main 分支源码包（永远最新，不受 Release 资产是否更新影响）
  if curl -fsSL -o /tmp/cf-gateway.tar.gz \
       "https://codeload.github.com/${REPO}/tar.gz/refs/heads/${BRANCH}"; then
    tar -xzf /tmp/cf-gateway.tar.gz -C "$DIR/cf-gateway" --strip-components=1
  # ② 兜底：Release 资产
  elif curl -fsSL -o /tmp/cf-gateway.zip \
       "https://github.com/${REPO}/releases/download/${RELEASE_TAG}/cf-gateway-1.0.0.zip"; then
    unzip -q /tmp/cf-gateway.zip -d "$DIR/cf-gateway"
    DIR="$(echo "$DIR"/cf-gateway/cf-gateway-*/)"
  else
    # ③ 兜底：转私或以上都失败时用 zipball API（需 GH_TOKEN）
    GH_TOKEN="${GH_TOKEN:-}"
    [[ -n "$GH_TOKEN" ]] || {
      printf '\033[1;31m[x]\033[0m 源码下载失败且未提供 GH_TOKEN。\n' >&2
      echo '    用法: GH_TOKEN=<PAT> bash install.sh' >&2
      exit 1
    }
    curl -fsSL -H "Authorization: token ${GH_TOKEN}" \
         -o /tmp/cf-gateway.zip "https://api.github.com/repos/${REPO}/zipball/${BRANCH}"
    unzip -q /tmp/cf-gateway.zip -d "$DIR/cf-gateway"
    DIR="$(echo "$DIR"/cf-gateway/sd19092549191-cf-gateway-*/)"
    [[ -f "$DIR/docker-compose.yml" ]] || DIR="$(echo "$DIR"/cf-gateway-*/)"
  fi
  cd "$DIR"
  printf '\033[1;36m[自举]\033[0m 源码就绪: %s\n' "$DIR"
fi

cd "$DIR"

PORT="8000"
BASE_URL=""
SKIP_DOCKER_INSTALL="0"

usage() {
  cat <<'USAGE'
用法: sudo bash install.sh [选项]
  --port <端口>          宿主机端口（默认 8000）
  --base-url <地址>      对外访问地址（默认 http://<本机IP>:<端口>）
  --skip-docker-install  不自动安装 Docker（已装好时用）
  -h, --help             显示帮助
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="${2:?缺少端口}"; shift 2 ;;
    --base-url) BASE_URL="${2:?缺少地址}"; shift 2 ;;
    --skip-docker-install) SKIP_DOCKER_INSTALL="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[!] 未知参数: $1"; usage; exit 1 ;;
  esac
done

log()  { printf '\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 1. 环境检查 ----------
[[ "$(uname -s)" == "Linux" ]] || die "本脚本面向 Linux；macOS / Windows 请用 DEPLOY.md 的手动步骤"

if [[ "${EUID}" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    log "需要 root 权限，正在通过 sudo 重新执行…"
    exec sudo -E bash "$0" "$@"
  fi
  die "请用 root 或 sudo 运行"
fi

# ---------- 2. Docker 检测 / 安装 ----------
if ! command -v docker >/dev/null 2>&1; then
  [[ "$SKIP_DOCKER_INSTALL" == "1" ]] && die "未检测到 docker，且指定了 --skip-docker-install"
  log "未检测到 Docker，正在安装（官方脚本，可能需要几分钟）…"
  command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl; }
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
else
  log "已检测到 Docker: $(docker --version)"
fi

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  die "未找到 docker compose，请安装 docker-compose-plugin 后重试"
fi
log "Compose 命令: $DC"

# ---------- 3. 生成 .env ----------
if [[ -f .env ]]; then
  log ".env 已存在，保留现有配置（如需重配请先备份后删除）"
else
  log "生成 .env …"
  ADMIN_PASS="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 20)"
  SECRET="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  if [[ -z "$BASE_URL" ]]; then
    IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    BASE_URL="http://${IP:-127.0.0.1}:${PORT}"
  fi
  cp .env.example .env
  sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${ADMIN_PASS}|" .env
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET}|" .env
  sed -i "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=${BASE_URL}|" .env
  sed -i "s|^GATEWAY_HOST_PORT=.*|GATEWAY_HOST_PORT=${PORT}|" .env
  warn "已生成随机管理密码：${ADMIN_PASS}（请立即保存）"
fi

# 从 .env 读回实际端口，避免和命令行参数不一致
PORT="$(grep -E '^GATEWAY_HOST_PORT=' .env | cut -d= -f2- | tr -d '[:space:]')"
PORT="${PORT:-8000}"

command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl; }

# ---------- 4. 构建并启动 ----------
if grep -qE '^R2_ACCESS_KEY_ID=.+$' .env; then
  log "检测到 R2 已配置，「官转」模式可用"
else
  warn "R2 未配置：「官转」密钥会自动回退为官链，/v1/files 上传参考素材不可用"
fi

log "构建镜像并启动（首次构建需要几分钟）…"
$DC up -d --build

# ---------- 5. 健康检查 ----------
log "等待服务就绪…"
for i in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    log "服务已就绪 ✅"
    break
  fi
  [[ "$i" == "40" ]] && { warn "等待超时，请查看日志：$DC logs -f"; exit 1; }
  sleep 3
done

BASE_URL="$(grep -E '^PUBLIC_BASE_URL=' .env | cut -d= -f2- | tr -d '[:space:]')"
ADMIN_PASS="$(grep -E '^ADMIN_PASSWORD=' .env | cut -d= -f2- | tr -d '[:space:]')"

cat <<EOF

============================================================
 部署完成 🎉
============================================================
 管理后台   : ${BASE_URL}/admin
 用户名     : admin
 密码       : ${ADMIN_PASS}
 健康检查   : ${BASE_URL}/health
────────────────────────────────────────────────────────────
 接入 New API 时填写：
   渠道 Base URL : ${BASE_URL}
   模型名         : 在后台「模型」页启用后，用 GET /v1/models 获取
   渠道 API Key   : 在后台「API 密钥」页创建，形如 sk-cf-xxxx
────────────────────────────────────────────────────────────
 常用命令（在 ${DIR} 下执行）：
   $DC logs -f           # 实时日志
   $DC restart           # 重启
   $DC down              # 停止
   $DC up -d --build     # 改代码后重建
 数据持久化目录：${DIR}/data（勿删）
============================================================
 下一步：打开管理后台 → 「账号」添加 CapCut 账号 Cookie
        → 「模型」同步目录并启用所需模型 → 「API 密钥」创建密钥
============================================================
EOF
