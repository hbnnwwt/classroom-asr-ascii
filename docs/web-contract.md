# Web 接口与迁移约定

适用于本轮可靠性改进后的本机服务。完整路由和请求结构可在服务的 `/docs` 查看；本页记录旧客户端需要注意的行为变化。

## 配置、认证与密钥

服务使用 `python -m asr.web --config FILE` 或 `ASR_WEB_CONFIG` 固定配置。HTTP `config` 字段省略、`default`、`config.yaml` 均映射到固定文件；其他值返回 400，错误码 `config_not_allowed`。HTTP 不能选择服务器上的任意 YAML。

无 `ASR_WEB_PASSWORD` 时，真实连接地址必须为 loopback，Host 也必须是 `localhost` 或字面 loopback 地址（如 `127.0.0.1`、`[::1]`）；任一条件不满足返回 403。自定义域名即使解析到本机，也需配置访问密码。设置密码后，所有页面、API、静态资源和下载使用 HTTP Basic，用户名 `classroom`。缺少或错误认证返回 401 和 `WWW-Authenticate`。伪造 Host、X-Forwarded-For 不会获得本机权限。浏览器的状态变更请求须同源；普通脚本可不发送 Origin。

`GET /api/llm/settings` 的 `api_key` 恒为 `null`；`api_key_set` 表示是否已有密钥，`api_key_masked` 只含固定掩码。保存设置和连接测试共用以下规则：

| `api_key` 输入 | 含义 |
|---|---|
| 省略或 `null` | 使用原密钥 |
| 非空字符串 | 使用新密钥 |
| `""` | 明确清空密钥 |

连接测试不写配置。客户端不应把掩码发回当作密钥，也不应将未编辑的密码框发送为空串。

## 上传凭证与取消

先获取凭证，再将其放入原有上传请求头 `X-Upload-Id`。不传凭证的旧上传客户端仍可工作。

| 请求 | 结果 |
|---|---|
| `POST /api/uploads` | 201，`{upload_id,status:"open",task_id:null}` |
| `GET /api/uploads/{upload_id}` | 200，返回 `open/cancelled/accepted` 和实际 task_id；未知为 404 |
| `POST /api/projects` 或 `/upload` | 保留旧成功 200 及原字段；可传 `X-Upload-Id` |
| `POST /api/uploads/{upload_id}/cancel` | 取消先完成时为 200 cancelled，重复取消幂等 |
| 已受理后取消 | 409，`code: upload_already_accepted` 及实际 task_id；任务继续 |

受理和取消在 SQLite 事务中竞争。取消被确认后，不会再用同一凭证创建任务；受理成功后，同一凭证不会创建第二个任务。XHR abort 只停止客户端发送，不能作为取消成功证据。若响应丢失，查询凭证恢复结果；未获确认时显示“取消结果暂未确认”。

示例（省略认证时要求本机访问；需认证时可给 curl 增加 `-u classroom`，由终端提示输入密码）：

```bash
curl -X POST http://127.0.0.1:8000/api/uploads
curl -H 'X-Upload-Id: 返回的upload_id' -F 'files=@lesson.wav' http://127.0.0.1:8000/api/projects
curl -X POST http://127.0.0.1:8000/api/uploads/返回的upload_id/cancel
```

## 排队、AI、删除与恢复

执行器默认 1 个 worker、最多 8 个等待位置。队满返回 503 `queue_full` 和 `Retry-After`，不留下半受理任务。AI 自动生成与手动生成统一准入；手动请求成功仍是 202 `{status:"started"}`，重复或任务忙返回 409。取消上传和删除都不提供强杀 native 推理的能力。

保留旧状态 `pending/running/done/failed`。状态、列表和详情补充：

| 字段 | 用途 |
|---|---|
| `execution_active` | 是否仍有登记的后台执行者 |
| `can_delete` | 当前是否允许删除 |
| `transcription_status` | 转写状态，AI 运行时可保持 done |
| `ai_status` | AI 独立状态 |
| `available_formats` | 当前实际可下载格式 |

单删忙任务返回 409 `task_busy`；任务即使被看门狗标记失败，只要执行者仍活跃也不能删。批删保留 `deleted`、`failed` 两个 ID 数组，新增 `failure_details` 逐项说明。

启动时，未结束的转写任务标记中断；仅 AI 中断时保留转写和旧 AI 稿，记录尝试失败，不自动再次调用服务。看门狗超时表示状态标记失败，不表示底层 native 推理已终止。关闭时先停止准入，再等待在途执行者，最后释放模型。

## 外部集成

### `GET /health`

此端点供调用方或负载均衡探活，检查能否读取服务配置的 SQLite 任务表。检查成功返回 HTTP 200，`status` 为 `ok`，`checks.db` 为 `true`。探测失败时返回 HTTP 503，`status` 为 `degraded`，`checks.db` 为 `false`。它沿用页面的访问控制。设置 `ASR_WEB_PASSWORD` 后，探针也需使用 HTTP Basic，用户名为 `classroom`。模型检查使用 `python -m asr check`，转写状态通过任务接口查询。

### `POST /api/transcribe`

此端点仅接受与服务进程直接建立的真实回环连接，包含设置访问密码的部署。远程程序通过 `POST /api/projects` 上传。若用同机反向代理接受远程请求，在代理的远程入口屏蔽 `/api/transcribe`。应用按直连对端识别回环，且不会使用转发头判断请求来源。

用 `application/x-www-form-urlencoded` 提交服务端文件路径。该任务借用源文件，调用方须确保源在转写完成前保持可读。任务删除时由服务清理产物并保留源文件。

| 字段 | 必填 | 含义 |
|---|---|---|
| `input_path` | 是 | 服务端路径。相对路径按服务进程当前目录解析 |
| `sync` | 否 | 默认为 `false`，受理后即返回。设为 `true` 时等待转写 |
| `fmts` | 否 | 同步返回的格式，默认 `srt,txt`。同步请求至少选择一项，用逗号分隔，可选 `srt`、`vtt`、`txt`、`md`、`json` |
| `name` / `config` / `quality` / `diarize` / `ai_feedback` | 否 | 沿用上传语义。`config` 映射到服务启动时固定的配置 |
| `timeout` | 否 | 同步等待转写的上限（秒）。取值大于 0 且不超过 3600，默认 3600 |

- 非法路径、无法探测媒体或没有音轨时返回 400。非法 `config`、`quality`、同步请求里的非法 `timeout` 或空白及不支持的 `fmts` 同样在受理前返回 400。
- 异步受理和同步等待超时均返回 HTTP 202，响应为 `{"task_id":"...","status":"accepted"}`。任务继续执行。保留该 `task_id`，轮询 `GET /status/{task_id}` 的 `transcription_status`，转写完成后下载所需格式。
- 同步请求在转写完成后返回 HTTP 200 和 `status: "done"`，AI 整理可继续在后台运行。转写失败时返回 HTTP 500，`detail` 包含 `task_id` 和 `message`。
- `formats` 是成功读取的格式及其文本内容。SRT 按产物生成时的 `meta.srt_encoding` 解码，默认为 `utf-8-sig`。其他格式用 UTF-8 解码。返回的文本会去除开头的 BOM。`available_formats` 为响应时已生成的格式。`format_errors` 按请求格式给出未生成或读取失败的原因。转写成功且某个请求格式不可用时，响应仍为 HTTP 200，调用方应检查该格式后再保存字幕。
- 受理使用现有有界队列。队满返回 HTTP 503、错误码 `queue_full` 和 `Retry-After`。

`meta.source_path` 记录解析后的源路径。下载仍限于该任务的产物目录。

### 字幕文本清洗

SenseVoiceSmall 返回识别结果后，FunASR 引擎在标点恢复和分段前去除已知的模型控制标记，例如 `<|zh|><|NEUTRAL|><|Speech|><|withitn|>`。普通文本中的尖括号保留，只有控制标记的结果不会生成空字幕。CLI、Web 上传和本机路径转写共用这一处理。

## 产物与 AI 发布

新任务的基础产物由 `meta.output_files` 登记真实文件名，下载限定在该任务输出目录；仅未登记的旧任务使用受限的旧文件查找。重命名项目不影响定位。`meta.quality` 记录新任务请求值，缺失的历史任务不会被推测为某一质量。

`meta.ai_files` 保留最后完整稿。本次每件生成尝试放在 `meta.ai_attempts[format]`，状态为 `queued/running/succeeded/failed`；当前错误放在 `meta.ai_errors`。先完整写入同目录临时文件，再原子替换并更新登记。替换失败时旧稿保持可用。

LLM 返回 `finish_reason=length/content_filter`、非字符串或空正文均为失败。兼容未返回结束原因的服务，但不将其描述为已验证完整。没有自动续写或自动重试；用户重新生成才发起新调用。`ended_at` 保持转写结束时间，不随 AI 重跑改变。
