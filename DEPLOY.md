# CF NewAPI Gateway 部署说明

一个把 **CapCut 直连通道**（Seedance 等）和 **Creative Fabrica MCP 通道**封装成
**OpenAI / New API 兼容接口**的网关。部署后可直接在 New API 里作为一个渠道使用。

---

## 目录

- [1. 它能做什么](#1-它能做什么)
- [2. 环境要求](#2-环境要求)
- [3. 快速部署（推荐）](#3-快速部署推荐)
- [4. 手动部署](#4-手动部署)
- [5. 1Panel 部署](#5-1panel-部署)
- [6. 配置项说明](#6-配置项说明)
- [7. 初始化三步](#7-初始化三步)
- [8. 接入 New API](#8-接入-new-api重点)
- [9. R2 存储与「官链 / 官转」](#9-r2-存储与官链--官转)
- [10. 运维](#10-运维)
- [11. API 参考](#11-api-参考)
- [12. 常见问题](#12-常见问题)
- [13. 品牌脱敏（模型名一律 sd-*）](#13-品牌脱敏对外模型名一律-sd--2026-09-21)

---

## 1. 它能做什么

```
                        ┌──────────────── CF NewAPI Gateway ────────────────┐
 New API / 业务系统      │                                                   │
   │  Bearer sk-cf-xxx  │  /v1/videos            创建任务（Sora 风格）        │
   ├──────────────────► │  /v1/videos/{id}       查询状态                    │
   │                    │  /v1/videos/{id}/content  302 → 成片直链           │
   │                    │  /v1/images/generations   图片生成                 │
   │                    │  /v1/chat/completions     通用兼容入口              │
   │                    │                                                   │
   │                    │  账号池调度 + 并发控制 + 积分流水 + 失败重试         │
   │                    └───────┬───────────────────────────┬───────────────┘
                               │                           │
                    CapCut 直连通道                 Creative Fabrica MCP
                  （Seedance 2.5 / 2.0 / Mini …）      （OAuth / Cookie）
                               │                           │
                               └────────► 成片 ────────────┘
                                            │
                              ┌─────────────┴─────────────┐
                              │  官链：返回上游原始 CDN 链接  │
                              │  官转：转存 Cloudflare R2   │
                              └───────────────────────────┘
```

**管理后台**：`http://<你的地址>:8000/admin`

| 模块 | 作用 |
|---|---|
| 账号 | 添加 CapCut Cookie / Creative Fabrica OAuth 账号，查看余额、并发、最近错误 |
| 模型 | 同步 CapCut 官方模型目录（含分辨率/时长/比例/素材上限），逐个启停 |
| API 密钥 | 创建 `sk-cf-xxx`，可限制可用模型、每日限额、选择成片链接方式 |
| 生成记录 | 每个任务的参数、积分实耗、原始返回、成片链接 |
| 日志 | 登录、任务、错误等审计事件 |

---

## 2. 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Linux（Ubuntu 20.04+ / Debian 11+ 已实测）；macOS 亦可手动跑 |
| Docker | 20.10+，含 `docker compose` v2（一键脚本会自动安装） |
| 内存 | ≥ 1 GB（网关本身很轻，主要开销在成片转存时的内存缓冲） |
| 磁盘 | ≥ 5 GB，R2 未配置时成片中转会临时落盘 `data/tmp` |
| 网络 | 需要能访问 `edit-api-sg.capcut.com`、`mcp.creativefabrica.com`；<br>若走 R2 还需能访问 `<account>.r2.cloudflarestorage.com` |
| 端口 | 默认 8000（可用 `GATEWAY_HOST_PORT` 改） |

> ⚠️ **必须单进程运行**。任务轮询是进程内后台 worker，用 `--workers N` 会导致同一任务被多次轮询。
> 本包的 Dockerfile 已固定单进程，自行改命令时请注意。

---

## 3. 快速部署（推荐）

```bash
# 1) 解压
unzip cf-gateway-1.0.0.zip && cd cf-gateway-1.0.0

# 2) 一键部署（自动装 Docker、生成随机密码、构建启动）
sudo bash install.sh --port 8000

# 若服务器已有 Docker，想跳过安装步骤：
sudo bash install.sh --port 8000 --skip-docker-install
```

脚本会做这些事：

1. 检测/安装 Docker 与 Compose
2. 从 `.env.example` 生成 `.env`，写入**随机管理密码**、**随机 SECRET_KEY**、自动探测的 `PUBLIC_BASE_URL`
3. `docker compose up -d --build`
4. 等 `/health` 返回 200，然后打印后台地址、账号密码和 New API 接入参数

结尾会输出类似：

```
 管理后台   : http://1.2.3.4:8000/admin
 用户名     : admin
 密码       : xxxxxxxxxxxxxxxxxxxx
```

**请立刻保存这个密码**，并登录后台确认能进。

---

## 4. 手动部署

```bash
unzip cf-gateway-1.0.0.zip && cd cf-gateway-1.0.0
cp .env.example .env
vim .env          # 至少改 ADMIN_PASSWORD 和 PUBLIC_BASE_URL
docker compose up -d --build
docker compose logs -f
```

验证：

```bash
curl http://127.0.0.1:8000/health
# {"ok":true,"service":"cf-gateway","version":"1.0.0"}
```

改代码后重建：

```bash
docker compose up -d --build
```

---

## 5. 1Panel 部署

1. 把发布包上传到服务器（如 `/opt/cf-gateway-1.0.0`）
2. 1Panel → **容器 → 编排 → 创建编排**
   - 来源选「本地上传 / 路径」，指向包内的 `docker-compose.yml`
   - 目录填 `/opt/cf-gateway-1.0.0`
3. 先在包内 `cp .env.example .env` 并改好 `ADMIN_PASSWORD`、`PUBLIC_BASE_URL`
4. 点「启动」；1Panel 会读取同目录的 `.env`
5. 若 1Panel 的编排界面不支持 `env_file`，把 `.env` 内容逐条粘到编排的「环境变量」里即可（键名完全一致）

> 数据卷是 `./data:/data`，升级时只替换 `app/`、`static/` 并重建容器，`data/` 不要动。

---

## 6. 配置项说明

完整项见 `.env.example`，以下是最常改的：

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_PASSWORD` | 内置默认值 | **必须改**。留空会使用代码内置的弱默认密码 |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | 对外访问地址，用于 OAuth 回调；末尾不要带 `/` |
| `GATEWAY_HOST_PORT` | `8000` | 宿主机映射端口（compose 用） |
| `GATEWAY_PORT` | `8000` | 容器内监听端口，一般不用改 |
| `DATA_DIR` | `/data` | 数据目录，容器内固定，对应 `./data` 卷 |
| `SECRET_KEY` | 自动生成 | 加密账号凭据 + 签发后台 JWT。留空则首次启动生成到 `data/.secret_key`。**多实例共享库时必须显式指定同一个值** |
| `REQUEST_GAP` | `3` | 同一账号两次请求最小间隔（秒）。调小会更容易触发上游风控，不建议低于 2 |
| `POLL_INTERVAL` | `10` | 任务结果轮询间隔（秒） |
| `MAX_RETRIES` | `2` | 任务失败自动重试次数 |
| `R2_*` | 空 | 见 [第 9 节](#9-r2-存储与官链--官转)，**五项必须全填才生效** |
| `CAPCUT_SIGN_MODE` | `auto` | CapCut 请求签名模式：`auto`=账号存过就用/否则现签；`mint`=永远现签；`static`=只用常量。见 [9.4 节](#94-capcut-请求签名sign--device-time与风控) |

---

## 7. 初始化三步

> 全部在管理后台 `http://<地址>:8000/admin` 里操作。

### 第 1 步：添加账号

**CapCut 账号**（当前主力通道）

- 浏览器登录 `www.capcut.com` → 开发者工具 → Network → 复制任意请求的 `Cookie` 请求头
- 后台「账号」→ 添加，类型选 **CapCut 直连**，Cookie 处粘贴
- 保存后网关会自动做一次积分验活，返回余额即可用
- 导入后看一眼账号行的「CapCut 签名 / 风控」列：**`可提交`** = 关键 Cookie（含 `d_ticket`）齐全；
  **`缺 d_ticket`** = 提交生成大概率被 shark 拦（见 9.4 节的处置办法）。
  刚登录就导出的新会话最容易缺这个票据，建议在浏览器里正常用过一次再导

> Cookie 会过期（一般几天到两周）。失效表现为余额查询报错、任务大量失败，
> 重新抓一次 Cookie 在账号页「编辑」里覆盖即可，无需重建账号。

**Creative Fabrica 账号**（备选通道）

- 添加时类型选 **Creative Fabrica**，走 OAuth 授权；若服务器在公网，点「授权」后跳转的
  回调地址必须能被浏览器访问到，因此 `PUBLIC_BASE_URL` 要填对

### 第 2 步：同步并启用模型

后台「模型」→ 点 **同步模型目录**。

- 该操作直接拉取 CapCut 官方实时目录（video / image / audio 三类），
  含每个模型支持的分辨率、时长、画幅、参考素材上限
- **新发现的模型默认是「停用」状态**，需要你确认成本后手动启用
- 想让 New API 看得见，就必须启用
- 列表里的**参考上限**（图/视频/音频）与**生成上限**（时长/分辨率）是网关实际生效值：
  前者默认 9/3/3，后者默认 ≤15s + 480/720p；两者都可在「编辑」里逐模型调整（详见 §8.5）

### 第 3 步：创建 API 密钥

后台「API 密钥」→ 创建：

| 字段 | 说明 |
|---|---|
| 名称 | 便于识别，如 `newapi-prod` |
| 每日请求限额 | `0` = 不限 |
| **CapCut 渠道成片链接方式** | **官链**：返回 CapCut 原始 CDN 链接（带签名，有时效）<br>**官转**：成片转存到你的 R2 桶，返回稳定链接 |
| 允许的模型 | 留空 = 全部；建议按渠道用途限制，便于计费归因 |
| 备注 | 自由填写 |

创建后密钥只显示一次（形如 `sk-cf-xxxxxxxx`），**请立即保存**，服务端只存哈希。

---

## 8. 接入 New API（重点）

### 8.1 先拿到接入参数

| 参数 | 从哪里拿 |
|---|---|
| Base URL | `http://<网关IP或域名>:8000` —— **注意不要带 `/v1`** |
| 密钥 | 后台「API 密钥」创建的 `sk-cf-xxxxxxxx` |
| 模型名 | 后台「模型」页已启用的 `model_id`，或调用 `GET /v1/models` 获取 |

### 8.2 方式 A：Sora 渠道（**推荐**，用于视频模型）

适用于 New API 内置的「Sora」渠道类型 —— 这套路径与 OpenAI 官方一致：

| 动作 | 路径 |
|---|---|
| 创建任务 | `POST /v1/videos` |
| 查询状态 | `GET /v1/videos/{video_id}` |
| 取成片 | `GET /v1/videos/{video_id}/content`（302 跳成片直链） |

**New API 渠道配置：**

| 字段 | 填写 |
|---|---|
| 类型 | `Sora` |
| 渠道名称 | `CF-Gateway` |
| Base URL / 代理地址 | `http://<网关IP>:8000` |
| 密钥 | `sk-cf-xxxxxxxx` |
| 模型 | 填网关的 `model_id`，多个用英文逗号分隔，例如：<br>`sd-seedance-2.0-mini,sd-seedance-2.0` |
| 模型映射（可选） | 若想对用户暴露 `sora-2` 这类名字：<br>`sora-2` → `sd-seedance-2.0-mini` |

创建任务示例（等价于 New API 会发的请求）：

```bash
curl -X POST "http://<网关>:8000/v1/videos" \
  -H "Authorization: Bearer sk-cf-xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sd-seedance-2.0-mini",
    "prompt": "一只橘猫在雨后霓虹街道上奔跑，电影感",
    "seconds": "4",
    "size": "480p 16:9"
  }'
```

返回：

```json
{
  "id": "gen_bd0a27622e230e21",
  "object": "video",
  "model": "sd-seedance-2.0-mini",
  "status": "queued",
  "created_at": 1789464666,
  "seconds": "4",
  "size": "480p 16:9",
  "progress": null,
  "error": null
}
```

轮询 `GET /v1/videos/gen_bd0a27622e230e21`，状态词表：

| 状态 | 含义 |
|---|---|
| `queued` | 已入队，等待调度 |
| `in_progress` | 已提交上游，生成中 |
| `completed` | 完成，响应里带 `url` |
| `failed` | 失败，`error.message` 是原因 |
| `cancelled` | 已在排队阶段被取消 |

生成完成后：

```bash
# 方式一：直接读状态响应里的 url 字段
# 方式二：走 content 接口，302 跳转
curl -L "http://<网关>:8000/v1/videos/gen_bd0a27622e230e21/content" \
  -H "Authorization: Bearer sk-cf-xxxxxxxx" -o out.mp4
```

> `progress` 恒为 `null`：上游不暴露真实进度，这里不编造数据。需要进度条请按任务平均耗时自行估算。

### 8.3 方式 B：OpenAI 渠道 + 通用兼容入口

如果 New API 版本没有 Sora 渠道，用普通 **OpenAI 渠道** 类型，走 `/v1/chat/completions`。
网关会把对话里的最后一条 user 消息当作 prompt，提交生成任务，**最多阻塞等待 55 秒**：

- 55 秒内出片 → 直接把成片链接作为 `content` 返回
- 超时未出片 → 返回 `task_id`，调用方再拿它去查 `GET /v1/generations/{task_id}`

| 字段 | 填写 |
|---|---|
| 类型 | `OpenAI` |
| Base URL / 代理地址 | `http://<网关IP>:8000` |
| 模型 | 同 8.2 |

参数通过 `extra_body` 透传（New API 支持）：

```bash
curl -X POST "http://<网关>:8000/v1/chat/completions" \
  -H "Authorization: Bearer sk-cf-xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sd-seedance-2.0-mini",
    "messages": [{"role": "user", "content": "一只橘猫在雨后霓虹街道上奔跑"}],
    "extra_body": {"seconds": "4", "size": "480p 16:9"}
  }'
```

### 8.4 方式 C：图片模型

图片模型走标准图片接口，New API 里用 OpenAI 渠道即可：

```bash
curl -X POST "http://<网关>:8000/v1/images/generations" \
  -H "Authorization: Bearer sk-cf-xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"model": "capcut-seedream-4.5", "prompt": "赛博朋克城市夜景"}'
```

### 8.5 参数速查

| 参数 | 说明 |
|---|---|
| `prompt` | 必填 |
| `seconds` / `duration` / `duration_seconds` | 时长（秒），三者等价，`seconds` 为 Sora 风格；**上限按模型**（默认 ≤15s，Seedance 2.5 ≤30s） |
| `size` | 如 `480p 16:9`、`1280x720`；**分辨率档按模型**（默认 480/720p，Seedance 2.5 到 1080p） |
| `resolution` | 如 `480p` / `720p` / `1080p`，实际档位按模型上限吸附 |
| `aspect_ratio` | 如 `16:9` / `9:16` / `1:1` |
| `image` / `video` / `audio` / `reference_files` | 参考素材 URL（可多个） |
| `generate_audio` | 是否生成音频 |
| `negative_prompt`、`seed` | 透传给上游（支持度取决于模型） |
| `reference_files` | 参考素材数组 `[{role,url}]`，role 为 `referenceImage`/`referenceVideo`/`referenceAudio`；也可用 `image`/`video`/`audio` 简写传 URL 数组 |

> **参考素材上限按模型生效**（不是全局一刀切）：
> 优先级 后台逐模型覆写 > **内置实测覆写** > 官方目录 `gen_limits` > 内置默认 图9/视频3/音频3，总数默认自动等于三类之和。
> 例：**Seedance 2.5 = 30 参考图 / 10 参考视频 / 10 参考音频**（官方接口对本账号返回的是降级值 9/3/3，已按实测能力覆写）。
> 后台「模型管理」列表新增**参考上限**列，标「实测」即覆写值；点编辑可逐模型调整，清空则回落到目录/默认。
> 超限会在**提交前**返回 400（如 `参考image最多 30 个（当前 31）`），不会排队后才失败。
>
> **开箱即用**：**Seedance 2.5 随首次启动自动出现在模型列表**（内置种子，`/v1/models` 立即可见，
> 对外 id 为 `sd-seedance-2.5`，**注意是下划线**，与 2.0 系的连字符不同）。
> 无需先加账号跑「目录同步」——上限由内置实测覆写解析，不依赖官方接口返回值。
> 此外 `sd-seedance-2.0-mini` / `sd-seedance-2.0` / `sd-seedance-1.0-fast` 也会一并播种。
>
> **为什么 2.5 能到 30/10/10**：参考素材走的是 CapCut 的 `omni_reference`（内部叫 `r2v`，即
> `material_gen_video_by_seedance25_r2v_*`）生成模式 —— 只有「开启参考素材」这条路径才放开大数量，
> 普通图生视频（i2v / 首尾帧）仍是 1 张。网关在 `reference_files` 非空时自动切 `omni_reference`，
> 所以上限只在真正带参考素材时才有意义。
>
> **大数量参考素材的上传并发**：网关会先把你给的 URL 下载下来再上传到 CapCut（imageX / VOD），
> 这一阶段是**有界并发**（默认 4，`CAPCUT_REF_UPLOAD_WORKERS` 可调 1–8），且严格保持入参顺序
> （提示词里的 `[image1]…[imageN]` 按下标对齐）。实测 12 张图：并发 8.1s vs 串行 21.9s。
> 并发整体失败会自动退回串行重试一次。

> **分辨率与时长上限同样按模型生效**（`gen_limits`，不是全局一刀切）：
>
> | 优先级 | 来源 | 说明 |
> |---|---|---|
> | 1 | 后台逐模型设置 | 「模型管理 → 编辑 → 生成能力上限」，留空即删除覆写 |
> | 2 | 内置实测覆写 | 目前只有 **Seedance 2.5**：`480p/720p/1080p` + `5/8/10/12/15/18/20/25/30` 秒 |
> | 3 | 内置默认 | 其余 CapCut 视频模型：`480p/720p` + 2–15 秒（连续取值，无档位） |
>
> 例：`sd-seedance-2.0` 请求 `1080p` / `30s` 会被夹取成 `720p` / `15s`（并在日志里 WARNING 留痕）；
> `sd-seedance-2.5` 同样是 `1080p` / `30s` 则原样生效。**只有 2.5 放开了 30s 与 1080p，其余模型维持原行为。**
> 时长若模型给了档位表（如 2.5），请求值会**吸附到最近档位**（如 6s → 5s，7s → 8s）。
>
> 调用方可以在 `GET /v1/models` 里读到每个 CapCut 模型实际生效的上限，无需试错：
>
> ```json
> {"id":"sd-seedance-2.5","object":"model","owned_by":"sd",
>  "capcut_limits":{"resolutions":["480p","720p","1080p"],
>                   "durations":[5,8,10,12,15,18,20,25,30],
>                   "min_duration":2,"max_duration":30,
>                   "max_reference":{"image":30,"video":10,"audio":10,"total":50}}}
> ```
>
> 后台「模型管理」列表有**生成上限**列（含来源标记：实测覆写 / 后台设置 / 内置默认）；
> 编辑框里可一键「按官方目录填入」—— 官方目录声明的分辨率/时长会显示在同一行，方便对照后决定放开多少。
>
> ⚠️ 已知限制：1080p 与「长时长」在 CapCut 侧挂在付费权益上（抓包可见
> `ai_benefits_ultrahd`、`ai_benefits_longevityduration`，`right_subscribe_type=svip`）。
> 网关**放开**只是不再本地拦截，上游是否真正接受取决于该账号的订阅等级；被上游拒绝时会走既有
> 失败重试/换号逻辑，报错原文可在任务详情里看到。

> **成片规格只由结构化参数决定，prompt 文本里不需要（也不会）被解析成规格。**
> 画幅优先级：显式 `aspect_ratio` / `ratio` > `size` 内嵌比例（`480p 16:9`、`1280x720`、`864x496`）> 模型模板默认值。
> prompt 请只用来描述画面内容。
>
> 实测依据：同一模型（模板默认 9:16）下，把「480p 分辨率，画幅 16:9 横屏（约 864x496）」写进 prompt 文本、
> 但结构化参数缺失时，成片仍是 496x864 竖屏；反之 4 条只传结构化参数、prompt 完全没提分辨率的任务，
> 成片全部为 864x496。**只写 prompt 是被忽略的**，别再依赖这种写法。

### 8.6 接入后自检

```bash
GW=http://<网关>:8000
KEY=sk-cf-xxxxxxxx

curl -s $GW/health
curl -s $GW/v1/models -H "Authorization: Bearer $KEY"
curl -s -X POST $GW/v1/videos -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"sd-seedance-2.0-mini","prompt":"测试","seconds":"4","size":"480p 16:9"}'
```

---

## 9. R2 存储与「官链 / 官转」

### 9.1 配置

在 `.env` 里填齐这五项，重启生效：

```env
R2_ENDPOINT=https://<account_id>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=your-bucket
R2_PUBLIC_BASE=https://your-domain.com   # 桶的公开访问域名
R2_KEY_PREFIX=cf-gateway/
```

`R2_PUBLIC_BASE` 必须是**公网可直接 GET 到对象**的地址：自定义域名（推荐）或 `https://pub-xxx.r2.dev`。

验证是否生效：后台 **总览** 页的「CapCut 成片存储」卡片会显示 `R2 已就绪` 和桶名。

### 9.2 两种链接方式

在**密钥**级别选择，只影响 CapCut 渠道：

| 模式 | 行为 |
|---|---|
| **官链**（默认） | 不改动链接，直接返回 CapCut CDN 原始地址。**带签名、有时效**（通常数小时），过期后不可访问 |
| **官转** | 成片下载后转存到你的 R2 桶，返回 R2 稳定链接；原始链接保留在结果 JSON 的 `originUrl` 字段 |

R2 未配置完整、或转存失败时，**官转会静默回退为官链**（不阻断出片），
结果里会带 `linkNote` 说明原因，后台日志有 warning。

> 走 **Creative Fabrica 通道**的出片**不带开关**：只要 R2 配置齐就会自动转存并对外返回 R2 链接（不暴露上游源站）。
> 这也是为什么配置 R2 后，CF 通道会全量走 R2。

### 9.3 参考素材上传

若需要让调用方上传本地图片/视频/音频作为参考素材，网关提供：

```
POST /v1/files        multipart/form-data，字段名 file，一次最多 50 个（图 30 / 视频 10 / 音频 10）
```

文件会先传到 R2，返回 **24 小时有效的签名 URL**，再把这个 URL 传进 `reference_files` 使用。
**此功能依赖 R2**，未配置时该接口返回 503。
上传接口的 30/10/10 是「批量闸门」；**该次生成真正允许多少，仍按所选模型的上限**在提交时校验
（例如 Seedance 2.5 就是 30/10/10，Seedance 2.0 Mini 仍是 9/3/3）。

> **R2 只是中转，不是最终存储**：提交时网关把素材**下载回来重新上传到 CapCut 自家的 imageX/VOD**，
> 上游任务引用的是 CapCut 内部 URI，CapCut 侧看不到你的桶和签名 URL。
> **自动清理**：`ref/` 前缀的对象默认 **48 小时**后由 worker 每 6 小时清理一批
> （`R2_REF_RETENTION_HOURS` 可调，`0` = 关闭自动清理）——中转用完即弃，避免桶内堆积。

---

## 9.4 CapCut 请求签名（sign / device-time）与风控

### 签名算法（已逆向，本地现签）

2026-09-15 从 CapCut 前端 bundle（`editor.<hash>.js`）逆向出 `sign` 的生成算法，
并用 HAR 里的 **87 条真实样本 100% 复现**：

```
sign = md5("9e2c|" + 路径末7字符 + "|" + pf + "|" + appvr + "|" + device-time + "|" + tdid + "|11ac")
device-time = 当前 unix 秒
```

- 输入只有「请求路径 + 客户端版本 + 时间戳」，**不含任何账号密钥** → 不需要抓包，网关**按当前时间现签**
- `pf` / `appvr` 必须与实际发出的请求头逐字符一致（网关默认 `pf=7 / appvr=8.4.0`）
- `upload_sign` 系列的 `tdid` 参与签名（历史值 `178931314028893759`）

模式由 `CAPCUT_SIGN_MODE` 控制（默认 `auto`）：

| 模式 | 行为 |
|---|---|
| `auto` | 后台给账号存过签名就用它，否则按当前时间现签 |
| `mint` | 永远现签（推荐，device-time 永远是新的） |
| `static` | 只用后台/内置常量（排查用） |

### 提交被风控拦截（ret=-6 "shark block only"）怎么办

实测结论：`common_task/new`（提交生成）被 shark 拦截**与 sign 无关**（现签也一样被拦），
与**账号会话信任**有关。关键差异是 Cookie 里的 `d_ticket`（风控票据）：
有 `d_ticket` 的账号能过，刚登录、没有该票据的新会话会被拦。`query` / `chat_upload_sign` /
`upload_sign` / `user_credit` 宽松，不受影响。

处置：

1. **账号管理 → 编辑 → CapCut 风控体检**：一键查看该账号缺哪些关键 Cookie
   （`d_ticket` 缺失会标红）
2. 在**该账号**的浏览器里正常使用一次（通过人机校验），然后：
   - 重新导出整份 Cookie 粘贴进「更新 Cookie」；或
   - 只复制 `d_ticket` 一个值，用编辑弹窗里的 **补单个 Cookie** 填 `d_ticket=xxx`
3. 也可以在弹窗里粘贴 request header / HAR 片段，点 **提取并保存** 自动写入该账号的专属签名
   （支持整份 HAR、curl、纯 header 块；自动识别接口并做本地复算校验）

相关后台接口：

```
GET    /admin/api/accounts/{id}/cookie-health     # Cookie 风控体检
GET    /admin/api/accounts/{id}/signs             # 签名现状（内置/账号专属/现签 + 复算校验）
PUT    /admin/api/accounts/{id}/signs             # 写入签名（支持 {"text": "..."} 自动识别）
DELETE /admin/api/accounts/{id}/signs             # 清空 → 回到按算法现签
POST   /admin/api/accounts/{id}/signs/extract     # 从 HAR / header 片段提取
POST   /admin/api/accounts/{id}/signs/mint        # 按算法现签一套
PATCH  /admin/api/accounts/{id}                   # cookie_patch: {"d_ticket": "..."} 补单个 Cookie
```

命令行也可以直接从 HAR 提取：`python -m app.capcut_signs <har 文件> [默认接口键]`

### 9.5 CapCut 风控第一道门：`X-Gnarly` 反爬参数（2026-09-15 实测）

`POST /lv/v1/common_task/new` 被 `ret=-6 shark block only` 拦住时，**先看这个**。
CapCut 网页端每次调 edit-api 都会在 **URL 查询串**（注意：不是请求头！）上带三个参数，
由前端 `webmssdk` + `secsdk_runtime_bundler` 生成：

```
...&region=PK&web_id=7685...&msToken=<116字符>&X-Bogus=<28字符>&X-Gnarly=<88字符>
```

其中 **`X-Gnarly` 是 shark 放行的开关**，实测（同一账号、同一秒、只改这一个参数）：

| 查询串里带的 `X-Gnarly` | 结果 |
|---|---|
| 不带 | `ret=-6 shark block only` |
| `Mx`（1 字符）/ `MxEcb3`（6 字符）/ 随机 28 字符 | `-6` |
| `MxEc`（4 字符）/ `MxEcb3Ov`（8 字符） | `ret=1000` 放行 |
| `MxEcb3Ovna5uDKefmxB5hffNmh15`（28 字符，内置值） | `ret=1000` 放行（反复稳定） |
| HAR 里那个**完整 88 字符**原值 | `-6` ← 反直觉，但实测如此，所以内置值只取前 28 字符 |

`X-Bogus` **不是必需**：只带它不带 `X-Gnarly` 仍被拦；只带 `X-Gnarly` 即可放行。

> **这条推翻了两个旧结论**：① 拦你的是反爬参数，**不是 sign**；② 也**不只是** `d_ticket`
> —— 账号 #3 带着 `d_ticket` 照样被拦，加上 `X-Gnarly` 后同一个请求立刻 `ret=1000`。

代码在 `app/capcut_antibot.py`，网关会自动给所有 CapCut edit-api 请求（含参考素材上传）注入：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `CAPCUT_ANTIBOT` | `on` | 设 `off` 关闭注入（排查用） |
| `CAPCUT_XGNARLY` | 内置 28 字符常量 | 失效时换成新抓的值 |
| `CAPCUT_XBOGUS` | 内置常量 | 非必需，带上更接近浏览器 |

**失效了怎么换新值**（会随设备/会话变化）：浏览器登录 capcut.com → DevTools Network
→ 随便触发一次 edit-api 请求（编辑器里点一次生成最省事）→ Copy as cURL → 存成文本：

```bash
python -m app.capcut_antibot <har 或 cURL 文本>   # 打印 X-Gnarly / X-Bogus
# 然后写进 .env：
CAPCUT_XGNARLY=<打印出来的值>
```

### 9.6 CapCut Cookie 导入：认哪些格式 / 为什么会被拒

网关按**账号渠道**分流解析：渠道选 `capcut` → CapCut 解析器；选 Creative Fabrica → CF 解析器
（只认 `creativefabrica.com` 域的 Cookie）。**渠道选错是最常见的「不认」原因**——把 CapCut 的
Cookie 按 CF 规则解析，会直接报 `未找到 creativefabrica.com 的有效 Cookie`。

CapCut 侧支持以下写法（自动跳过元数据外壳，优先采用含 `sessionid` 的那一份）：

| 形态 | 示例 |
|---|---|
| 插件导出数组 | `[{"name":"sessionid","value":"…","domain":".capcut.com"}, …]` |
| 脚本导出外壳 | `{"account":"a@b.com","exported_at":…,"cookies":{"sessionid":"…"},"playwright_cookies":[…],"cookie_header":"sessionid=…; …"}` |
| `{"cookies":[...]}` 包装 | `{"cookies":[{"name":"sessionid",…}]}` |
| 纯键值对象 | `{"sessionid":"…","uid_tt":"…"}` |
| 头字符串 | `sessionid=…; uid_tt=…; ttwid=…` |

硬性要求：**必须含 `sessionid`**。被拒时接口会回显「本次解析到的 cookie 名」以便定位——
若回显 `account, exported_at, cookies, playwright_cookies …`，说明是外部外壳没被识别
（2026-09-15 已修复）；若回显 `ttwid, uid_tt` 之类，说明这份导出根本没有登录态，
需要重新登录后导出。文件带 BOM（Windows 记事本「UTF-8 带 BOM」）也已兼容。

导入结果分四类，管理端弹窗与 `batch-import` 响应都会列出：

| 分类 | 含义 |
|---|---|
| `created` | 成功入库（`ok=true` 表示验活通过，`detail` 里带积分余额） |
| `dead` | 格式对但验活失败；勾选「验活失败的账号不保留」时会被丢弃 |
| `skipped` | 与已有账号同身份（按 `uid_tt` → `sessionid` 去重） |
| `invalid` | 格式校验就没过（缺 `sessionid` / JSON 坏了 / 空内容） |

命令行等价写法（`$TOKEN` 来自 `POST /admin/api/login`）：

```bash
curl -X POST http://127.0.0.1:8001/admin/api/accounts/batch-import \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"provider":"capcut","probe":true,"keep_dead":true,
       "accounts":[{"name":"acc1","cookie":"<cookie 文件原文>"}]}'
```

### `d_ticket` 从哪来

它**不是客户端算出来的**，而是 passport 登录接口
`POST https://login-row.www.capcut.com/passport/web/email/login/` 的**响应**里下发的风控票据
（响应头 `d-ticket` / `d-ticket-sec-uid` / `d_ticket`，伴随 `tt-ticket-guard-result: 0`）。
服务端依据「账号 uid + 设备指纹（`did` / `uifid` / `verifyFp`）+ 本次登录的风险评分」签发，
客户端只能接收并回传。因此：

- **每次正常登录后导出的整份 Cookie 才会带 `d_ticket`**；从 HAR 里手工拼 Cookie 通常会漏掉它
- 它是会话级票据，会过期 → 被 shark 拦（`ret=-6`）时按 9.4 的流程重新登录导出或单补一个
- 前端 bundle 里搜不到任何 `d_ticket` 字样，也就**没有可逆向的本地生成算法**

---

## 10. 运维

### 日志

```bash
docker compose logs -f --tail 200
```

### 数据备份

**只需要备份 `data/` 目录**，里面有：

| 文件 | 内容 |
|---|---|
| `cf_gateway.db` | 账号、密钥、模型配置、生成记录、积分流水 |
| `.secret_key` | 加密密钥（**丢了会导致已存账号凭据无法解密**） |
| `tmp/` | 上传中转的临时文件，可随时清 |

```bash
# 停服冷备（推荐，保证 SQLite WAL 落盘）
docker compose stop
tar czf cf-gateway-backup-$(date +%F).tar.gz data/
docker compose start
```

### 升级

```bash
# 1) 备份 data/
# 2) 用新版本的 app/ static/ Dockerfile requirements.txt 覆盖
# 3) 重建
docker compose up -d --build
```

数据库结构变更由网关启动时自动迁移（`ALTER TABLE ADD COLUMN`），老库可直接沿用。

### 排障速查

| 现象 | 排查方向 |
|---|---|
| `/health` 不通 | `docker compose logs`；多半是端口占用或 `.env` 语法错误 |
| 登录报用户名或密码错误 | 确认 `.env` 的 `ADMIN_PASSWORD`；改完要 `docker compose up -d` 重建容器才会重新读取 |
| 创建任务返回 404 model_not_found | 模型没启用，或该密钥的「允许的模型」里没包含它 |
| 创建任务返回 403 permission_denied | 密钥被停用，或模型不在该密钥白名单内 |
| 任务一直 `in_progress` 不结束 | 看生成记录里的上游状态；多半是账号 Cookie 失效或余额不足 |
| 任务失败「积分不足」 | 后台「账号」页看余额，补号或换号 |
| 官转没生效 | 总览页看 R2 是否为「未配置」；密钥的链接方式是否为「官转」 |
| New API 报 404 | Base URL 加了 `/v1` 导致路径重复拼接，去掉 `/v1` 再试 |
| **`/health` 正常、`/v1/models` 正常，但上传参考素材报 `Failed to connect to proxy URL: http://127.0.0.1:xxxxx`** | 进程继承了**会话级**代理变量（在带代理的终端里启动过、代理已失效）。程序默认已忽略这类变量；若你确实需要代理，请显式配置 `RELAY_OUTBOUND_PROXY=http://host:port` 并重启 |

> **出网代理说明**：网关默认**直连出网**，会主动忽略从启动它的 shell 继承来的
> `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`。这样做是因为这类变量常来自 IDE 终端/临时会话，
> 会话结束后代理消失，而常驻进程仍拿着旧值 → 表现为「服务在跑但上传、生成全挂」，很难排查。
> 需要走代理请设 `RELAY_OUTBOUND_PROXY`；调试时若要保留父进程原样，设 `RELAY_KEEP_PROXY_ENV=1`。
> **自检**：`curl -X POST .../v1/files -F "file=@某图.png"` 能返回签名 URL 就说明出网正常。

---

## 11. API 参考

所有接口用 `Authorization: Bearer sk-cf-xxxxxxxx` 鉴权。
错误统一为 OpenAI 格式：`{"error":{"message":..., "type":..., "code":...}}`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查（无需鉴权） |
| GET | `/v1/models` | 可用模型列表 |
| POST | `/v1/videos` | 创建视频任务（Sora 风格） |
| GET | `/v1/videos/{id}` | 查询视频任务 |
| GET | `/v1/videos/{id}/content` | 302 跳成片直链 |
| POST | `/v1/video/generations` | 同上创建（单数别名，兼容部分聚合器） |
| GET | `/v1/video/generations/{id}` | 同上查询（单数别名） |
| POST | `/v1/videos/generations` | 创建视频任务（复数风格，返回 `object: generation`） |
| GET | `/v1/videos/generations/{id}` | 查询 |
| POST | `/v1/images/generations` | 创建图片任务 |
| GET | `/v1/images/generations/{id}` | 查询 |
| POST | `/v1/generations` | 通用创建（不限类型） |
| GET | `/v1/generations/{id}` | 通用查询（含 `result`、`link_mode`） |
| DELETE | `/v1/generations/{id}` | 取消（仅 `queued` 可取消） |
| POST | `/v1/chat/completions` | 通用兼容入口，阻塞最多 55 秒 |
| POST | `/v1/files` | 上传参考素材到 R2（需配置 R2） |

错误码：

| 状态码 | `type` | 含义 |
|---|---|---|
| 400 | `invalid_request_error` | 参数错误 |
| 401 | `authentication_error` | 密钥缺失或无效 |
| 402 | `insufficient_balance` | 账号额度不足 |
| 403 | `permission_denied` | 密钥停用 / 模型不在白名单 |
| 404 | `not_found_error` | 任务或模型不存在 |
| 429 | `rate_limit_error` | 达到密钥每日限额 |
| 500 | `upstream_error` | 上游错误 |
| 503 | `service_unavailable` | 依赖未就绪（如未配置 R2） |

---

## 12. 常见问题

**Q：能开多个实例做高可用吗？**
不建议。任务轮询是进程内 worker，多实例会重复轮询同一任务。确实需要扩容时，
让每个实例使用**独立的数据目录**，然后在 New API 侧对两个渠道做负载均衡（不要共享数据库）。

**Q：`sk-cf-` 密钥丢了能找回吗？**
不能。服务端只存哈希，只能删除后重建。

**Q：成片链接会过期吗？**
- **官链**：会，CapCut CDN 链接带签名和时效（通常数小时）
- **官转**：不会，只要 R2 对象还在（R2 不自动过期，需要的话自己在 Cloudflare 侧配生命周期规则）
- **CF 通道 + R2 已配置**：不会

所以要长期保存，请用「官转」或自行转存。

**Q：能限制某个密钥只能用某几个模型吗？**
能。创建/编辑密钥时在「允许的模型」里填 `model_id`，逗号分隔；留空表示全部。

**Q：怎么控制成本？**
1. 后台「模型」页只启用真正要用的模型，其他保持停用
2. 每个模型的 `estimated_cost` 可在模型编辑里改（CapCut 官方目录不暴露单价，默认是 0）
3. 生成记录页能看到每个任务的**积分实耗**（按账号余额差值计算，是准确的）
4. 用密钥的「每日请求限额」做硬性上限

**Q：CapCut 账号 Cookie 多久失效？**
没有固定周期，通常几天到两周。建议多备几个账号轮换（账号页支持设置并发），
失效时批量替换 Cookie 即可，历史任务记录不受影响。

**Q：客户轮询用哪个模型名？**
用 **`sd-poll`**（0 价）。**绝不能用收费模型名轮询** —— New API 会把响应里那几十个
token 当成「秒」再收一次费（实测 ≈¥40/次）。见 `NEWAPI_BILLING.md` 第 4 节。

## 13. 品牌脱敏：对外模型名一律 `sd-*`（2026-09-21）

**要求**：客户端可见的一切模型名不得出现上游品牌。改动覆盖三块：

| 位置 | 改前 | 改后 |
|---|---|---|
| 网关模型目录 | `capcut-seedance-2.0` / `capcut-seedance_2.5` | `sd-seedance-2.0` / `sd-seedance-2.5` |
| `/v1/models` 的 `owned_by` | `capcut` | `sd` |
| `/v1/models` 的能力字段 | `capcut_limits` | `sd_limits` |
| New API 轮询模型 | `capcut-poll` | `sd-poll` |
| New API 渠道「模型映射」 | `sd-seedance-2.0-720p → capcut-seedance-2.0` | **清空**（由网关别名解析强制分辨率） |

**老库升级**：启动时自动改名（`db._migrate_rebrand_model_ids`，幂等），覆盖
`models.model_id`、`generations.model_id`、`api_keys.enabled_models_text`（Key 白名单）。
⚠️ 白名单那处漏改会让该 Key「一个模型都没有」。

**不停机热更（已部署实例）**：

```bash
set -e
REPO=sd19092549191/cf-gateway
for f in app/db.py app/capcut_channel.py app/routers/openai.py; do
  curl -fsSL "https://raw.githubusercontent.com/$REPO/main/$f" -o "/tmp/$(basename $f)"
  docker cp "/tmp/$(basename $f)" capcut2:/app/$f
done
docker restart capcut2      # ⚠️ docker cp 后必须 restart 才生效
sleep 8
docker exec capcut2 python -c "import sqlite3;print([r[0] for r in sqlite3.connect('/app/relay_data/cf_gateway.db').execute('select model_id from models')])"
```

**自检（零成本）**：

```bash
curl -s http://127.0.0.1:8000/v1/models -H "Authorization: Bearer <你的网关Key>" \
  | grep -i capcut && echo "❌ 还有残留" || echo "✅ 已脱敏"
```

`relay/_check_model_names.py [base_url]` 会一次跑完（遍历 `_test_state.json` 里所有 Key，
连 `owned_by` / 字段名一起查）。
