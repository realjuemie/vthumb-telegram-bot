# VThumb Telegram Bot

Telegram 视频缩略图机器人：按需读取分块，生成 PotPlayer 风格联系表，不预先下载完整视频。

**试用：** [Launch @vthumbot](https://t.me/vthumbot)

## 主题示例

<table>
<tr>
<th align="center">PotPlayer 风格</th>
<th align="center">白底黑字</th>
</tr>
<tr>
<td><img src="docs/examples/theme-potplayer.jpg" alt="PotPlayer 风格"></td>
<td><img src="docs/examples/theme-white-bg.jpg" alt="白底黑字"></td>
</tr>
<tr>
<th align="center">纯图无信息</th>
<th align="center">黑底白字</th>
</tr>
<tr>
<td><img src="docs/examples/theme-pure-image.jpg" alt="纯图无信息"></td>
<td><img src="docs/examples/theme-minimal-white.jpg" alt="黑底白字"></td>
</tr>
</table>

`/setting` 还可切换极简模式（黑 / 白）。每个用户独立保存。

机器人不会预先下载完整源视频。它只读取 FFprobe / FFmpeg 实际请求的分块，并使用任务级临时缓存避免重复下载；大视频达到自适应硬上限后会主动停止。

## 功能

- 普通视频和“作为文件发送”的视频均可处理
- MTProto 分块读取，不受 Bot API `getFile` 的 20MB 下载限制
- 512KB 对齐的 HTTP Range 读取
- LRU 内存缓存 + 任务级临时磁盘缓存，内存淘汰后仍不会重复下载
- 小文件兼容模式与大视频自适应读取预算
- 大视频读取比例和绝对流量双重硬限制
- PotPlayer 风格 PNG 联系表
- 6 种可切换的视觉主题（PotPlayer 风格、黑底白字、白底黑字、纯图无信息、极简模式：黑、极简模式：白），每个用户独立保存
- 单帧缩略图尺寸 1280px 宽（ffmpeg 阶段），保证 tile 内的细节锐利
- ffmpeg `-strict -1`，支持非标准 mjpeg / yuvj420p 等少数输入流（不会再因 "Non full-range YUV is non-standard" 静默失败）
- 失败回复嵌入 ffmpeg 真实 stderr，便于排查（容器日志同步输出 WARNING 级别，便于事后追溯）
- 长文件名自动换行并按需扩展顶部 band（极简模式）
- 文件名、文件大小、分辨率、帧率、视频/音频编码、时长
- 第一帧提前取样，减少第一张画面过晚的问题
- 横屏、竖屏和旋转视频自适应
- 处理、截帧、合成和上传进度实时更新
- 多人同时发送按到达顺序排队，并提示前方任务数
- 用户可选择媒体发送或文件原图发送
- 管理员白名单与持久化用户授权
- 每个用户独立保存缩略图布局和发送方式
- 自动同步 Telegram 机器人命令菜单

## 工作原理

```text
Telegram 视频消息
       |
       v
Telethon / MTProto 原生媒体对象
       |
       v
容器内 HTTP Range 服务
       |
       +--> 512KB Telegram 分块读取
       +--> LRU 内存缓存
       +--> 任务级临时磁盘分块缓存
       +--> 自适应读取预算与硬上限
       |
       v
FFprobe 读取媒体信息
FFmpeg 按时间点截取画面
       |
       v
Pillow 合成 PNG
       |
       v
Telethon 上传成品
```

机器人不会创建完整源视频文件。已读取的 512KB 分块、单张 JPEG 帧和最终 PNG 会写入任务级临时目录，任务结束后自动删除。小文件在 FFmpeg 确实需要时可能读取完整范围；大视频始终受比例与绝对容量硬上限保护。

## 默认设置

新用户默认使用：

- `16帧 4x4`
- `媒体发送（Telegram 会压缩）`
- 输出宽度 `1920px`
- 主题 `PotPlayer 风格`

`/setting` 可选择：

| 帧数 | 横 x 竖 |
| --- | --- |
| 16 帧 | 4x4 |
| 20 帧 | 5x4 |
| 25 帧 | 5x5 |
| 30 帧 | 5x6 |

发送方式：

- 媒体发送：在聊天中直接显示图片，但 Telegram 会压缩
- 文件发送：以文件发送 PNG，保留原图

视觉主题（见下文“主题”章节）：

| 主题 | 描述 |
| --- | --- |
| PotPlayer 风格 | 默认风格。白底，文件名/大小/分辨率/编码/时长 6 行信息，白字加 2px 黑描边；右下角时间戳白字黑底 |
| 黑底白字 | 纯黑底，白字无描边，仅文件元信息（6 行） |
| 白底黑字 | 纯白底，黑字无描边，仅文件元信息（6 行） |
| 纯图无信息 | 完全白底，仅 N×N 帧 + 时间戳，无任何文字 |
| 极简模式：黑 | 黑色细 band 在顶 + 底部；顶部仅显示文件名（过长自动换行），底部右下角显示 `分辨率 \| 时长 \| 帧率 \| 文件大小` |
| 极简模式：白 | 同极简模式：黑，但底色为白 |

所有选择均按 Telegram 用户 ID 单独保存，不影响其他用户。

## 主题

`/setting` 提供 6 种视觉主题，前 4 种（PotPlayer 风格 / 黑底白字 / 白底黑字 / 纯图无信息）顶部为多行元信息块；后 2 种（极简模式：黑 / 极简模式：白）改为顶部 band 仅显示文件名、底部 band 仅显示一条紧凑 metadata（`1920x1080 \| 00:30:12 \| 60FPS \| 668.76MB`），NxN 帧区域保持与 PotPlayer 风格一致。

**长文件名处理**：极简模式下若文件名超出 band 宽度，会自动换行并垂直扩展顶部 band 高度，NxN 帧区域整体下移以腾出空间。其他主题仍走原“按字符数断行”的旧路径（信息块本身已多行）。

**时间戳样式**：所有主题统一为 PotPlayer 风格的“白字 + 2px 黑色描边 + 半透明黑底”，确保在不同底色（白/黑/灰）下都能读清，不会随主题切换而变。

主题选择会写入 `data/preferences.json`（按用户 ID 隔离），重启容器后保留。

## 机器人命令

所有用户可见：

- `/start`：开始使用
- `/help`：查看使用说明
- `/id`：查看自己的 Telegram 用户 ID

已授权用户：

- `/status`：查看运行状态和个人设置
- `/setting`：通过交互按钮修改布局和发送方式

管理员：

- `/add 用户ID`：添加授权用户
- `/del 用户ID`：删除授权用户
- `/users`：查看管理员和授权名单

`ADMIN_IDS` 中的管理员始终有权限，不能通过 `/del` 删除。

## /merge 私聊媒体合并 (2026-08-06)

在私聊中依次发送 `N` 条媒体（图片、视频、动图、圆视频），bot 会用一条
`send_media_group` 把它们**在线转发**给同一个人——只发一次，按顺序排列，
**不下载、不暂存**到本地。

### 用法

```
/merge 3
... 发 3 条媒体 ...
```

收到 3 条后的 5 秒内（debounce）bot 会触发合并（防抖），按"图片在前、视频
在后"的顺序排好，发送给当前用户（不发到群里）。`/merge` 末尾数字的范围是
1-20。

子命令：

- `/merge cancel` — 取消当前合并请求
- `/merge status` — 查看当前会话的合并状态

### 限制

- 只支持**私聊**（避免群组多人同时发的混乱）
- 单用户单会话 — 再次发 `/merge N` 会拒绝之前的，提示先 `/merge cancel`
- 30 分钟超时 — 防止开了 merge 后忘了
- 文件 ID 失效（如源消息被删除）— 报告"第 N 条失效" + 提示用"引用转发"重试

### 状态持久化

`data/merge_state.json` 记录 pending 合并，崩溃可恢复（启动时清理 stale 状态）。

## 文件名换行算法 (2026-08-11)

potplayer / black_bg / white_bg 主题以及 minimal 主题的文件名换行都是通过
`_wrap_filename_to_width` 函数实现的，目标是：

- **行填充率 75-90%** — 不再每行都塞到溢出点
- **不在扩展名中间断开** — `.mp4` / `.mkv` / `.mov` 等视频后缀**始终保持完整**
- **不留下长长的尾点** — 不会以 `.` 结尾，dot-break 会前移一位
- **没有空白也优雅降级** — CJK 长字符串按字符切分，多行 fill rate 接近
- **超长名（200 字符）按 98% fill 切 5-9 行** — 而不是 1 行溢出 + 1 行 1 字符尾巴

### 关键常量

- `BAL_TARGET = 0.75` — 目标填充率 75%
- `BAL_MIN    = 0.50` — 行最小填充率（避免孤儿行）
- `TARGET_RATIO = 0.85` — 老 API 的 target ratio，仍保留以兼容
- `_VIDEO_EXTS` — 视频扩展名黑名单：`.mp4` `.mkv` `.mov` `.avi` `.webm`
  `.ts` `.flv` `.wmv` `.m4v` `.mpg` `.mpeg` `.m2ts` `.3gp` `.3g2` `.rm`
  `.rmvb` `.vob` `.mts`

### 历史 / 已知坑

- **第一版** (`e883c6d`): greedy 字符级 wrap — 把 `.mp4` 从中间切开
- **第二版** (`fix-2026-08-11`): BAL_TARGET=0.75 + extension-aware
  + best_dot_drop — 完全修复 `.m`+`p4` 拆分问题

## 部署要求

- Docker Engine 24+ 或 Docker Desktop
- Docker Compose v2
- Telegram Bot Token
- Telegram `API_ID` 和 `API_HASH`
- 能连接 Telegram MTProto 数据中心的网络

## 获取 Telegram 凭据

### 1. Bot Token

1. 在 Telegram 打开 [@BotFather](https://t.me/BotFather)。
2. 发送 `/newbot`。
3. 按提示创建机器人并保存 Token。

### 2. API ID 和 API Hash

1. 登录 [my.telegram.org](https://my.telegram.org)。
2. 打开 **API development tools**。
3. 创建应用并保存 `api_id` 与 `api_hash`。

### 3. 管理员 ID

可以通过现有 Telegram ID 查询机器人获取数字 ID。机器人启动后也支持 `/id`，但首次部署必须先在 `.env` 中至少配置一个管理员。

## Docker 快速部署

```bash
git clone https://github.com/realjuemie/vthumb-telegram-bot.git
cd vthumb-telegram-bot
cp .env.example .env
```

Windows PowerShell：

```powershell
git clone https://github.com/realjuemie/vthumb-telegram-bot.git
Set-Location vthumb-telegram-bot
Copy-Item .env.example .env
```

编辑 `.env`，至少填写：

```dotenv
BOT_TOKEN=1234567890:replace_with_bot_token
TG_API_ID=12345678
TG_API_HASH=replace_with_api_hash
ADMIN_IDS=123456789
```

多个管理员用英文逗号分隔：

```dotenv
ADMIN_IDS=123456789,987654321
```

启动：

```bash
docker compose up -d --build
```

查看状态和日志：

```bash
docker compose ps
docker compose logs -f --tail 100
```

停止：

```bash
docker compose down
```

更新：

```bash
git pull
docker compose up -d --build
```

## 中国大陆构建加速

如果 Docker 构建卡在 Debian 软件源或 PyPI，可以在 `.env` 中启用镜像：

```dotenv
DEBIAN_MIRROR=http://mirrors.aliyun.com
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
```

然后重新构建：

```bash
docker compose build --no-cache
docker compose up -d
```

说明：

- `DEBIAN_MIRROR` 会把 `deb.debian.org` 替换为指定镜像。
- `PIP_INDEX_URL` 只影响 Docker 构建阶段的 Python 依赖安装。
- 镜像地址可能调整，请以镜像站当前帮助页面为准。
- 如果镜像同步异常，删除这两个变量即可恢复官方源。

## 代理配置

Telethon 使用 MTProto TCP 连接 Telegram。需要代理时，在 `.env` 中设置：

```dotenv
MT_PROXY_URL=http://host.docker.internal:7897
```

也支持 SOCKS5：

```dotenv
MT_PROXY_URL=socks5://host.docker.internal:1080
```

注意：

- Docker Desktop 中访问宿主机代理通常使用 `host.docker.internal`，不能写 `127.0.0.1`。
- 代理软件需要允许局域网或 Docker 虚拟网络访问。
- 端口请改成自己的实际端口。
- TUN 模式是否覆盖 Docker 网络取决于代理软件；连接失败时优先显式配置 `MT_PROXY_URL`。

构建阶段如果也需要 HTTP 代理，可另外设置：

```dotenv
HTTP_PROXY=http://host.docker.internal:7897
HTTPS_PROXY=http://host.docker.internal:7897
```

## 配置项

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BOT_TOKEN` | 必填 | BotFather 提供的机器人 Token |
| `TG_API_ID` | 必填 | my.telegram.org 提供的 API ID |
| `TG_API_HASH` | 必填 | my.telegram.org 提供的 API Hash |
| `ADMIN_IDS` | 必填 | 管理员数字 ID，多个用逗号分隔 |
| `THUMB_WIDTH` | `1920` | 输出 PNG 宽度 |
| `MAX_CONCURRENT_JOBS` | `1` | 同时处理的视频数量 |
| `FFMPEG_TIMEOUT` | `90` | 单次 FFprobe / FFmpeg 超时秒数 |
| `RANGE_CACHE_MB` | `128` | Range 内存缓存上限；淘汰分块仍保留在任务级临时磁盘缓存 |
| `MAX_SOURCE_FETCH_MB` | `256` | 大视频单任务绝对读取硬上限 |
| `MAX_SOURCE_FETCH_RATIO` | `0.35` | 大视频初始读取预算比例 |
| `HARD_SOURCE_FETCH_RATIO` | `0.55` | 大视频最大读取比例硬上限 |
| `MIN_SOURCE_FETCH_MB` | `32` | 大视频初始读取预算下限 |
| `SMALL_FILE_FULL_READ_MB` | `64` | 小文件兼容阈值，范围读取可按需扩展到整个文件 |
| `SOURCE_FETCH_GROWTH_MB` | `16` | 预算每次自适应增加的容量 |
| `MT_PROXY_URL` | 空 | MTProto HTTP / SOCKS5 代理 |
| `DEBIAN_MIRROR` | 空 | Docker 构建 Debian 镜像地址 |
| `PIP_INDEX_URL` | `https://pypi.org/simple` | Docker 构建 PyPI 地址 |

`MAX_CONCURRENT_JOBS` 建议从 `1` 开始。多个任务会同时占用 Telegram 连接、内存和 CPU。

## 读取预算

文件不超过 `SMALL_FILE_FULL_READ_MB` 时，读取硬上限等于文件大小。机器人仍按需请求分块，并不会一开始就下载整个文件。

大视频初始预算：

```text
min(
  文件大小,
  MAX_SOURCE_FETCH_MB,
  max(MIN_SOURCE_FETCH_MB, 文件大小 * MAX_SOURCE_FETCH_RATIO)
)
```

大视频硬上限：

```text
min(
  文件大小,
  MAX_SOURCE_FETCH_MB,
  max(SMALL_FILE_FULL_READ_MB, 文件大小 * HARD_SOURCE_FETCH_RATIO)
)
```

读取达到初始预算后，只在 FFmpeg 仍请求新分块时按 `SOURCE_FETCH_GROWTH_MB` 逐步扩展，直到硬上限。默认情况下：

- 64MB 以内的小文件可以按需读取完整范围，兼容索引在末尾或关键帧稀疏的视频。
- 大视频初始预算为 35%，可按需提升至 55%。
- 无论比例如何，大视频最多读取 256MB。
- 已下载分块即使从 128MB 内存缓存中淘汰，也会从临时磁盘复用，不重复计入网络读取量。
- 达到硬上限后任务主动停止并向用户显示实际读取量。

视频时长并不决定读取量。码率、封装索引位置和关键帧间隔更直接影响随机截帧所需的数据。

## 持久化数据

Docker Compose 将本地 `./data` 挂载到容器 `/data`：

- `data/access.json`：授权用户
- `data/preferences.json`：每用户布局和发送方式

这些文件包含用户 ID，已被 `.gitignore` 排除，不应提交到公开仓库。

## 本地运行

需要：

- Python 3.12+
- FFmpeg 和 FFprobe

安装依赖：

```bash
python -m pip install -r requirements.txt
```

Linux / macOS：

```bash
export BOT_TOKEN="..."
export TG_API_ID="..."
export TG_API_HASH="..."
export ADMIN_IDS="123456789"
export ACCESS_FILE="./data/access.json"
export PREFERENCES_FILE="./data/preferences.json"
python -m app.bot
```

Windows PowerShell：

```powershell
$env:BOT_TOKEN="..."
$env:TG_API_ID="..."
$env:TG_API_HASH="..."
$env:ADMIN_IDS="123456789"
$env:ACCESS_FILE="./data/access.json"
$env:PREFERENCES_FILE="./data/preferences.json"
python -m app.bot
```

## 测试

本地：

```bash
python -m unittest discover -s tests -v
```

使用已构建镜像：

```bash
docker run --rm \
  -v "$PWD:/src" \
  -w /src \
  vthumb-telegram-bot \
  python -m unittest discover -s tests -v
```

## 常见问题

### 机器人无法启动

检查：

```bash
docker compose logs --tail 200
```

重点确认 `BOT_TOKEN`、`TG_API_ID`、`TG_API_HASH` 和 `ADMIN_IDS` 已填写。

### MTProto 连接反复断开

部分 HTTP 代理会关闭空闲 TCP 连接，Telethon 通常会自动重连。如果持续无法恢复，请检查代理端口、Docker 网络访问和代理软件的局域网权限。

### 提示达到分块读取上限

说明视频在随机定位时需要读取较多内容。可以适当提高：

```dotenv
HARD_SOURCE_FETCH_RATIO=0.65
MAX_SOURCE_FETCH_MB=512
```

也可以仅提高小文件兼容阈值：

```dotenv
SMALL_FILE_FULL_READ_MB=96
```

提高限制会增加网络和临时磁盘用量，不建议直接取消大视频硬上限。

### 图片在 Telegram 中变模糊

执行 `/setting`，选择“文件发送（原图）”。媒体发送会被 Telegram 压缩。

### 命令菜单没有刷新

重新进入机器人聊天或重启 Telegram 客户端。机器人每次启动都会重新同步完整命令菜单。

## 安全建议

- 不要提交 `.env`。
- 不要提交 `data/` 和 `debug-logs/`。
- Bot Token 和 API Hash 泄露后应立即更换。
- 建议只开放给可信用户，避免公开机器人消耗大量网络和 CPU。
- 公开部署时建议限制容器资源并监控日志。

## 项目结构

```text
app/
  access_control.py    # 管理员和用户白名单
  bot.py               # Telegram 事件、命令、进度和任务流程
  mtproto_range.py     # MTProto 分块读取与 HTTP Range 服务
  settings.py          # 环境变量
  user_preferences.py  # 每用户布局和发送方式
  vthumb.py            # FFmpeg 截帧与 PNG 合成
tests/
Dockerfile
docker-compose.yml
.env.example
```

## License

[MIT](LICENSE)
