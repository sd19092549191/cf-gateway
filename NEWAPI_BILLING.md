# New API 按秒计费配置（CapCut 视频网关）

> 结论先行：New API 原生没有「视频按秒」维度，只有 **按 token 倍率** 与 **按次固定价**（新版另支持阶梯表达式）。
> 按秒的做法是：**网关把「计费秒数」当成 completion_tokens 上报**，New API 按 token 定价即等价按秒。

## 1. 计费口径（与 CapCut 上游一致）

```
计费秒数 = ceil(输出时长 + Σ参考视频时长)
```

- 参考**图片 / 音频不计费**（上游实测：只有参考视频进公式）。
- 时长会被模型上限夹取（2.0 最大 15s；2.5 最大 30s 且按档吸附，27s → 25s），网关按**生效值**计费。
- 参考视频时长由网关用 HTTP Range 读远程 mp4 的 `mvhd` 得到（moov 常在文件尾，头尾双扫）；读取失败会记 warning 并按 0 计——排障时先看这条日志。

## 2. 网关侧（一处开关）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `USAGE_BILLING_SECONDS` | `0`（关） | 设 `1` 后，生成响应 `usage.completion_tokens = 计费秒数`，并把 `prompt_tokens` 固定为 0 |
| `BILLING_SECONDS_MODE` | `total` | `total`=输出+参考视频（推荐，等价真实成本）；`output`=只算输出时长 |

响应同时回带排查字段（不影响计费）：

```json
{ "task_id": "gen_xxx", "task_status": "polling",
  "billing_seconds": 11,
  "billing": {"billable_seconds": 11, "output_seconds": 5.0,
              "reference_video_seconds": 5.062, "mode": "total"} }
```

## 3. New API 侧：倍率换算

计费公式（按 token 倍率）：

```
quota = (prompt_tokens + completion_tokens × completion_ratio) × model_ratio × group_ratio
1 USD = 500,000 quota        （后台「系统设置」里的货币/兑换率决定人民币显示）
```

网关已把 `prompt_tokens` 设为 0，因此只要把 **completion_ratio = 1**、**group_ratio = 1**（用默认组），就有：

```
quota = 计费秒数 × model_ratio     →     model_ratio 就是「每秒 quota」
```

换算表（按 1 USD = 500,000 quota、汇率 7.3 计）：

| 目标价（每秒） | model_ratio 填 |
|---|---|
| ¥0.01 | 685 |
| ¥0.05 | 3,425 |
| ¥0.10 | 6,850 |
| ¥0.50 | 34,250 |
| ¥1.00 | 68,500 |

公式：`model_ratio = 每秒人民币 ÷ 汇率 × 500000`（汇率取 New API 后台实际配置值；若界面按 USD 显示，直接 `美元价 × 500000`）。

**模型管理 → 找到该模型 → 倍率**里分别给 4 个模型填（可给不同分辨率/模型不同价）：`sd-seedance-2.0` / `sd-seedance-2.0-mini` / `sd-seedance-1.0-fast` / `sd-seedance-2.5`。

## 4. 轮询必须免费：单独给一个 0 价模型名（生产名 `sd-poll`）

客户轮询（`@query <task_id>`）也是一次 API 调用，若用同一个模型名会**被再收一次费**。
网关对轮询请求**不校验模型名**，所以：

1. 渠道的模型列表里加上 `sd-poll`（只是一个名字，网关无需实现它）；
2. New API 里把 `sd-poll` 配成 **固定价 0**（`ModelPrice["sd-poll"] = 0`）；
3. 客户端轮询时用 `model: "sd-poll"`（内容仍是 `@query gen_xxx`）。

⚠️ **模型名不得出现上游品牌**（2026-09-21 运营要求）：所有对外名字一律 `sd-` 前缀，
网关侧也一样（`capcut-seedance-*` → `sd-seedance-*`，见 `db._migrate_rebrand_model_ids`）。

**实测数据（2026-09-21）**：

| 轮询用的模型名 | New API 定价 | 单次轮询成本 |
|---|---|---|
| `sd-seedance-2.0-720p`（收费模型） | `model_ratio=300000` | **+4020 total_usage ≈ ¥40.2** ❌ |
| `sd-poll` | `ModelPrice=0` | **+0** ✅ |

- 根因：网关对 `@query` 上报 `usage={0,0,0}`，但 **New API 会用自己的 tokenizer 重算**
  （实测 22 prompt + 45~67 completion tokens）；而按秒计费下 1 token = 1 秒，
  于是这几十个 token 就变成了几十秒的钱。
- 只配 `ModelPrice` 不够，**还要把 `sd-poll` 加进渠道的模型列表**，否则
  `503 model_not_found: No available channel for model sd-poll`。
- 改渠道用 `PUT /api/channel/` 时**请求体里不能带 `status` 字段**（New API 直接回
  `Invalid parameters`），`relay/_newapi_admin.py` 已处理。 

或者干脆让客户端**长轮询等待**（网关 `CHAT_WAIT_SECONDS=180`），5~15s 的短单一次调用就出片，连轮询都不用。

## 5. 进阶：阶梯单价（可选）

若想「时长越长每秒越便宜」，用 New API 的 **阶梯表达式计费**（`billing_mode = tiered_expr`），
用一个只依赖 `c`（completion tokens = 计费秒数）的表达式，输入 tokens 自然被忽略：

```
c<=5 ? c*4000 : c*3000
```

（表达式输出值 ÷ 1,000,000 × 500,000 × group_ratio = 最终 quota，具体以该版本后台提示为准；配好后必须实测校准。）

## 6. 上线步骤

1. 用新包重建网关镜像，容器加环境变量：`USAGE_BILLING_SECONDS=1`（可选 `BILLING_SECONDS_MODE=total`、`CHAT_WAIT_SECONDS=180`）；
2. New API：按第 3 节填 4 个模型的倍率，按第 4 节加 `sd-poll`（**固定价 0** 并挂进渠道模型列表）；
3. **实测校准**（必做，New API 的预扣/重算行为因版本而异）：
   - 打一单「5s + 1 个 5.06s 参考视频」→ 应计 11 秒 → 检查 New API 日志里该次请求的额度消耗 = `11 × model_ratio`；
   - 再发一次 `@query` 轮询 → 额度消耗应为 0（若不为 0，说明 `sd-poll` 定价或本地 token 计数在起作用，改用 0 固定价重试）。

## 7. 已知注意点

- **非流式最准**：网关对生成请求统一返回非流式 JSON，New API 按响应里的 usage 结算；流式客户端建议只用于轮询。
- **token 被本地兜底计数**：若上游 usage 为 0，New API 会用自带 tokenizer 估算——所以生成响应的 usage 必须是真实秒数（本方案已保证），轮询响应是 0（靠 0 价模型名兜住）。
- **轮询指令必须整条匹配**（`@query`/`@poll`/`@查询`/`@查单` + 任务号），普通 prompt 里出现 `@query` 不会被误判。
