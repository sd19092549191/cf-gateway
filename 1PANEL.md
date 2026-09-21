# 1Panel 部署（5 步版）

> 前提：服务器已装 1Panel（含 Docker）。全程只需要浏览器 + 面板文件管理器，不用 SSH 也行。

## 0. 最快路径：一条命令（有 SSH 时）

```bash
curl -fsSL https://raw.githubusercontent.com/sd19092549191/cf-gateway/main/install.sh | sudo bash
```

会自动下载源码包 → 装 Docker（缺才装）→ 生成随机管理密码的 .env → 构建启动 → 健康检查。
只需要 1Panel 编排（不带自动安装）的话：下载 zip 解压到 `/opt/1panel/docker/compose/cf-gateway/` 后按下面 5 步走。

## 第 1 步：上传并解压（顺序铁律：先传文件，再建编排）

1. 面板 → 主机 → 文件 → 进入 `/opt/1panel/docker/compose/`
2. 新建文件夹 `cf-gateway`
3. 把 `cf-gateway-1.0.0.zip` 上传进去并解压（解压后目录里应能看到 `Dockerfile`、`docker-compose.yml`、`app/`）

## 第 2 步：创建 .env（在 `/opt/1panel/docker/compose/cf-gateway/` 下）

```bash
cp .env.example .env
```

最少只需要改 2 行：

```ini
ADMIN_PASSWORD=换成你的强密码
PUBLIC_BASE_URL=http://服务器IP:8000
```

（`GATEWAY_PORT=8000`、`GATEWAY_HOST_PORT=8000`、`DATA_DIR=/data` 保持默认即可）

## 第 3 步：创建编排

面板 → 容器 → 编排 → 创建编排：

- **名称**：`cf-gateway`（小写，只填名字，**不要填路径**）
- **文件夹**：选 `cf-gateway`
- 「忽略服务器已存在的镜像，重新拉取一次」**不要勾**
- 首次构建约 1-2 分钟（拉 python:3.12-slim + 装依赖）

## 第 4 步：放行端口（两处都要）

1. 面板 → 主机 → 防火墙 → 放行 `8000/tcp`
2. 云厂商控制台 → 安全组 → 入站放行 `8000`

## 第 5 步：验收

```bash
curl http://服务器IP:8000/health        # 应返回 {"status":"ok",...}
```

浏览器打开 `http://服务器IP:8000/admin` → 用 `admin` + 你设的密码登录 → 账号管理 → 批量导入 Cookie（渠道选 **CapCut**）→ 模型页确认 `sd-seedance-2.0` / `sd-seedance-2.5` → 后台「API Key」里签一把 Key 给 New API / 客户端用。

---

## 出错速查

| 症状 | 原因 | 修法 |
|---|---|---|
| 创建编排报 `pull access denied for cf-gateway` | compose 里残留 `image:` 行 | 确认用的是本包最新 `docker-compose.yml`（已删除该行） |
| `open Dockerfile: no such file or directory` | 先建编排后传文件 | 删编排 → 传文件解压 → 重建 |
| 编排名报「支持非特殊字符开头…」 | 名称里有大写/路径 | 只填 `cf-gateway` |
| 容器反复重启，日志同一个 Traceback | `.env` 没建或密码没改 | 补第 2 步 |
| `/health` 正常但上传/提交报 `Failed to connect to proxy URL` | 容器继承了代理变量 | 本镜像默认已清理代理直连；确需代理在 `.env` 设 `RELAY_OUTBOUND_PROXY` |
| 改了代码想更新 | 面板「重启」不重建镜像 | 面板编排 → 编辑保存触发重建；或 `docker compose build --no-cache && docker compose up -d --force-recreate` |

## 数据在哪

数据库 / 加密密钥 / 临时素材都在 `/opt/1panel/docker/compose/cf-gateway/data/`（对应容器 `/data`）。**备份这个目录 = 备份整个网关**（账号 Cookie 是加密存储，密钥也在里面，一起备份才能恢复）。
