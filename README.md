# VThumb Telegram Bot

一个可 Docker 部署的 Telegram 视频缩略图机器人。它通过 MTProto 按需读取视频分块，使用 FFprobe / FFmpeg 远程定位画面，再由 Pillow 生成带媒体信息和时间戳的视频联系表。

机器人不会先把完整源视频保存到磁盘。针对大视频还提供可配置的读取预算，达到上限后主动停止，避免任务退化为全量下载。

## 功能

- 普通视频和“作为文件发送”的视频均可处理
- MTProto 分块读取，不受 Bot API `getFile` 的 20MB 下载限制
- 512KB 对齐的 HTTP Range 读取
- 有上限的 LRU 内存缓存，重复 Range 请求不会重复下载
- 大视频读取比例和绝对流量双重限制
- PotPlayer 风格 PNG 联系表
- 文件名、文件大小、分辨率、帧率、视频/音频编码、时长
- 第一帧提前取样，减少第一张画面过晚的问题
- 横屏、竖屏和旋转视频自适应
- 处理、截帧、合成和上传进度实时更新
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
       +--> 每任务读取预算
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

源视频不会写入磁盘。磁盘中只会短暂出现单张 JPEG 帧和最终 PNG，任务结束后自动删除。

## 默认设置

新用户默认使用：

- `16帧 4x4`
- `媒体发送（Telegram 会压缩）`
- 输出宽度 `1920px`

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

所有选择均按 Telegram 用户 ID 单独保存，不影响其他用户。

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
| `RANGE_CACHE_MB` | `128` | 每个运行实例的 Range 内存缓存上限 |
| `MAX_SOURCE_FETCH_MB` | `256` | 单任务最大源视频读取量 |
| `MAX_SOURCE_FETCH_RATIO` | `0.35` | 大视频最大读取比例 |
| `MIN_SOURCE_FETCH_MB` | `32` | 小视频最低读取预算 |
| `MT_PROXY_URL` | 空 | MTProto HTTP / SOCKS5 代理 |
| `DEBIAN_MIRROR` | 空 | Docker 构建 Debian 镜像地址 |
| `PIP_INDEX_URL` | `https://pypi.org/simple` | Docker 构建 PyPI 地址 |

`MAX_CONCURRENT_JOBS` 建议从 `1` 开始。多个任务会同时占用 Telegram 连接、内存和 CPU。

## 读取预算

单任务预算计算方式：

```text
min(
  文件大小,
  MAX_SOURCE_FETCH_MB,
  max(MIN_SOURCE_FETCH_MB, 文件大小 * MAX_SOURCE_FETCH_RATIO)
)
```

默认情况下：

- 小文件可能允许读取完整内容，以保证兼容索引位于文件末尾的视频。
- 大文件最多读取 35%，且不会超过 256MB。
- 达到预算后任务主动停止并向用户提示。

这能限制大视频流量，但也意味着关键帧极少、索引结构特殊或不利于随机读取的视频可能无法完成所有截图。

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
MAX_SOURCE_FETCH_RATIO=0.5
MAX_SOURCE_FETCH_MB=512
```

提高限制会增加网络流量和内存缓存压力，不建议直接取消预算。

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
