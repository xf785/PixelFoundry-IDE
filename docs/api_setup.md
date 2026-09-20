# API 接入与中转站配置指南

本文面向「设置 → API 配置」页，讲清楚三类 API（通用文本 / 图片生成 / 图转视频）怎么填、
**中转站（relay / 聚合站 / one-api / new-api 之类）** 的特殊之处怎么处理，
以及 401/403/404/超时/返回结构不符时怎么快速定位。

> 所有能力都在应用内完成，**不依赖任何第三方服务**：预览请求、从 curl 导入、
> 测试并检测字段都是本地解析 + 一次真实请求。

---

## 一、配置总览（三类 API）

| 类型 | 用途 | 默认端点 | 必填 |
| --- | --- | --- | --- |
| 通用文本 API（llm） | 生成图片/动画提示词 | `{Base URL}/chat/completions` | Base URL、API Key、模型名称 |
| 图片生成 API（image） | 文生图 / 图生图 | `{Base URL}/images/generations` | Base URL、API Key、模型名称 |
| 图转视频 API（video） | 首帧图 + 提示词 → 视频 | 提交 `{Base URL}/videos/generations`，轮询 `…/{id}` | Base URL、API Key、模型名称 |

三类共用同一批「高级选项」：端点路径、超时、代理、SSL 校验、**鉴权方式**、
额外请求头、请求体模板（JSON）、完全自定义模式。

**填配置的正确顺序**：① 选服务商预设 → ② 填 API Key → ③ 点「保存配置」→
④ 点「测试连接」确认可达 → ⑤ 用「预览请求…」核对请求 → ⑥ 用「测试并检测字段…」
把返回结构里的字段路径一键填好 → ⑦ 保存。

---

## 二、鉴权方式选择表

中转站最常把人卡住的地方：**Key 是对的，但放错了地方**。三类 API 都有「鉴权方式」下拉：

| 选项 | 实际发出的内容 | 什么时候用 |
| --- | --- | --- |
| `Bearer 令牌` | `Authorization: Bearer <Key>` | 默认；OpenAI、DeepSeek、方舟、gpt.ge 等官方接口与多数中转站 |
| `X-API-Key 请求头` | `X-API-Key: <Key>` | 大量中转站/自建网关（one-api、new-api 常见） |
| `api-key 请求头` | `api-key: <Key>` | 部分网关（大小写敏感，注意是小写 `api-key`） |
| `URL 查询参数` | `…?key=<Key>`（参数名可改） | 少数站点把 Key 放查询串；参数名填 `key` / `api_key` / `api-key` |
| `自定义请求头` | `<头名>: <前缀><Key>` | 站点要求 `X-Token: Token xxx` 这类自定义形式 |
| `不鉴权` | 不带任何鉴权头 | 本地 Ollama、内网服务 |

要点：

- **额外请求头优先级最高**：`高级选项 → 额外请求头(JSON)` 里写的头会覆盖上面任何一种方式。
  临时试错时可以直接写 `{"X-API-Key": "sk-xxx"}`，不必改鉴权方式。
- 直连可灵（Kling）官方 API 的 Key 是 **JWT**（形如 `eyJ…`），默认 Bearer 就是对的；
  经中转站转发时可灵的 Key 通常换了格式，这时要按**中转站**的要求改「鉴权方式」。
- 401/403 的错误信息里会直接提示这一点（见第六节）。

---

## 三、Base URL 与端点

- **Base URL**：填到「版本前缀」为止，例如 `https://api.openai.com/v1`、
  `https://ark.cn-beijing.volces.com/api/v3`。**不要**把具体接口名写进去。
- **端点路径(可选)**：Base URL 之外的额外路径。常见坑：Base URL 已带 `/api/v3` 时
  还填 `/api/v3/images/generations`，最终会拼成 `…/api/v3/api/v3/images/…`（404）。
- **完整 URL 覆盖**：图片/文本 API 支持用 `url` 参数（在「完全自定义」场景）直接覆盖整条地址。
- 视频 API 的端点用 **`{base}` / `{id}` 占位符**：
  - 提交端点：`{base}/v1/videos/image2video`
  - 轮询端点：`{base}/v1/videos/image2video/{id}`（`{id}` = 提交返回的任务 ID）
- 「查询模型」会自动兼容两种写法：Base URL 带 `/v1` 时请求 `{base}/models`，
  不带时请求 `{base}/v1/models`。

---

## 四、中转站常见形态

### 形态 A：OpenAI 兼容（最常见）

端点与 OpenAI 一致，只是换了域名和鉴权：

```
Base URL : https://relay.example.com/v1
鉴权方式 : X-API-Key（或 api-key / 查询参数，看站点文档）
模型名称 : 站点列表里的名字，如 sora-2 / doubao-seedance-1-5-pro
```

预设「中转站：OpenAI 风格 /v1/videos」就是这一形态（提交 `{base}/videos`，轮询 `{base}/videos/{id}`，
结果字段 `data.0.url`）。

### 形态 B：提交 + 轮询（两步任务）

提交拿任务 ID → 轮询查状态 → 取结果 URL。四个字段决定成败：

| 设置项 | 含义 | 例子 |
| --- | --- | --- |
| 任务ID字段路径 | 提交响应里任务 ID 的位置 | `id`、`data.task_id`、`request_id` |
| 状态字段路径 | 轮询响应里状态的位置 | `status`、`data.task_status`、`state` |
| 成功状态(逗号分隔) | 视为完成的状态值 | `succeeded,success,completed,done` |
| 视频URL字段路径 | 结果视频地址的位置 | `data.0.url`、`output.video_url`、`videos.0.url` |

预设「中转站：提交+轮询（通用模板）」已填好一套通用值。
**即使填错也不致命**：取不到时会自动依次尝试一批常见候选路径
（任务 ID：`data.task_id`、`data.id`、`task_id`、`request_id`…；
视频 URL：`data.video_url`、`output.url`、`videos.0.url`、`content.video_url`…），
日志里能看到实际命中的路径。

### 形态 C：火山方舟 / 豆包 content 数组

方舟 Seedance 的请求体不是 `prompt` 平铺，而是 `content` 数组：

```json
{
  "model": "doubao-seedance-1-0-pro-250528",
  "content": [
    {"type": "text", "text": "提示词"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
  ],
  "duration": 5
}
```

选「服务商适配 = Doubao Seedance（火山方舟）」即可，端点与结果解析（`content.video_url`）
都已内置；`gpt.ge (V-API) 豆包视频` 用同一套请求体，但端点是 `{base}/task/volces/seedance`
（注意：**不加 `/v1`**）。

想自己拼请求体时，用「请求体模板(JSON, 可选)」+ 占位符最灵活。

---

## 五、从 curl 导入（推荐的中转站起手式）

不用手抄端点、请求头、请求体 —— 直接用浏览器里**真实的**那条请求：

1. 在该中转站的网页/客户端里正常操作一次（或看站点文档里的示例请求）；
2. 按 `F12` 打开开发者工具 → **网络（Network）** 标签；
3. 找到目标请求 → 右键 → **复制 → 以 cURL 格式复制（Copy as cURL）**；
4. 回到应用：设置 → 对应 API 类型 → **「从 curl 导入…」** → 粘贴 → 「解析并填入」。

导入器会自动：

- 解析多行命令（含行尾 `\`、单/双引号、`--url`、`-X`、`-H`、`-d/--data-raw/--data-binary/--json`）；
- 填好 **Base URL / 提交端点 / 请求方法 / 额外请求头 / 请求体模板**；
- 把请求体里认得出的字段换成占位符：`prompt`→`$prompt`、`image`→`$image`
  （值是 `http(s)` 链接时改用 `$image_url`）、`model`→`$model`、`duration`→`$duration`、
  `frames`→`$frames`、`fps`→`$fps`、`seed`→`$seed`、`ratio`→`$ratio`、
  `resolution`/`size`→`$resolution`、`mode`→`$mode`、`negative_prompt`→`$negative_prompt`、
  `last_frame`→`$last_image`（大小写不敏感，嵌套一层也认）。

导入**不做**的事（会弹「注意事项」提示）：

- 不把请求头里的 Key 写进「API Key」字段（避免明文散落）：请自己把 Key 填进 API Key，
  再把额外请求头里的那份删掉；
- 请求体不是 JSON（表单/纯文本）时不生成模板；
- 轮询端点、任务ID/状态/视频URL 字段路径仍需用下一节的「测试并检测字段…」确定。

---

## 六、预览请求 / 测试并检测字段

视频 API 配置页多了三个按钮（图片/文本 API 有「从 curl 导入…」）：

| 按钮 | 做什么 | 会不会发请求 |
| --- | --- | --- |
| **预览请求…** | 展示将要发出的「方法 + URL + 请求头 + JSON 请求体」（Key 已打码为 `***`，可一键复制） | **不发**，纯本地组装 |
| **测试并检测字段…** | 用一张程序合成的 256×256 测试图**真发一次提交请求**（不轮询），显示 HTTP 状态与原始响应，并列出识别到的字段路径 | 发 1 次 |
| **从 curl 导入…** | 粘贴 cURL 命令自动填配置（见第五节） | 不发 |

「测试并检测字段…」的结果区下方会给出候选路径，形如：

```
[任务ID字段路径]  data.task_id      abc123              [使用]
[状态字段路径]    data.status       running             [使用]
[视频URL字段路径] data.video.url    https://…/v.mp4     [使用]
```

点「使用」即写进对应设置项（并立即生效于当前客户端），最后**点「保存配置」持久化**。
一条候选都没有时，展开原始响应手工填路径即可。

---

## 七、请求体模板与占位符（视频）

「请求体模板(JSON, 可选)」用 `$` 占位符（避免与 JSON 花括号冲突），可用：

| 占位符 | 值 |
| --- | --- |
| `$model` | 配置里的模型名称 |
| `$prompt` | 本次提示词（引号/换行会自动转义，不会破坏 JSON） |
| `$negative_prompt` | 「负面提示词」设置 |
| `$image` | 首帧图 data URI（`data:image/png;base64,...`） |
| `$image_raw` | 首帧图裸 base64（不带前缀） |
| `$image_url` | 「首帧图片URL」设置（**自备图床**时用它代替 base64，省流量） |
| `$last_image` | 尾帧图（首尾帧一致时用） |
| `$frames` / `$fps` | 默认帧数 / 帧率 |
| `$duration` | 时长（秒） |
| `$seed` / `$ratio` / `$resolution` / `$mode` | 对应设置项 |
| `$task_id` | 仅**轮询请求体模板**里可用（当前任务 ID） |

示例：

```json
{"model": "$model", "prompt": "$prompt", "image": "$image_url",
 "negative_prompt": "$negative_prompt", "ratio": "$ratio", "seed": $seed}
```

其他相关设置：

- **提交方法**：`POST`（默认）/ `PUT`（JSON 请求体）/ `GET`
  （模板被摊平成查询参数 `k=v`，嵌套用 `a.b`，布尔转 `true/false`，数组整体不发送）。
- **轮询方法**：`GET`（默认）/ `POST` / `PUT`；选 POST/PUT 时可用
  **轮询请求体模板**，如 `{"task_id": "$task_id", "action": "query"}`。
- **额外字段(JSON)**：附加进请求体（如 `{"watermark": false}`），优先级高于默认体、低于模板。
- **提示词模板**：非模板模式下给 `prompt` 加前后缀，如 `"{prompt}, pixel art"`。

---

## 八、轮询参数说明（视频）

| 设置项 | 默认 | 说明 |
| --- | --- | --- |
| 轮询间隔(秒) | 5 | 两次查询之间等待多久；中转站限流严时调大（10~15） |
| 最大轮询次数 | 120 | 120 × 5s = 10 分钟；视频任务慢时可加大 |
| 成功状态(逗号分隔) | 内置 `succeeded,success,completed,done,finished` | 留空即用内置值 |
| 失败状态(逗号分隔) | 内置 `failed,error,cancelled,canceled,expired,rejected` | 命中即立刻报错并带服务端信息 |
| 状态字段路径 | `status` | 取不到时按内置候选回退 |

轮询是**先查后等**（提交后立刻查一次），任务完成得快不会白等一个间隔。

---

## 九、常见错误排查表

| 现象 | 常见原因 | 处理 |
| --- | --- | --- |
| **401 / 403** | Key 放错位置；中转站要 `X-API-Key` / `api-key` / 查询参数；余额不足（少数站点回 403） | 改「鉴权方式」，或选「自定义请求头」填头名前缀，或用「额外请求头」直接覆盖；错误信息里也会提示这点 |
| **404 Invalid URL** | Base URL 缺 `/v1` 等前缀；端点重复拼了版本段；该站根本不走 OpenAI 路径 | 用「预览请求…」看实际 URL；「从 curl 导入…」拿到真实端点；gpt.ge 视频需选对应适配（`/task/volces/seedance`） |
| **404 / 405 只在轮询出现** | 轮询端点写错（少了 `{id}` 或多了路径） | 在「轮询端点」里填 `…/{id}`；用「测试并检测字段…」确认任务 ID |
| **超时 / SSL/TLS 错误** | 网络被拦截、需要代理 | 高级选项「代理」填 `http://127.0.0.1:7890`；必要时关「校验 SSL 证书」；错误信息会附提示 |
| **轮询一直不结束** | 状态值不在「成功状态」里；状态字段路径不对 | 「测试并检测字段…」定位状态路径；把站点返回的状态值（如 `SUCCESS`）加进「成功状态」 |
| **返回结构与预期不符 / 提示无法获取任务 ID** | 提交响应里任务 ID 在别的路径 | 用「测试并检测字段…」的候选路径一键写入；或手填 `data.task_id` 之类 |
| **拿到任务但视频 URL 为空** | 结果字段路径不对 | 同上：检测字段 → 写入「视频URL字段路径」 |
| **生图 400 unsupported size** | 站点不支持小尺寸 | 程序会自动放大到 512/768/1024/1536 重试；也可把「默认尺寸」调大 |
| **图片返回解析失败** | 返回结构不是 `data[]` | 「响应图片数组字段路径」填实际路径（如 `result.images`） |
| **文字模型返回空** | 推理模型把 token 用在思考上 | 换非推理模型或调大 `max_tokens`；程序会自动回退取 `reasoning_content` |

排查三步法：**「测试连接」看可达性 → 「预览请求…」看请求 → 「测试并检测字段…」看响应**。

---

## 十、最小可用配置示例

### 1) 官方直连（OpenAI 兼容生图）

```
Base URL : https://api.openai.com/v1
API Key  : sk-…
模型名称 : gpt-image-1
鉴权方式 : Bearer 令牌（默认）
其余留空即可（端点默认 /images/generations）
```

### 2) 中转站视频（提交 + 轮询）

```
Base URL : https://relay.example.com
API Key  : sk-…
模型名称 : doubao-seedance-1-5-pro
服务商适配 : 通用（OpenAI 兼容轮询）
鉴权方式  : X-API-Key
提交端点  : {base}/v1/videos/generations
轮询端点  : {base}/v1/videos/generations/{id}
轮询方法  : GET
任务ID字段路径 : data.task_id
状态字段路径   : data.status
成功状态       : succeeded,completed
视频URL字段路径: data.video_url
请求体模板     : {"model": "$model", "prompt": "$prompt", "image": "$image",
                 "duration": "$duration", "ratio": "16:9"}
```

最省事的做法：先选预设「中转站：提交+轮询（通用模板）」，
「从 curl 导入…」把真实端点与请求头填进来，
再用「测试并检测字段…」一键修正三个字段路径。

### 3) 本地 Ollama（文本）

```
Base URL : http://localhost:11434/v1
API Key  : （随便填，或用「不鉴权」）
鉴权方式 : 不鉴权
模型名称 : llama3.1
```

---

## English summary

PixelFoundry speaks plain OpenAI-compatible HTTP, so **relay / aggregator stations**
(one-api, new-api, self-hosted gateways…) work without code changes — but they differ in
authentication, endpoints and response shapes. This guide covers:

- **Authentication styles** for all three API kinds (LLM / image / video):
  `Bearer`, `X-API-Key`, `api-key`, URL query parameter, custom header name+prefix, or none.
  Extra headers always win, so you can override anything ad hoc.
- **Base URL vs endpoint path** rules, and `{base}` / `{id}` placeholders for video
  submit/poll endpoints.
- **Relay shapes**: OpenAI-compatible, submit-then-poll task APIs, and Volcengine Ark
  `content` arrays.
- **Import from curl**: paste a browser "Copy as cURL" command and the app fills in the
  base URL, submit endpoint, request method, extra headers and a JSON body template with
  `$prompt` / `$image` / `$image_url` / `$model` … placeholders.
- **Preview request** (no network call, API key redacted) and **Test & detect fields**
  (one real submit request, then click-to-apply suggestions for the job-ID / status /
  video-URL JSON paths).
- **Video placeholders** (`$model $prompt $negative_prompt $image $image_raw $image_url
  $last_image $frames $fps $duration $seed $ratio $resolution $mode`, plus `$task_id` for
  the poll body), submit methods POST/PUT/GET, poll body templates and polling parameters.
- **Troubleshooting table** for 401/403, 404 Invalid URL, timeouts/SSL, stuck polling and
  unexpected response shapes — plus minimal working configurations.
