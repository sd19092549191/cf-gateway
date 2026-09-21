import os
import secrets

_ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def _load_dotenv(path: str = _ENV_FILE) -> int:
    """从项目根 .env 读取配置（零依赖）。

    规则：``KEY=VALUE``、``#`` 开头为注释、允许 ``export `` 前缀、值两侧引号会被剥离、
    **未加引号的值会剥掉行内注释**（``KEY=2  # 说明`` → ``2``）。
    已存在的进程环境变量优先（Docker / systemd 注入不会被本地 .env 覆盖）。
    """
    if not os.path.exists(path):
        return 0
    loaded = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                else:
                    # 未加引号：剥掉 " #" 起的行内注释（# 前必须有空白）
                    for i, ch in enumerate(value):
                        if ch == "#" and i > 0 and value[i - 1] in " \t":
                            value = value[:i].rstrip()
                            break
                if key and key not in os.environ:
                    os.environ[key] = value
                    loaded += 1
    except OSError:
        return 0
    return loaded


DOTENV_LOADED = _load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


_PROXY_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy")


def sanitize_proxy_env() -> list[str]:
    """服务端出网默认直连：清掉从父 shell 继承来的代理变量。

    为什么必须做：常驻网关常常是在带 ``HTTP(S)_PROXY`` 的交互式 shell（IDE 终端、沙箱等）
    里启动的，而那些代理是**会话级**的——会话一结束代理就没了，进程却还拿着旧值。
    结果就是「端口在监听、/health 正常，但任何出网动作全废」：
    R2 上传 / CapCut 提交 / MCP 调用一律报
    ``Failed to connect to proxy URL: http://127.0.0.1:xxxxx``。
    （这坑真踩过：本机 8001 常驻进程继承了沙箱代理，导致 ``/v1/files`` 上传必挂。）

    规则：
    - 默认清空代理变量（大小写都清）→ 直连；
    - 要走代理请显式设 ``RELAY_OUTBOUND_PROXY=http://host:port``，会写回大小写两套变量；
    - 想保留父进程原样，设 ``RELAY_KEEP_PROXY_ENV=1``（仅调试用）。
    """
    if _env("RELAY_KEEP_PROXY_ENV").lower() in ("1", "true", "yes", "on"):
        return []
    removed = [n for n in _PROXY_ENV_NAMES if os.environ.get(n)]
    for name in _PROXY_ENV_NAMES:
        os.environ.pop(name, None)
    explicit = _env("RELAY_OUTBOUND_PROXY")
    if explicit:
        os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = explicit
        os.environ["HTTPS_PROXY"] = os.environ["https_proxy"] = explicit
    return removed


# 必须在任何出网客户端（httpx / requests / boto3）被创建之前执行。
PROXY_ENV_REMOVED = sanitize_proxy_env()


class Config:
    # 管理后台账号（容器环境变量注入；未设置时使用默认值并告警）
    ADMIN_USER = _env("ADMIN_USER", "admin")
    ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "")
    ADMIN_PASSWORD_DEFAULT = "cf-admin-2026"  # 未配置时使用的默认密码（部署时会强制生成随机密码）

    # 对外访问地址（用于 OAuth 回调 redirect_uri 等）
    PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

    # Creative Fabrica MCP / OAuth（均为动态发现，这里只是初始入口）
    MCP_ENDPOINT = _env("MCP_ENDPOINT", "https://mcp.creativefabrica.com/mcp")
    MCP_RESOURCE = _env("MCP_RESOURCE", "https://mcp.creativefabrica.com/mcp")
    OAUTH_ISSUER = _env("OAUTH_ISSUER", "https://oauth.creativefabrica.com")
    OAUTH_SCOPES = _env("OAUTH_SCOPES", "mcp:read mcp:generate offline_access")

    DATA_DIR = _env("DATA_DIR", "/data")
    DB_PATH = os.path.join(DATA_DIR, "cf_gateway.db")

    # 敏感凭据加密密钥；未提供时首次启动生成并持久化到 DATA_DIR/.secret_key
    SECRET_KEY = _env("SECRET_KEY", "")

    # Worker
    WORKER_INTERVAL = float(_env("WORKER_INTERVAL", "2"))
    TOOLS_SYNC_INTERVAL = float(_env("TOOLS_SYNC_INTERVAL", "3600"))
    MAX_RETRIES = int(_env("MAX_RETRIES", "2"))
    REQUEST_GAP = float(_env("REQUEST_GAP", "3"))       # 每账号两次请求最小间隔（秒）
    POLL_INTERVAL = float(_env("POLL_INTERVAL", "10"))  # 生成轮询间隔（秒）

    CLIENT_NAME = "CF-NewAPI-Gateway"
    CLIENT_VERSION = "1.0.0"

    # R2 转存（全部配置就位才启用；把生成结果转存到客户 R2，对外不暴露源站链接）
    R2_ENDPOINT = _env("R2_ENDPOINT", "")
    R2_ACCESS_KEY_ID = _env("R2_ACCESS_KEY_ID", "")
    R2_SECRET_ACCESS_KEY = _env("R2_SECRET_ACCESS_KEY", "")
    R2_BUCKET = _env("R2_BUCKET", "")
    R2_PUBLIC_BASE = _env("R2_PUBLIC_BASE", "")
    R2_KEY_PREFIX = _env("R2_KEY_PREFIX", "cf-gateway/")
    # 参考素材（ref/ 前缀）在 R2 的保留时长（小时）：中转用完即弃，防桶内堆积。
    # 0 = 不自动清理。worker 每 6 小时跑一轮。
    R2_REF_RETENTION_HOURS = float(_env("R2_REF_RETENTION_HOURS", "48"))


_config = None


def get_config() -> Config:
    global _config
    return _config or Config()


def load_secret_key() -> str:
    """返回用于 AES 加密与 JWT 签名的密钥（hex，32 字节）。"""
    cfg = get_config()
    if cfg.SECRET_KEY:
        return cfg.SECRET_KEY
    path = os.path.join(cfg.DATA_DIR, ".secret_key")
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            key = f.read().strip()
            if key:
                return key
    key = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


SECRET_KEY = ""  # 在 main.py 启动时由 load_secret_key() 填充
