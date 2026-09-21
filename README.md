# CF NewAPI Gateway

把 CapCut 直连通道（Seedance 等）与 Creative Fabrica MCP 通道，封装成 **OpenAI / New API 兼容**的
视频、图片生成网关。部署后在 New API 里作为一个渠道即可使用。

- 完整部署与接入说明：**[DEPLOY.md](./DEPLOY.md)**
- 管理后台：`http://<地址>:8000/admin`
- 健康检查：`http://<地址>:8000/health`

## 最快路径

```bash
unzip cf-gateway-1.0.0.zip && cd cf-gateway-1.0.0
sudo bash install.sh --port 8000
```

脚本会装好 Docker、生成随机管理密码并启动服务，结尾直接打印 New API 要填的参数。

## 手动路径

```bash
cp .env.example .env   # 至少改 ADMIN_PASSWORD、PUBLIC_BASE_URL
docker compose up -d --build
```

## 目录结构

```
├── app/                    服务代码
│   ├── routers/openai.py   对外 OpenAI / New API 兼容接口
│   ├── routers/admin.py    管理后台接口
│   ├── worker.py           任务提交与轮询（进程内后台 worker）
│   ├── capcut_channel.py   CapCut 直连通道 + 官方模型目录
│   ├── cf_client.py        Creative Fabrica MCP 通道
│   ├── r2.py               成片转存 Cloudflare R2
│   └── config.py           配置（支持 .env）
├── static/admin.html       管理后台页面
├── Dockerfile
├── docker-compose.yml
├── install.sh              一键部署脚本（Ubuntu / Debian）
├── requirements.txt
└── .env.example            配置模板
```

## 注意事项

- **单进程运行**：任务轮询在进程内，不要用多 worker
- **`data/` 是唯一需要备份的目录**，含数据库与加密密钥
- **务必修改 `ADMIN_PASSWORD`**：留空会使用内置弱默认密码
- **分辨率/时长/参考素材上限按模型生效**（不是全局一刀切）：默认 ≤15s、480/720p、参考 9/3/3；
  只有 Seedance 2.5 放开到 30s、1080p、参考 30/10/10；每项都可在后台「模型管理 → 编辑」里逐模型改
- **CapCut 请求签名本地现签**：算法已从前端逆向（`md5("9e2c|路径末7字符|pf|appvr|device-time|tdid|11ac")`），
  无需抓包维护常量；`CAPCUT_SIGN_MODE` 可切 auto / mint / static
- **提交被拦（ret=-6 shark block）的开关是反爬参数 `X-Gnarly`**，不是 sign、也不只是 `d_ticket`：
  它在请求 **URL 查询串**上（webmssdk/secsdk 生成），网关默认自动注入（`app/capcut_antibot.py`）。
  实测同一账号只加这一个参数，`ret` 立刻从 `-6` 变 `1000`。详见 DEPLOY.md 9.5 节
- **Cookie 导入认哪些格式**：CapCut 渠道支持插件数组、脚本导出外壳（`cookies` / `playwright_cookies` /
  `cookie_header`，元数据自动忽略）、`k=v;` 头字符串；**必须含 `sessionid`**，且**渠道必须选 CapCut**
  （选成 Creative Fabrica 会按 `creativefabrica.com` 域解析而报错）。详见 DEPLOY.md 9.6 节
