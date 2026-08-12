import asyncio
import contextlib
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from telethon import Button, TelegramClient, events, functions, types
from telethon.sessions import MemorySession

from .access_control import AccessControl
from .mtproto_range import FetchBudgetExceeded, MTProtoRangeServer
from .settings import Settings
from .user_preferences import (
    DELIVERY_FILE,
    DELIVERY_MEDIA,
    PRESETS,
    PRESETS_BY_KEY,
    THEME_LABELS,
    THEMES,
    ThumbnailPreset,
    UserPreferences,
)
from .vthumb import (
    THEME_BY_NAME,
    SourceInfo,
    create_contact_sheet,
)


VIDEO_MIME_PREFIX = "video/"
BOT_COMMANDS = (
    ("start", "开始使用并查看视频发送方式"),
    ("help", "查看使用说明和自适应读取策略"),
    ("status", "查看缩略图设置和读取预算"),
    ("setting", "选择每个用户独立的缩略图预设"),
    ("id", "查看自己的 Telegram 用户 ID"),
    ("add", "管理员：添加授权用户"),
    ("del", "管理员：删除授权用户"),
    ("users", "管理员：查看授权用户"),
    ("merge", "合并接下来 N 条媒体 (默认 2)，图片在前视频在后"),
)

# ── /merge feature constants ─────────────────────────────────────────
MERGE_DEFAULT_COUNT = 2          # bare /merge picks this many
MERGE_MAX_COUNT     = 20         # Telegram media_group caps at 10 per batch
MERGE_MIN_COUNT     = 1
MERGE_DEBOUNCE_SEC  = 5.0        # wait this long after last media before firing
MERGE_LOG_PREFIX    = "[merge]"
SUPPORTED_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".asf",
    ".avi",
    ".divx",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ogm",
    ".ogv",
    ".rm",
    ".rmvb",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
}


def extract_video(message: Any) -> dict[str, Any] | None:
    document = message.document
    if not document:
        return None

    file_info = message.file
    file_name = getattr(file_info, "name", None) or "telegram-video.mp4"
    mime_type = getattr(file_info, "mime_type", None) or getattr(document, "mime_type", "") or ""
    if message.video or mime_type.startswith(VIDEO_MIME_PREFIX) or Path(file_name).suffix.lower() in SUPPORTED_EXTENSIONS:
        return {
            "media": document,
            "file_name": file_name,
            "file_size": getattr(file_info, "size", None) or getattr(document, "size", None),
        }
    return None


def command_name(text: str | None) -> str | None:
    if not text or not text.startswith("/"):
        return None
    command = text.split(None, 1)[0][1:].split("@", 1)[0].lower()
    return command or None


def permission_message(user_id: int | None) -> str:
    return f"你还没有使用权限。\n你的 Telegram ID：{user_id or '未知'}\n请将此 ID 提供给管理员添加。"


def command_user_id(text: str) -> int | None:
    parts = text.split()
    if len(parts) != 2:
        return None
    try:
        user_id = int(parts[1])
    except ValueError:
        return None
    return user_id if user_id > 0 else None


def delivery_label(delivery: str) -> str:
    return "文件发送（原图）" if delivery == DELIVERY_FILE else "媒体发送（TG压缩）"


def setting_text(preset: ThumbnailPreset, delivery: str, theme: str) -> str:
    return (
        "缩略图设置（横x竖）\n"
        f"当前布局：{preset.label}\n"
        f"发送方式：{delivery_label(delivery)}\n"
        f"主题风格：{THEME_LABELS.get(theme, theme)}\n\n"
        "提示：文件发送保留原图；媒体发送会被 Telegram 压缩。\n"
        "点击下方按钮切换，只影响你自己的任务。"
    )


def setting_buttons(current: ThumbnailPreset, delivery: str, theme: str) -> list[list[Button]]:
    buttons = []
    for preset in PRESETS:
        marker = "✓ " if preset == current else ""
        buttons.append(
            Button.inline(
                marker + preset.label,
                data=f"setting:{preset.count}:{preset.cols}".encode("ascii"),
            )
        )
    media_marker = "✓ " if delivery == DELIVERY_MEDIA else ""
    file_marker = "✓ " if delivery == DELIVERY_FILE else ""
    delivery_buttons = [
        Button.inline(media_marker + "媒体发送（TG压缩）", data=b"delivery:media"),
        Button.inline(file_marker + "文件发送（原图）", data=b"delivery:file"),
    ]
    # Theme rows: 2 buttons per row, chunked automatically so adding a new theme
    # (4 -> 6 themes so far) doesn't require a layout rewrite. Callbacks are
    # `theme:<theme-name>` decoded by `theme_from_callback`.
    theme_buttons: list[Button] = []
    for theme_name in THEMES:
        marker = "✓ " if theme_name == theme else ""
        theme_buttons.append(
            Button.inline(
                marker + THEME_LABELS[theme_name],
                data=f"theme:{theme_name}".encode("ascii"),
            )
        )
    theme_rows = [theme_buttons[i : i + 2] for i in range(0, len(theme_buttons), 2)]
    return [
        buttons[:2],
        buttons[2:],
        delivery_buttons,
        *theme_rows,
    ]


def setting_from_callback(data: bytes) -> ThumbnailPreset | None:
    try:
        prefix, count, cols = data.decode("ascii").split(":", 2)
        if prefix != "setting":
            return None
        return PRESETS_BY_KEY.get((int(count), int(cols)))
    except (UnicodeDecodeError, ValueError):
        return None


def delivery_from_callback(data: bytes) -> str | None:
    try:
        prefix, delivery = data.decode("ascii").split(":", 1)
    except (UnicodeDecodeError, ValueError):
        return None
    if prefix != "delivery" or delivery not in {DELIVERY_MEDIA, DELIVERY_FILE}:
        return None
    return delivery


def theme_from_callback(data: bytes) -> str | None:
    try:
        prefix, theme = data.decode("ascii").split(":", 1)
    except (UnicodeDecodeError, ValueError):
        return None
    if prefix != "theme" or theme not in THEME_BY_NAME:
        return None
    return theme


async def handle_command(
    client: TelegramClient,
    settings: Settings,
    access: AccessControl,
    preferences: UserPreferences,
    message: Any,
) -> bool:
    command = command_name(message.raw_text)
    if command is None:
        return False

    user_id = message.sender_id
    is_admin = access.is_admin(user_id)
    is_allowed = access.is_allowed(user_id)
    buttons = None

    if command == "start":
        if is_allowed:
            text = (
                "发送视频或以文件形式发送视频，我会按需读取分块并生成缩略图。\n"
                "不会预先完整下载大视频。\n\n使用 /help 查看详细说明。"
            )
        else:
            text = permission_message(user_id)
    elif command == "help":
        if not is_allowed:
            text = permission_message(user_id) + "\n\n使用 /id 可再次查看 ID。"
        else:
            text = (
                "使用说明：\n"
                "1. 直接发送视频，或将视频作为文件发送。\n"
                "2. 使用 /setting 选择帧数与横竖布局。\n"
                f"3. {settings.small_file_full_read_mb}MB 以内按需读取，必要时可读取完整范围。\n"
                f"4. 大视频初始读取预算 {settings.max_source_fetch_ratio:.0%}，"
                f"按需提升至 {settings.hard_source_fetch_ratio:.0%}，"
                f"且不超过 {settings.max_source_fetch_mb}MB。\n"
                "5. 已读取分块会临时缓存，任务结束后自动删除。"
            )
            if is_admin:
                text += "\n\n管理员命令：\n/add 用户ID\n/del 用户ID\n/users"
    elif command == "id":
        text = f"你的 Telegram ID：{user_id or '未知'}"
    elif command == "status":
        if not is_allowed:
            text = permission_message(user_id)
        else:
            preset = preferences.get(user_id)
            delivery = preferences.get_delivery(user_id)
            text = (
                "运行状态：正常\n"
                f"缩略图：{preset.label} / {settings.thumb_width}px\n"
                f"发送方式：{delivery_label(delivery)}\n"
                f"分块缓存：{settings.range_cache_mb}MB 内存 + 临时磁盘去重\n"
                f"小文件阈值：{settings.small_file_full_read_mb}MB\n"
                f"大视频预算：初始 {settings.max_source_fetch_ratio:.0%}，"
                f"按需至 {settings.hard_source_fetch_ratio:.0%}，"
                f"最高 {settings.max_source_fetch_mb}MB"
            )
    elif command == "setting":
        if not is_allowed:
            text = permission_message(user_id)
        else:
            preset = preferences.get(user_id)
            delivery = preferences.get_delivery(user_id)
            theme = preferences.get_theme(user_id)
            text = setting_text(preset, delivery, theme)
            buttons = setting_buttons(preset, delivery, theme)
    elif command in {"add", "del", "users"}:
        if not is_admin:
            text = "此命令仅限管理员使用。"
        elif command == "users":
            users = access.users()
            text = "管理员：" + ", ".join(str(item) for item in sorted(access.admin_ids))
            text += "\n授权用户：" + (", ".join(str(item) for item in users) if users else "暂无")
        else:
            target_id = command_user_id(message.raw_text)
            if target_id is None:
                text = f"格式错误，请使用 /{command} 用户ID"
            elif command == "add":
                added = await access.add(target_id)
                text = f"已添加用户：{target_id}" if added else f"用户已在授权名单中：{target_id}"
            elif target_id in access.admin_ids:
                text = "管理员由环境变量配置，不能通过 /del 删除。"
            else:
                removed = await access.remove(target_id)
                text = f"已删除用户：{target_id}" if removed else f"用户不在授权名单中：{target_id}"
    elif command == "merge":
        # /merge [N | cancel | status]
        await _merge_handle(client, settings, access, preferences, message)
        return True
    else:
        text = "未知命令，请使用 /help 查看可用命令。"
    await client.send_message(message.chat_id, text, reply_to=message.id, buttons=buttons)
    return True


async def _merge_handle(
    client: TelegramClient,
    settings: Settings,
    access: AccessControl,
    preferences: UserPreferences,
    message: Any,
) -> None:
    """Parse /merge [N | cancel | status]. All sub-commands in one
    handler to keep state co-located. Called from handle_command."""
    user_id = message.sender_id
    # Allow any authorized user + admin to use /merge. (Same gate as
    # /add etc. is admin-only; /merge is general utility.)
    if not access.is_allowed(user_id) and not access.is_admin(user_id):
        await client.send_message(
            message.chat_id,
            permission_message(user_id),
            reply_to=message.id,
        )
        return
    raw = (message.raw_text or "").split()
    arg = raw[1].lower() if len(raw) > 1 else ""

    # ── cancel ──
    if arg == "cancel":
        state = _pending_merge.pop(user_id, None)
        if state is None:
            return await client.send_message(
                message.chat_id, "ℹ️ 没有等待中的合并任务。", reply_to=message.id,
            )
        if state["task"] and not state["task"].done():
            state["task"].cancel()
        return await client.send_message(
            message.chat_id, "✓ 已取消当前合并任务。", reply_to=message.id,
        )

    # ── status ──
    if arg == "status":
        state = _pending_merge.get(user_id)
        if not state:
            return await client.send_message(
                message.chat_id, "ℹ️ 当前没有等待中的合并任务。", reply_to=message.id,
            )
        remain = state["expected_count"] - state["received_count"]
        return await client.send_message(
            message.chat_id,
            f"⏱ {state['received_count']}/{state['expected_count']} 已收, 还差 {remain} 条。\n"
            f"5 秒静默后开始合并。\n"
            f"取消: /merge cancel",
            reply_to=message.id,
        )

    # ── set N (or default) ──
    if user_id in _pending_merge:
        return await client.send_message(
            message.chat_id,
            "⚠️ 你已经有等待中的合并任务。\n"
            "先发完或发送 /merge cancel 取消。",
            reply_to=message.id,
        )
    if arg == "":
        n = MERGE_DEFAULT_COUNT
    else:
        try:
            n = int(arg)
        except ValueError:
            return await client.send_message(
                message.chat_id,
                f"✗ 用法: /merge [N | cancel | status]\n"
                f"  N 范围: {MERGE_MIN_COUNT}-{MERGE_MAX_COUNT}。",
                reply_to=message.id,
            )
        if n < MERGE_MIN_COUNT or n > MERGE_MAX_COUNT:
            return await client.send_message(
                message.chat_id,
                f"✗ N 必须是 {MERGE_MIN_COUNT}-{MERGE_MAX_COUNT} 之间的整数。",
                reply_to=message.id,
            )

    state = {
        "user_id": user_id,
        "chat_id": message.chat_id,
        "expected_count": n,
        "collected": [],
        "task": None,
        "started_at": asyncio.get_event_loop().time(),
        "notify_msg_id": None,
    }
    _pending_merge[user_id] = state

    notify = await client.send_message(
        message.chat_id,
        f"✓ 模式开启, 等待接下来 {n} 条媒体消息。\n"
        f"  支持: 图片 / 视频 / 动图 / 圆视频。\n"
        f"  图片会排在前, 视频排在后, 自带的说明文字会被去除。\n"
        f"  5 秒静默后开始合并。\n"
        f"  取消: /merge cancel",
        reply_to=message.id,
    )
    state["notify_msg_id"] = notify.id


async def _merge_collect_media(
    client: TelegramClient,
    settings: Settings,
    access: AccessControl,
    preferences: UserPreferences,
    message: Any,
) -> bool:
    """Called from on_message after handle_command returns False. If
    this user has an active merge session AND the message contains
    media, count it toward the session and return True (so the
    caller skips process_message).

    Returns False if no merge is active OR the message has no media.
    """
    user_id = message.sender_id
    state = _pending_merge.get(user_id)
    if not state:
        return False  # no merge active; let the normal flow continue

    # Identify media type from Telethon message attributes.
    kind = _merge_classify(message)
    if not kind:
        return False  # not media; let normal flow continue (text/etc.)
    state["collected"].append(message)

    recv = len(state["collected"])
    expected = state["expected_count"]
    logging.info(
        f"{MERGE_LOG_PREFIX} uid={user_id} got media {recv}/{expected} (kind={kind})"
    )

    if recv < expected:
        # Still collecting -- restart the debounce, ask for the next one.
        _merge_schedule_debounce(
            state,
            lambda: _merge_fire(client, state, user_id),
        )
        await client.send_message(
            message.chat_id,
            f"⏱ {recv}/{expected}, 等待第 {recv + 1} 条...",
            reply_to=message.id,
        )
        return True

    # Got all N -- finalization debounce.
    _merge_schedule_debounce(
        state,
        lambda: _merge_fire(client, state, user_id),
    )
    await client.send_message(
        message.chat_id,
        f"⏱ {recv}/{expected}, 5 秒后开始合并...",
        reply_to=message.id,
    )
    return True


def _merge_classify(message: Any) -> str:
    """Return 'photo', 'animation', 'video', 'video_note', or ''."""
    if getattr(message, "video", None):
        return "video"
    if getattr(message, "video_note", None):
        return "video_note"
    if getattr(message, "animation", None):
        return "animation"
    if getattr(message, "photo", None):
        return "photo"
    return ""


def _merge_schedule_debounce(state: dict, on_fire) -> None:
    """Cancel any previous timer, schedule a new 5-second one."""
    if state.get("task") and not state["task"].done():
        state["task"].cancel()
    async def _wait():
        try:
            await asyncio.sleep(MERGE_DEBOUNCE_SEC)
        except asyncio.CancelledError:
            return
        try:
            await on_fire()
        except Exception as e:
            logging.exception(f"{MERGE_LOG_PREFIX} debounce fire failed: {e}")
    state["task"] = asyncio.create_task(_wait())


async def _merge_fire(client: TelegramClient, state: dict, user_id: int) -> None:
    """Debounce fired -- finalize this user's pending merge."""
    # Pop the state atomically so concurrent /merge can't see it.
    current = _pending_merge.pop(user_id, None)
    if current is None:
        return  # already cancelled
    state = current  # use the popped state

    chat_id = state["chat_id"]
    msgs = state["collected"]
    photos = [m for m in msgs if _merge_classify(m) in ("photo", "animation")]
    videos = [m for m in msgs if _merge_classify(m) in ("video", "video_note")]

    # Build Telethon InputMedia objects, photos first, videos last,
    # using only file_id references -- no downloads, no disk I/O.
    #
    # Telethon 1.40 doesn't export `InputMediaVideo` in `telethon.tl.types`
    # -- `Message.video` is a `Document`, and we send it as
    # `InputMediaDocument` which Telethon will render as a video in
    # a media_group (the `video_cover` and `video_timestamp` params
    # are only needed for round video_notes).
    #
    # `InputMediaPhoto` and `InputMediaDocument` constructors accept
    # `Input*` TL types (id/access_hash/file_reference). `Message.photo`
    # is a `Photo` and `Message.video` is a `Document` -- distinct from
    # the Input* types. We re-wrap them into the Input* form.
    #
    # Caption stripping is automatic: when we re-wrap from a Photo/
    # Document, we don't carry over the original message's text
    # caption -- Telegram renders the album without any text.
    from telethon.tl.types import (
        InputMediaPhoto,
        InputMediaDocument,
        InputPhoto,
        InputDocument,
    )
    media: list = []
    for m in photos:
        if _merge_classify(m) == "photo":
            ph = m.photo
            input_photo = InputPhoto(ph.id, ph.access_hash, ph.file_reference)
            media.append(InputMediaPhoto(input_photo))
        else:  # animation
            doc = m.animation
            input_doc = InputDocument(doc.id, doc.access_hash, doc.file_reference)
            media.append(InputMediaDocument(input_doc))
    for m in videos:
        if _merge_classify(m) == "video":
            doc = m.video
            input_doc = InputDocument(doc.id, doc.access_hash, doc.file_reference)
            media.append(InputMediaDocument(input_doc))
        else:  # video_note
            doc = m.video_note
            input_doc = InputDocument(doc.id, doc.access_hash, doc.file_reference)
            media.append(InputMediaDocument(input_doc))

    if not media:
        await client.send_message(
            chat_id,
            "✗ 没有可合并的媒体 (全部 file_id 缺失)。请重新 /merge。",
        )
        return

    logging.info(
        f"{MERGE_LOG_PREFIX} uid={user_id} firing: "
        f"{len(photos)} photo/animation + {len(videos)} video = {len(media)} items"
    )

    # Telegram caps media_group at 10 items; chunk if needed.
    BATCH = 10
    sent = 0
    failed_indices: list[int] = []
    for batch_start in range(0, len(media), BATCH):
        batch = media[batch_start:batch_start + BATCH]
        try:
            await client.send_file(
                chat_id,
                batch,
                force_document=False,
            )
            sent += len(batch)
        except Exception as e:
            err = str(e).lower()
            logging.warning(f"{MERGE_LOG_PREFIX} uid={user_id} batch failed: {e}")
            if any(tok in err for tok in (
                "file_id_invalid", "media_invalid", "file reference",
                "invalid file", "wrong file id", "media_empty",
                "400 bad_request",
            )):
                for i in range(len(batch)):
                    failed_indices.append(batch_start + i + 1)
            else:
                await client.send_message(
                    chat_id,
                    f"⚠️ 第 {batch_start + 1}-{batch_start + len(batch)} 条合并失败: {e}",
                )
                continue

    summary = [f"✓ 已合并 {state['expected_count']} 条 ({len(photos)} 图 + {len(videos)} 视频)"]
    if sent:
        summary.append(f"  成功发送 {sent}/{len(media)} 条")
    if failed_indices:
        idx_list = ", ".join(str(i) for i in failed_indices)
        summary.append("")
        summary.append(f"✗ 第 {idx_list} 条 file_id 已失效 (Telegram 缓存已清理)。")
        summary.append("  请在 Telegram 长按原消息 → 引用回复 → 把那条转发给我。")
        summary.append("  然后重新发送 /merge 即可。")
    try:
        await client.send_message(chat_id, "\n".join(summary))
    except Exception:
        pass

    # Best-effort: delete the original "OK waiting" reply so the chat
    # is tidy.
    notify_id = state.get("notify_msg_id")
    if notify_id is not None:
        try:
            await client.delete_messages(chat_id, [notify_id])
        except Exception:
            pass


# Module-level merge state. One pending merge per user at a time.
# Dict keyed by sender_id, value is a dict (see _merge_handle).
_pending_merge: dict[int, dict] = {}


async def set_bot_commands(client: TelegramClient) -> None:
    await client(
        functions.bots.SetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(),
            lang_code="",
            commands=[types.BotCommand(command=name, description=description) for name, description in BOT_COMMANDS],
        )
    )


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: list[str]) -> None:
        super().__init__(fmt)
        self.secrets = [secret for secret in secrets if secret]

    def format(self, record: logging.LogRecord) -> str:
        output = super().format(record)
        for secret in self.secrets:
            output = output.replace(secret, "<BOT_TOKEN_REDACTED>")
        return output


def configure_logging(token: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s", [token]))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def format_progress(stage: str, current: int, total: int) -> str:
    labels = {
        "metadata": "正在读取视频信息",
        "frames": "正在截取视频帧",
        "compose": "正在合成缩略图",
        "upload": "正在上传成品图片",
    }
    label = labels.get(stage, "正在处理")
    ratio = min(1.0, max(0.0, current / total)) if total > 0 else 0.0
    filled = round(ratio * 12)
    bar = "#" * filled + "-" * (12 - filled)
    percent = round(ratio * 100)
    detail = f" {current}/{total}" if stage == "frames" and total > 0 else ""
    return f"{label}{detail}\n[{bar}] {percent}%"


class ProgressReporter:
    def __init__(self, client: TelegramClient, chat_id: int, message_id: int) -> None:
        self.client = client
        self.chat_id = chat_id
        self.message_id = message_id
        self.loop = asyncio.get_running_loop()
        self.state: tuple[str, int, int] = ("metadata", 0, 1)
        self.last_text = ""
        self.dirty = True
        self.task = asyncio.create_task(self._run())

    @classmethod
    async def create(cls, client: TelegramClient, chat_id: int, reply_to: int) -> "ProgressReporter":
        message = await client.send_message(
            chat_id,
            "收到，准备按需读取视频分块。大视频不会预先完整下载。",
            reply_to=reply_to,
        )
        return cls(client, chat_id, message.id)

    def report(self, stage: str, current: int | float, total: int | float) -> None:
        try:
            self.loop.call_soon_threadsafe(self._set_state, stage, int(current), int(total))
        except RuntimeError:
            pass

    def _set_state(self, stage: str, current: int, total: int) -> None:
        self.state = (stage, current, total)
        self.dirty = True

    async def show_now(self, stage: str, current: int, total: int) -> None:
        self._set_state(stage, current, total)
        await self._flush()

    async def finish(self, text: str) -> None:
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        await self._edit(text)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(1.2)
            await self._flush()

    async def _flush(self) -> None:
        if not self.dirty:
            return
        self.dirty = False
        await self._edit(format_progress(*self.state))

    async def _edit(self, text: str) -> None:
        if text == self.last_text:
            return
        try:
            await self.client.edit_message(self.chat_id, self.message_id, text)
            self.last_text = text
        except Exception as exc:
            logging.warning("Could not update progress message (%s)", type(exc).__name__)


def user_error_message(exc: Exception) -> str:
    if isinstance(exc, FetchBudgetExceeded):
        fetched_mb = exc.fetched / 1024 / 1024
        limit_mb = exc.budget / 1024 / 1024
        return (
            f"这个视频的索引或关键帧不利于随机截取，已读取 {fetched_mb:.1f}MB，"
            f"达到自适应硬上限 {limit_mb:.1f}MB。为避免接近全量下载，任务已主动停止。"
        )
    if isinstance(exc, subprocess.TimeoutExpired):
        return "截帧超时，请稍后重试，或提高 FFMPEG_TIMEOUT。"
    if isinstance(exc, subprocess.CalledProcessError):
        # Surface the actual ffmpeg error to the user instead of a generic message.
        # ffmpeg's stderr/stdout was captured but never logged previously -- the
        # catch was swallowing the diagnostic. Embed a short excerpt in the user
        # message; the full stderr still goes to container logs via the caller.
        err = (exc.stderr or "").strip()
        if not err and exc.stdout:
            err = (exc.stdout or "").strip()
        # Telegram caps outgoing text at 4096 bytes; budget for ~600 chars of
        # ffmpeg-output plus a short prefix so the message still fits.
        excerpt = err[:600].replace("\r\n", "\n")
        suffix = (f"\n\nffmpeg 输出：\n{excerpt}" if excerpt else
                  f"\n\n（未捕获到 ffmpeg 错误输出，请查看容器日志）")
        return (
            "无法读取或截取这个视频。"
            f"可能是格式、远程读取或代理连接问题。{suffix}"
        )
    return "生成失败，请查看容器日志中的错误类型。"


async def process_message(
    client: TelegramClient,
    media_server: MTProtoRangeServer,
    settings: Settings,
    preferences: UserPreferences,
    semaphore: asyncio.Semaphore,
    message: Any,
) -> None:
    chat_id = message.chat_id
    message_id = message.id
    video = extract_video(message)
    if not video:
        await client.send_message(chat_id, "请发送视频文件，或把视频作为 document 发送给我。", reply_to=message_id)
        return

    async with semaphore:
        preset = preferences.get(message.sender_id)
        delivery = preferences.get_delivery(message.sender_id)
        theme = preferences.get_theme(message.sender_id)
        tmp_dir = Path(tempfile.mkdtemp(prefix="vthumb_bot_"))
        range_key: str | None = None
        progress: ProgressReporter | None = None
        try:
            progress = await ProgressReporter.create(client, chat_id, message_id)
            file_size = video.get("file_size")
            if not isinstance(file_size, int) or file_size <= 0:
                raise RuntimeError("Telegram did not provide the video file size.")
            filename = video.get("file_name") or "telegram-video.mp4"
            range_key, file_url = media_server.register(
                video["media"],
                file_size,
                Path(filename).name,
                settings.source_fetch_budget(file_size),
                hard_budget=settings.source_fetch_hard_budget(file_size),
                budget_growth=settings.source_fetch_growth_mb * 1024 * 1024,
            )
            output = tmp_dir / f"{Path(filename).name}.png"
            source = SourceInfo(
                url=file_url,
                filename=Path(filename).name,
                size=video.get("file_size"),
            )
            await asyncio.to_thread(
                create_contact_sheet,
                source,
                output,
                count=preset.count,
                cols=preset.cols,
                width=settings.thumb_width,
                timeout=settings.ffmpeg_timeout,
                theme=THEME_BY_NAME[theme],
                progress_callback=progress.report,
            )
            output_size = output.stat().st_size
            await progress.show_now("upload", 0, output_size)
            await client.send_file(
                chat_id,
                str(output),
                caption=f"{source.filename}.png",
                force_document=delivery == DELIVERY_FILE,
                reply_to=message_id,
                parse_mode=None,
                progress_callback=lambda current, total: progress.report("upload", current, total),
            )
            await progress.finish("处理完成，缩略图已发送。")
        except Exception as exc:
            if range_key:
                exc = media_server.failure(range_key) or exc
            logging.exception("Failed to process message %s", message_id)
            # When ffmpeg (or another subprocess) failed, log the captured stderr
            # at WARNING level so the operator sees it in `docker logs` immediately.
            # `logging.exception` already records the traceback, this is the
            # *content* of the failed subprocess output (which is otherwise swallowed).
            stderr_text = getattr(exc, "stderr", None) if isinstance(exc, subprocess.CalledProcessError) else None
            if stderr_text:
                logging.warning(
                    "ffmpeg/subprocess stderr for message %s (exit=%s): %s",
                    message_id,
                    getattr(exc, "returncode", "?"),
                    stderr_text,
                )
            try:
                error_text = "处理失败：" + user_error_message(exc)
                if progress:
                    await progress.finish(error_text)
                else:
                    await client.send_message(chat_id, error_text, reply_to=message_id)
            except Exception as reply_exc:
                logging.error(
                    "Could not send failure message for %s (%s)",
                    message_id,
                    type(reply_exc).__name__,
                )
        finally:
            if range_key:
                media_server.unregister(range_key)
            shutil.rmtree(tmp_dir, ignore_errors=True)


async def poll() -> None:
    settings = Settings.from_env()
    configure_logging(settings.bot_token)
    access = AccessControl(Path(settings.access_file), settings.admin_ids)
    preferences = UserPreferences(Path(settings.preferences_file))
    mt_client = TelegramClient(
        MemorySession(),
        settings.api_id,
        settings.api_hash,
        proxy=settings.mt_proxy,
        receive_updates=True,
    )
    media_server = MTProtoRangeServer(
        mt_client,
        cache_bytes=settings.range_cache_mb * 1024 * 1024,
    )
    semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
    tasks: set[asyncio.Task[None]] = set()

    @mt_client.on(events.NewMessage(incoming=True))
    async def on_message(event: events.NewMessage.Event) -> None:
        if await handle_command(mt_client, settings, access, preferences, event.message):
            return
        # /merge short-circuit: if a merge session is active for this
        # sender, collect the media and DO NOT process_message() it.
        if await _merge_collect_media(
            mt_client, settings, access, preferences, event.message
        ):
            return
        if not access.is_allowed(event.sender_id):
            await mt_client.send_message(
                event.chat_id,
                permission_message(event.sender_id),
                reply_to=event.message.id,
            )
            return
        task = asyncio.create_task(
            process_message(mt_client, media_server, settings, preferences, semaphore, event.message)
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @mt_client.on(events.CallbackQuery(pattern=b"^(setting|delivery|theme):"))
    async def on_setting(event: events.CallbackQuery.Event) -> None:
        user_id = event.sender_id
        if not access.is_allowed(user_id):
            await event.answer("你没有使用权限。", alert=True)
            return
        if user_id is None:
            await event.answer("设置参数无效。", alert=True)
            return
        changed = False
        preset = preferences.get(user_id)
        delivery = preferences.get_delivery(user_id)
        theme = preferences.get_theme(user_id)
        if event.data.startswith(b"setting:"):
            selected = setting_from_callback(event.data)
            if selected is None:
                await event.answer("设置参数无效。", alert=True)
                return
            changed = selected != preset
            preset = await preferences.set(user_id, selected.count, selected.cols)
        elif event.data.startswith(b"theme:"):
            selected_theme = theme_from_callback(event.data)
            if selected_theme is None:
                await event.answer("主题无效。", alert=True)
                return
            changed = selected_theme != theme
            theme = await preferences.set_theme(user_id, selected_theme)
        else:
            selected_delivery = delivery_from_callback(event.data)
            if selected_delivery is None:
                await event.answer("发送方式无效。", alert=True)
                return
            changed = selected_delivery != delivery
            delivery = await preferences.set_delivery(user_id, selected_delivery)
        await event.answer("设置已保存。" if changed else "当前已是这个设置。")
        if changed:
            await event.edit(
                setting_text(preset, delivery, theme),
                buttons=setting_buttons(preset, delivery, theme),
            )

    try:
        await mt_client.start(bot_token=settings.bot_token)
        await set_bot_commands(mt_client)
        await media_server.start()
        logging.info("vthumb Telegram bot started with MTProto range reading.")
        await mt_client.run_until_disconnected()
    finally:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await media_server.stop()
        await mt_client.disconnect()


if __name__ == "__main__":
    asyncio.run(poll())
