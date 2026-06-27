# qqbot-jmcomic

基于 NapCatQQ + OneBot 11 的 QQ 群机器人。群成员发送 `@机器人 JM123456` 后，机器人会把 JM 编号提交给后端，后端调用 `jmcomic` 下载并导出 PDF，最后由机器人把 PDF 上传回原群。

这个项目只封装调用 [`JMComic-Crawler-Python`](https://github.com/hect0x7/JMComic-Crawler-Python) 发布的 `jmcomic` 包，不修改第三方项目源码。

## 功能

- 只处理 QQ 群消息。
- 使用 OneBot 11 结构化消息段判断是否真的 `@` 了机器人。
- 支持 `JM123456`、`jm123456` 两种输入；纯数字不会触发下载。
- 可选开启关键词搜索：`@机器人 搜索 关键词`，用户回复序号后进入同一套预览确认流程。
- 一条消息只允许一个编号。
- 先发送封面和标题预览，用户回复确认后才加入下载队列。
- 如果预览检测到页数超过阈值，用户需要二次确认后才会开始下载。
- 同一群内同一用户同时只能有一个排队中、下载中或转换中的任务。
- 每个用户默认最多 1 个活跃任务，每个群默认最多 3 个活跃任务。
- 群主、群管理员和机器人管理者可以查询状态、队列和取消任务；清理缓存只允许机器人管理者执行。
- 下载任务写入 SQLite，服务重启后不会只依赖内存状态。
- 后端控制台会显示下载进度条；如果预览拿到了页数，会显示百分比和 `已下载/总页数`。
- 群内只发送关键状态，不会按“已保存 N 张图片”频繁刷屏。
- 任务失败会保存并返回稳定报错码，Bot 群消息也会显示报错码。
- JMComic 下载和 PDF 导出在独立子进程执行，总超时或长时间无文件写入都会终止子进程，避免单个卡死任务堵住队列。
- PDF 文件会命名为 `[JM编号]漫画标题.pdf`，并自动清理 Windows 不允许的字符。
- 下载完成后调用 NapCatQQ `upload_group_file` 上传 PDF。
- PDF 过大时会自动拆分为多个分卷 PDF 上传，分卷文件名使用 `JM123456_part01-of03.pdf`，方便在 QQ 群文件列表里识别。
- 上传失败会按配置重试，默认最多重试 5 次。
- 后端会定期清理过期缓存；Bot 上传成功后也会清理本次上传缓存，避免 `data/` 目录无限增长。
- Bot 群内文案集中放在 `lang/zh_CN.json`，后续维护提示语不用翻代码。
- Token、Cookie 和登录信息都通过本地配置提供，不写死在代码里。

## 环境要求

- Python 3.12+
- NapCatQQ
- OneBot 11 HTTP 和 WebSocket
- 可用的 JMComic 配置文件

## 快速开始

克隆项目后，先创建虚拟环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

后续从 GitHub 拉取更新后，也建议重新执行一次安装命令，确保新增依赖例如 `img2pdf` 已安装。

如果 PyPI 访问较慢，可以使用镜像：

```powershell
.\.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -e ".[test]"
```

复制环境变量文件：

```powershell
Copy-Item .env.example .env
```

复制 JMComic 配置文件：

```powershell
Copy-Item config\jmcomic-option.yml.example config\jmcomic-option.yml
```

然后编辑 `.env` 和 `config/jmcomic-option.yml`。

## 配置

`.env` 示例：

```env
BOT_QQ_ID=
NAPCAT_WS_URL=ws://127.0.0.1:3001
NAPCAT_HTTP_URL=http://127.0.0.1:3000
NAPCAT_ACCESS_TOKEN=
NAPCAT_HTTP_TIMEOUT_SECONDS=60
NAPCAT_UPLOAD_TIMEOUT_SECONDS=900
NAPCAT_MAX_UPLOAD_BYTES=104857600
NAPCAT_MAX_UPLOAD_FILENAME_BYTES=96
NAPCAT_UPLOAD_RETRIES=5
BOT_LANG=zh_CN
BOT_MANAGER_QQ_IDS=
BACKEND_URL=http://127.0.0.1:8000
BACKEND_API_TOKEN=
ENABLE_SEARCH=false
SEARCH_TIMEOUT_SECONDS=20
SEARCH_RESULT_LIMIT=5
SEARCH_CONFIRM_TIMEOUT_SECONDS=600
MAX_CONCURRENT_JOBS=1
MAX_ACTIVE_JOBS_PER_GROUP=3
MAX_ACTIVE_JOBS_PER_USER=1
JOB_TIMEOUT_SECONDS=1800
JOB_STALL_TIMEOUT_SECONDS=300
JOB_PROGRESS_CHECK_SECONDS=10
PREVIEW_TIMEOUT_SECONDS=30
JOB_PROGRESS_NOTIFY_SECONDS=300
JOB_CONFIRM_TIMEOUT_SECONDS=600
USER_COMMAND_COOLDOWN_SECONDS=10
LARGE_ALBUM_WARNING_PAGES=100
CACHE_CLEANUP_INTERVAL_SECONDS=3600
JOB_CACHE_TTL_SECONDS=259200
BOT_DOWNLOAD_CACHE_TTL_SECONDS=259200
PREVIEW_CACHE_TTL_SECONDS=86400
JM_DOWNLOAD_IMAGE_THREADS=16
JM_DOWNLOAD_PHOTO_THREADS=4
JM_DOWNLOAD_MAX_IMAGE_THREADS=16
JM_DOWNLOAD_MAX_PHOTO_THREADS=4
JMCOMIC_OPTION_PATH=./config/jmcomic-option.yml
DATA_DIR=./data
```

字段说明：

| 变量 | 说明 |
| --- | --- |
| `BOT_QQ_ID` | 机器人 QQ 号，必须填写 |
| `NAPCAT_WS_URL` | NapCatQQ OneBot 11 WebSocket 地址 |
| `NAPCAT_HTTP_URL` | NapCatQQ OneBot 11 HTTP 地址 |
| `NAPCAT_ACCESS_TOKEN` | NapCatQQ access token，没有则留空 |
| `NAPCAT_HTTP_TIMEOUT_SECONDS` | NapCatQQ 普通 HTTP API 超时，默认 `60` 秒 |
| `NAPCAT_UPLOAD_TIMEOUT_SECONDS` | NapCatQQ 上传群文件超时，默认 `900` 秒；大 PDF 建议保持较大 |
| `NAPCAT_MAX_UPLOAD_BYTES` | 单个上传文件大小上限，超过会自动拆分 PDF；默认 `104857600`，即 100MB |
| `NAPCAT_MAX_UPLOAD_FILENAME_BYTES` | 上传到 QQ 群文件时使用的展示文件名字节上限，默认 `96` |
| `NAPCAT_UPLOAD_RETRIES` | 单个文件上传失败后的重试次数，默认 `5` |
| `BOT_LANG` | Bot 群内提示语言文件，默认 `zh_CN`，对应 `lang/zh_CN.json` |
| `BOT_MANAGER_QQ_IDS` | 机器人管理者 QQ 号，多个用英文逗号分隔；管理者可执行清理缓存等维护命令 |
| `BACKEND_URL` | 后端 FastAPI 地址 |
| `BACKEND_API_TOKEN` | 后端 API token，没有则留空 |
| `ENABLE_SEARCH` | 是否启用关键词搜索，默认 `false`；启用后 Bot 和后端都会处理搜索 |
| `SEARCH_TIMEOUT_SECONDS` | 后端搜索子进程超时时间，默认 `20` 秒 |
| `SEARCH_RESULT_LIMIT` | 每次搜索返回结果数，默认 `5`，最大 `10` |
| `SEARCH_CONFIRM_TIMEOUT_SECONDS` | 搜索结果出来后等待用户回复序号的时间，默认 `600` 秒 |
| `MAX_CONCURRENT_JOBS` | 同时下载任务数，默认 `1` |
| `MAX_ACTIVE_JOBS_PER_GROUP` | 每个群允许同时存在的活跃任务数，默认 `3` |
| `MAX_ACTIVE_JOBS_PER_USER` | 每个用户允许同时存在的活跃任务数，默认 `1` |
| `JOB_TIMEOUT_SECONDS` | 单个任务总超时时间，默认 `1800` 秒 |
| `JOB_STALL_TIMEOUT_SECONDS` | 下载子进程无文件变化的卡住超时，默认 `300` 秒；设为 `0` 可关闭 |
| `JOB_PROGRESS_CHECK_SECONDS` | 后端检查下载进度和卡住状态的间隔，默认 `10` 秒 |
| `PREVIEW_TIMEOUT_SECONDS` | 获取漫画封面和标题的超时时间，默认 `30` 秒 |
| `JOB_PROGRESS_NOTIFY_SECONDS` | 群内非下载阶段进度通知间隔，默认 `300` 秒；后端控制台进度条不受影响 |
| `JOB_CONFIRM_TIMEOUT_SECONDS` | 预览后等待用户确认的时间，默认 `600` 秒 |
| `USER_COMMAND_COOLDOWN_SECONDS` | 同一群同一用户发送新任务或搜索命令的冷却时间，默认 `10` 秒 |
| `LARGE_ALBUM_WARNING_PAGES` | 超过多少页触发二次确认，默认 `100`；设为 `0` 可关闭 |
| `CACHE_CLEANUP_INTERVAL_SECONDS` | 后端缓存清理间隔，默认 `3600` 秒；设为 `0` 可关闭 |
| `JOB_CACHE_TTL_SECONDS` | 已完成/已失败任务目录保留时间，默认 `259200` 秒，即 3 天 |
| `BOT_DOWNLOAD_CACHE_TTL_SECONDS` | Bot 下载到本地准备上传的 PDF 缓存保留时间，默认 3 天 |
| `PREVIEW_CACHE_TTL_SECONDS` | 漫画预览临时文件保留时间，默认 `86400` 秒，即 1 天 |
| `JM_DOWNLOAD_IMAGE_THREADS` | JMComic 图片下载线程数，默认建议 `16` |
| `JM_DOWNLOAD_PHOTO_THREADS` | JMComic 章节下载线程数，默认建议 `4` |
| `JM_DOWNLOAD_MAX_IMAGE_THREADS` | 图片下载线程硬上限，默认 `16`，防止小服务器被过高并发拖死 |
| `JM_DOWNLOAD_MAX_PHOTO_THREADS` | 章节下载线程硬上限，默认 `4` |
| `JMCOMIC_OPTION_PATH` | JMComic 配置文件路径 |
| `DATA_DIR` | 数据目录 |

不要提交 `.env`、JMComic Cookie、NapCat token 或任何登录信息。

## NapCatQQ 配置

在 NapCatQQ 中开启 OneBot 11：

- HTTP 服务地址对应 `NAPCAT_HTTP_URL`，例如 `http://127.0.0.1:3000`。
- WebSocket 服务地址对应 `NAPCAT_WS_URL`，例如 `ws://127.0.0.1:3001`。
- 如果 NapCatQQ 配置了 access token，把同一个值写到 `NAPCAT_ACCESS_TOKEN`。

本项目默认 Bot、后端、NapCatQQ 同机部署。上传文件时会调用：

```json
{
  "group_id": "123456789",
  "file": "PDF绝对路径",
  "name": "[JM123456]title.pdf"
}
```

Bot 会检查 NapCatQQ 响应中的 `status` 和 `retcode`，不会只看 HTTP 状态码。大 PDF 如果触发 `rich media transfer failed`，通常是 NapCat/QQ 上传阶段拒绝了大文件；本项目会按 `NAPCAT_MAX_UPLOAD_BYTES` 自动拆成多个 PDF 分卷再上传。

## JMComic 配置

编辑：

```text
config/jmcomic-option.yml
```

填入你自己的 JMComic 客户端、Cookie 或下载配置。服务器部署建议优先使用 `impl: api`，通常比网页端更不容易遇到 IP 地区限制。

示例：

```yaml
client:
  impl: api
  retry_times: 5
  postman:
    meta_data:
      headers:
        User-Agent: "Mozilla/5.0"
      cookies:
        AVS: "你的AVS Cookie"

download:
  image:
    decode: true
  threading:
    image: 20
    photo: 4
```

`download.threading.image` 和 `download.threading.photo` 可以影响下载并发。也可以在 `.env` 里用 `JM_DOWNLOAD_IMAGE_THREADS` 和 `JM_DOWNLOAD_PHOTO_THREADS` 覆盖它们，服务器建议先从 `16` 和 `4` 开始试。数值越大不一定越快，过高可能触发限流、CPU/IO 飙升，甚至让 SSH 都响应变慢。后端会再用 `JM_DOWNLOAD_MAX_IMAGE_THREADS` 和 `JM_DOWNLOAD_MAX_PHOTO_THREADS` 做硬上限，避免误填过高并发把小服务器拖死。后端只在 `backend/downloader.py` 中调用 `jmcomic`。

每个任务会使用独立目录：

```text
data/jobs/{job_id}/
```

PDF 生成后会校验：

- PDF 文件存在
- 文件大小大于 0
- 最终只能有一个 PDF
- 文件名包含 JM 编号和漫画标题
- 文件名会清理 Windows 非法字符

## 启动

先启动后端：

```powershell
.\.venv\Scripts\python.exe -m backend.main
```

再启动 Bot：

```powershell
.\.venv\Scripts\python.exe -m bot.main
```

群内使用示例：

```text
@机器人 JM123456
```

如果开启了 `ENABLE_SEARCH=true`，也可以搜索关键词：

```text
@机器人 搜索 戦乙女
```

机器人会返回最多 `SEARCH_RESULT_LIMIT` 条结果。用户回复序号后，机器人会继续发送封面、标题、页数和预计时间，并询问是否下载；不会直接加入下载队列。

没有编号时，机器人会回复：

```text
用法：@机器人 JM123456
```

机器人会先发送封面、标题、页数和预计时间，并询问是否下载。用户回复：

```text
下载
```

如果页数超过 `LARGE_ALBUM_WARNING_PAGES`，机器人会先发送警告，用户需要再次回复“下载”才会加入队列。

确认后，机器人会加入下载队列并回复：

```text
已接收 JM123456，任务编号：xxxx
```

如果正在下载的任务卡住或不想继续，同一个用户可以在群里回复：

```text
取消下载
```

取消会按“群号 + 用户 QQ”在后端查询当前任务，所以 Bot 重启后仍然可以取消排队中或下载中的任务。

管理员命令需要 `@机器人`：

```text
@机器人 状态
@机器人 队列
@机器人 取消 JM123456
@机器人 取消 任务编号前几位
@机器人 清理缓存
```

`状态`、`队列`、`取消` 允许 QQ 群主、QQ群管理员、机器人管理者执行。`清理缓存` 只允许机器人管理者执行。机器人管理者由部署者在 `.env` 的 `BOT_MANAGER_QQ_IDS` 中配置，不等同于 QQ 群管理员。

任务完成后，机器人会上传 PDF 并发送完成消息。

## 如何测试

### 1. 跑单元测试

单元测试不需要真实 NapCatQQ，也不会真实下载 JMComic 内容：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

覆盖内容包括：

- `JM123456` 解析
- 没有 `@` 机器人时忽略
- 没有编号时提示用法
- 正常创建任务
- 下载失败
- PDF 未生成
- 上传成功
- 上传失败重试
- 搜索命令解析
- 搜索结果选择后进入预览确认
- 管理员命令权限
- 每群/每用户活跃任务限制
- 上传阶段管理员取消

### 2. 测后端是否能启动

启动后端：

```powershell
.\.venv\Scripts\python.exe -m backend.main
```

另开一个终端检查健康接口：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

正常会返回：

```json
{
  "status": "ok"
}
```

### 3. 测后端创建任务接口

确认 `config/jmcomic-option.yml` 已配置好后，可以手动创建一个任务：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/jobs `
  -ContentType "application/json" `
  -Body '{"album_id":"123456","group_id":"123456789","user_id":"987654321"}'
```

返回示例：

```json
{
  "job_id": "uuid",
  "status": "queued"
}
```

查询任务：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/jobs/{job_id}
```

任务完成后下载 PDF：

```powershell
Invoke-WebRequest `
  -Uri http://127.0.0.1:8000/api/jobs/{job_id}/file `
  -OutFile .\test.pdf
```

### 4. 测 NapCatQQ 联通

确认 NapCatQQ 已登录并启用 OneBot 11 HTTP 后，可以直接调用发群消息接口：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:3000/send_group_msg `
  -ContentType "application/json" `
  -Body '{"group_id":"你的群号","message":"NapCatQQ 测试消息"}'
```

如果你配置了 `NAPCAT_ACCESS_TOKEN`，需要在请求里带上：

```powershell
-Headers @{ Authorization = "Bearer 你的token" }
```

### 5. 完整联调

1. 启动 NapCatQQ，并确认 HTTP / WebSocket 已开启。
2. 启动后端：`.\.venv\Scripts\python.exe -m backend.main`
3. 启动 Bot：`.\.venv\Scripts\python.exe -m bot.main`
4. 在 QQ 群发送：`@机器人 JM123456`
5. 检查机器人是否发送封面、标题和页数预览。
6. 回复：`下载`
7. 检查机器人是否回复任务编号。
8. 等待下载和转换完成。
9. 检查群文件里是否出现 PDF。

如果失败，先看两个终端里的日志。群内只会发送简短错误和报错码，完整异常会留在服务日志中。

常见报错码示例：

| 报错码 | 含义 |
| --- | --- |
| `JM_DOWNLOAD_FAILED` | JM 下载失败，通常是网络、Cookie、限流或请求失败 |
| `JM_NOT_FOUND` | JM 内容不存在或不可访问 |
| `PDF_GENERATION_FAILED` | 图片转 PDF 失败 |
| `JOB_TIMEOUT` | 任务超过 `JOB_TIMEOUT_SECONDS` 总超时 |
| `JOB_STALLED` | 超过 `JOB_STALL_TIMEOUT_SECONDS` 没有新文件写入，任务被自动终止 |
| `USER_CANCELLED` | 用户主动取消任务 |
| `NAPCAT_UPLOAD_FAILED` | PDF 已生成，但 NapCat 上传群文件失败 |

`PDF_GENERATION_FAILED` 优先检查后端虚拟环境是否安装了 `img2pdf`，并查看对应任务目录下的 `worker-output.log` 和 `worker-error.log`。

## 后端接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `GET` | `/api/albums/{album_id}/preview` | 获取漫画封面、标题、页数和预计时间 |
| `POST` | `/api/search` | 关键词搜索漫画，默认需要 `ENABLE_SEARCH=true` |
| `POST` | `/api/jobs` | 创建下载任务 |
| `GET` | `/api/jobs/active?group_id=...&user_id=...` | 查询某个群用户当前活跃任务 |
| `POST` | `/api/jobs/active/cancel?group_id=...&user_id=...` | 取消某个群用户当前活跃任务 |
| `GET` | `/api/jobs/{job_id}` | 查询任务状态，包含 `downloaded_files`、`total_files`、`error_code` |
| `POST` | `/api/jobs/{job_id}/cancel` | 按任务编号取消排队中或下载中的任务 |
| `GET` | `/api/jobs/{job_id}/file` | 下载 PDF |
| `GET` | `/api/admin/status` | 查询服务器状态、缓存大小和任务统计 |
| `GET` | `/api/admin/queue` | 查询当前队列和最近错误任务 |
| `POST` | `/api/admin/jobs/{target}/cancel` | 管理员按 JM 编号或任务编号取消任务 |
| `POST` | `/api/admin/cache/cleanup` | 手动清理缓存；有活跃后端任务时会拒绝 |

任务状态：

```text
queued
downloading
converting
completed
failed
```

## 项目结构

```text
project/
├─ bot/
│  ├─ main.py
│  ├─ napcat_client.py
│  ├─ message_parser.py
│  ├─ backend_client.py
│  └─ lang.py
├─ backend/
│  ├─ main.py
│  ├─ search_worker.py
│  ├─ downloader.py
│  ├─ task_manager.py
│  └─ models.py
├─ config/
│  └─ jmcomic-option.yml.example
├─ lang/
│  └─ zh_CN.json
├─ data/
├─ tests/
├─ .env.example
├─ pyproject.toml
└─ README.md
```

## 安全说明

- 不要把 Token、Cookie、账号密码提交到仓库。
- 只允许处理数字形式 JM 编号。
- 不允许用户控制文件路径。
- 不使用 `shell=True`。
- 下载、转换、上传失败时，群内只返回简短错误，详细异常写入日志。
