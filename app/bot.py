import asyncio
import contextlib
import html
import json
import logging
import secrets
import shutil
import subprocess
import tempfile
import time
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
    queue: "JobQueue | None" = None,
    forward: "ForwardGate | None" = None,
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
                "5. 已读取分块会临时缓存，任务结束后自动删除。\n"
                "6. 多人同时发送会按到达顺序排队，轮到你时自动开始。"
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
                f"最高 {settings.max_source_fetch_mb}MB\n"
                f"任务队列：处理中 {queue.running if queue else 0}，"
                f"排队 {queue.waiting if queue else 0}"
                f"（并发 {queue.concurrency if queue else settings.max_concurrent_jobs}）"
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
    elif command == "forward":
        # Hidden admin toggle. Not listed in BOT_COMMANDS or /help.
        if not is_admin or forward is None:
            return True
        await _forward_handle_command(client, access, forward, message)
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


FORWARD_DEBOUNCE_SEC = 5.0
FORWARD_LOG_PREFIX = "[forward]"


class ForwardGate:
    """Hidden admin toggle. Default off. Not in /help or bot command menu."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.enabled = self._load()
        self.lock = asyncio.Lock()

    def _load(self) -> bool:
        if not self.path.exists():
            return False
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return bool(payload.get("enabled", False))
        except (OSError, ValueError, TypeError):
            return False

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"enabled": self.enabled}) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    async def set_enabled(self, enabled: bool) -> bool:
        async with self.lock:
            changed = self.enabled != enabled
            self.enabled = enabled
            self._save()
            return changed


def format_user_label(sender: Any, user_id: int | None) -> str:
    if sender is not None:
        username = getattr(sender, "username", None)
        if username:
            return f"@{username}"
        first = (getattr(sender, "first_name", None) or "").strip()
        last = (getattr(sender, "last_name", None) or "").strip()
        name = " ".join(part for part in (first, last) if part)
        if name:
            return name
    return str(user_id or "未知")


def format_forward_notice(label: str, user_id: int | None, count: int) -> str:
    safe = html.escape(label or str(user_id or "未知"), quote=True)
    if user_id:
        return (
            f"用户 {safe} "
            f'(<a href="tg://user?id={int(user_id)}">{int(user_id)}</a>) '
            f"完成 {count} 个任务"
        )
    return f"用户 {safe} 完成 {count} 个任务"


_pending_forward: dict[int, dict] = {}


async def _forward_handle_command(
    client: TelegramClient,
    access: AccessControl,
    gate: ForwardGate,
    message: Any,
) -> None:
    raw = (message.raw_text or "").split()
    arg = raw[1].lower() if len(raw) > 1 else ""
    if arg in {"on", "1", "true", "开"}:
        await gate.set_enabled(True)
        text = "管理员转发已开启。其他用户完成任务后会静默汇总给你。"
    elif arg in {"off", "0", "false", "关"}:
        await gate.set_enabled(False)
        text = "管理员转发已关闭。"
    else:
        state = "开" if gate.enabled else "关"
        text = f"转发当前：{state}\n用法：/forward on 或 /forward off"
    await client.send_message(message.chat_id, text, reply_to=message.id)


async def _forward_note_success(
    client: TelegramClient,
    access: AccessControl,
    gate: ForwardGate,
    message: Any,
    png_path: Path,
) -> None:
    if not gate.enabled:
        return
    user_id = message.sender_id
    if user_id is None or access.is_admin(user_id):
        return
    keep_dir = Path(tempfile.mkdtemp(prefix="vthumb_fwd_"))
    kept: Path | None = None
    try:
        dest = keep_dir / png_path.name
        shutil.copy2(png_path, dest)
        kept = dest
    except OSError as exc:
        logging.warning("%s copy sheet failed: %s", FORWARD_LOG_PREFIX, exc)
        shutil.rmtree(keep_dir, ignore_errors=True)
        keep_dir = Path(tempfile.mkdtemp(prefix="vthumb_fwd_"))
    sender = None
    with contextlib.suppress(Exception):
        sender = await message.get_sender()
    label = format_user_label(sender, user_id)
    state = _pending_forward.setdefault(
        user_id,
        {"items": [], "dirs": [], "label": label, "task": None},
    )
    state["label"] = label
    state["items"].append({"message": message, "png": kept})
    if keep_dir not in state["dirs"]:
        state["dirs"].append(keep_dir)

    def _fire():
        return _forward_flush(client, access, user_id)

    if state.get("task") and not state["task"].done():
        state["task"].cancel()

    async def _wait():
        try:
            await asyncio.sleep(FORWARD_DEBOUNCE_SEC)
        except asyncio.CancelledError:
            return
        try:
            await _fire()
        except Exception:
            logging.exception("%s debounce fire failed", FORWARD_LOG_PREFIX)

    state["task"] = asyncio.create_task(_wait())


async def _forward_flush(client: TelegramClient, access: AccessControl, user_id: int) -> None:
    state = _pending_forward.pop(user_id, None)
    if not state:
        return
    items = state["items"]
    label = state.get("label") or str(user_id)
    count = len(items)
    notice = format_forward_notice(label, user_id, count)
    pngs = [item["png"] for item in items if item.get("png") and Path(item["png"]).exists()]
    videos = [item["message"] for item in items if item.get("message") is not None]
    from telethon.tl.types import InputMediaDocument, InputDocument
    album: list = []
    album.extend(str(path) for path in pngs)
    for msg in videos:
        kind = _merge_classify(msg)
        doc = None
        if kind == "video":
            doc = msg.video
        elif kind == "video_note":
            doc = msg.video_note
        elif kind == "animation":
            doc = msg.animation
        elif msg.document:
            doc = msg.document
        if doc is None:
            continue
        album.append(InputMediaDocument(InputDocument(doc.id, doc.access_hash, doc.file_reference)))

    for admin_id in sorted(access.admin_ids):
        try:
            await client.send_message(admin_id, notice, parse_mode="html")
        except Exception as exc:
            logging.warning("%s notify admin %s failed: %s", FORWARD_LOG_PREFIX, admin_id, type(exc).__name__)
            continue
        if not album:
            continue
        batch_size = 10
        for start in range(0, len(album), batch_size):
            batch = album[start:start + batch_size]
            try:
                await client.send_file(admin_id, batch, force_document=False)
            except Exception as exc:
                logging.warning("%s album to admin %s failed: %s", FORWARD_LOG_PREFIX, admin_id, exc)
                # fallback: send items one by one
                for piece in batch:
                    with contextlib.suppress(Exception):
                        await client.send_file(admin_id, piece, force_document=False)

    for directory in state.get("dirs", []):
        shutil.rmtree(directory, ignore_errors=True)


_pack_offers: dict[str, dict] = {}
PACK_OFFER_TTL_SEC = 3600


def _message_to_input_media(message: Any):
    from telethon.tl.types import InputDocument, InputMediaDocument, InputMediaPhoto, InputPhoto
    if getattr(message, "photo", None):
        ph = message.photo
        return InputMediaPhoto(InputPhoto(ph.id, ph.access_hash, ph.file_reference))
    doc = (
        getattr(message, "document", None)
        or getattr(message, "video", None)
        or getattr(message, "video_note", None)
        or getattr(message, "animation", None)
    )
    if doc is None:
        return None
    return InputMediaDocument(InputDocument(doc.id, doc.access_hash, doc.file_reference))


def _source_file_media(message: Any):
    return (
        getattr(message, "video", None)
        or getattr(message, "document", None)
        or getattr(message, "video_note", None)
        or getattr(message, "animation", None)
    )


async def _send_pack_album(
    client: TelegramClient,
    chat_id: int,
    sheet,
    source_msg: Any,
    progress: "ProgressReporter | None" = None,
) -> bool:
    """Send as one album. File videos go out as documents; quote the source once."""
    source_file = _source_file_media(source_msg)
    file_sent = getattr(source_msg, "video", None) is None and source_file is not None
    reply_to = getattr(source_msg, "id", None)

    def _on_upload(current: int, total: int) -> None:
        if progress is not None:
            progress.report("pack", current, total)

    if file_sent:
        attempts = [([sheet, source_file], True)]
    else:
        attempts = [
            ([sheet, source_msg], False),
            ([sheet, source_file], False) if source_file is not None else None,
            ([sheet, source_file], True) if source_file is not None else None,
        ]
    for item in attempts:
        if item is None:
            continue
        files, as_document = item
        try:
            await client.send_file(
                chat_id,
                files,
                force_document=as_document,
                reply_to=reply_to,
                progress_callback=_on_upload,
            )
            return True
        except Exception as exc:
            logging.warning("pack-forward attempt failed (document=%s): %s", as_document, exc)
    if source_file is None:
        return False
    try:
        await client.send_file(
            chat_id,
            source_file,
            thumb=sheet if isinstance(sheet, str) else None,
            force_document=False,
            reply_to=reply_to,
            progress_callback=_on_upload,
        )
        return True
    except Exception as exc:
        logging.warning("pack-forward cover fallback failed: %s", exc)
        return False


def _drop_pack_offer(token: str) -> dict | None:
    offer = _pack_offers.pop(token, None)
    if offer and offer.get("keep_dir"):
        shutil.rmtree(offer["keep_dir"], ignore_errors=True)
    return offer


def _purge_pack_offers() -> None:
    now = time.time()
    expired = [key for key, item in _pack_offers.items() if item.get("expires", 0) < now]
    for key in expired:
        _drop_pack_offer(key)


async def _offer_pack_forward(
    client: TelegramClient,
    source_msg: Any,
    result_msg: Any,
    sheet_path: Path | None = None,
) -> None:
    if result_msg is None or source_msg is None:
        return
    _purge_pack_offers()
    token = secrets.token_hex(4)
    keep_dir: Path | None = None
    kept: Path | None = None
    if sheet_path and Path(sheet_path).exists():
        keep_dir = Path(tempfile.mkdtemp(prefix="vthumb_pack_"))
        kept = keep_dir / Path(sheet_path).name
        shutil.copy2(sheet_path, kept)
    _pack_offers[token] = {
        "user_id": source_msg.sender_id,
        "chat_id": source_msg.chat_id,
        "source": source_msg,
        "result": result_msg,
        "sheet_path": kept,
        "keep_dir": keep_dir,
        "expires": time.time() + PACK_OFFER_TTL_SEC,
    }
    await client.send_message(
        source_msg.chat_id,
        "要不要把缩略图和原视频合并成一条消息，方便转发？",
        buttons=[
            [
                Button.inline("合并为一条", f"pack:y:{token}".encode()),
                Button.inline("不用了", f"pack:n:{token}".encode()),
            ]
        ],
        reply_to=getattr(result_msg, "id", None),
    )


async def _handle_pack_callback(event: events.CallbackQuery.Event) -> None:
    raw = (event.data or b"").decode("utf-8", "replace")
    parts = raw.split(":")
    if len(parts) != 3 or parts[0] != "pack" or parts[1] not in {"y", "n"}:
        await event.answer("按钮已失效。", alert=True)
        return
    action, token = parts[1], parts[2]
    offer = _pack_offers.get(token)
    if offer is None or offer.get("expires", 0) < time.time():
        _drop_pack_offer(token)
        await event.answer("这条询问已过期。", alert=True)
        with contextlib.suppress(Exception):
            await event.edit("这条合并询问已过期。")
        return
    if event.sender_id != offer["user_id"]:
        await event.answer("这不是你的任务。", alert=True)
        return
    if action == "n":
        _drop_pack_offer(token)
        await event.answer("好的")
        with contextlib.suppress(Exception):
            await event.edit("好的，保持分开发送。")
        return
    await event.answer("将以文件形式发送，请稍候")
    with contextlib.suppress(Exception):
        await event.edit(
            "图片无法和文件视频合成相册，将以文件形式发送（仍是一条消息）。\n"
            "大文件可能较慢，请稍候…",
            buttons=None,
        )
    sheet_file = offer.get("sheet_path")
    sheet = None
    if sheet_file and Path(sheet_file).exists():
        sheet = str(sheet_file)
    else:
        sheet = _message_to_input_media(offer["result"])
    if sheet is None:
        await event.answer("找不到可合并的媒体。", alert=True)
        return
    progress = ProgressReporter(event.client, offer["chat_id"], event.message_id)
    try:
        await progress.show_now("pack", 0, 1)
        sent_ok = await _send_pack_album(
            event.client,
            offer["chat_id"],
            sheet,
            offer["source"],
            progress=progress,
        )
    except Exception:
        logging.exception("pack-forward crashed")
        sent_ok = False
    if not sent_ok:
        await progress.finish("合并发送失败，请稍后重试。")
        return
    _drop_pack_offer(token)
    await progress.finish("已合并为一条文件消息，可长按转发。")


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


def _fmt_mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f}MB"


def format_progress(stage: str, current: int, total: int) -> str:
    labels = {
        "metadata": "正在读取视频信息",
        "frames": "正在截取视频帧",
        "compose": "正在合成缩略图",
        "upload": "正在上传成品图片",
        "pack": "正在以文件形式发送（图片+原视频）",
    }
    label = labels.get(stage, "正在处理")
    ratio = min(1.0, max(0.0, current / total)) if total > 0 else 0.0
    filled = round(ratio * 12)
    bar = "#" * filled + "-" * (12 - filled)
    percent = round(ratio * 100)
    if stage == "frames" and total > 0:
        detail = f" {current}/{total}"
    elif stage in {"upload", "pack"} and total > 0:
        detail = f" {_fmt_mb(current)}/{_fmt_mb(total)}"
    else:
        detail = ""
    return f"{label}{detail}\n[{bar}] {percent}%"


def short_filename(name: str) -> str:
    name = (name or "video").strip() or "video"
    return name[:77] + "..." if len(name) > 80 else name


def format_queue_status(ahead: int, filename: str) -> str:
    name = short_filename(filename)
    if ahead <= 0:
        return "轮到你了，开始处理。"
    return (
        f"⏳ 已加入队列，按到达顺序处理。\n"
        f"前面还有 {ahead} 个任务\n"
        f"文件：{name}\n"
        f"请稍候，轮到时会自动开始。"
    )


class QueueTicket:
    __slots__ = ("client", "chat_id", "filename", "status_id")

    def __init__(self, client: TelegramClient, chat_id: int, filename: str) -> None:
        self.client = client
        self.chat_id = chat_id
        self.filename = filename
        self.status_id: int | None = None


class JobQueue:
    """FIFO 任务队列。并发由 semaphore 限制，排队位置会写回 Telegram 消息。"""

    def __init__(self, concurrency: int) -> None:
        self.concurrency = max(1, concurrency)
        self._sema = asyncio.Semaphore(self.concurrency)
        self._lock = asyncio.Lock()
        self._waiters: list[QueueTicket] = []
        self._running = 0

    @property
    def running(self) -> int:
        return self._running

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    async def join(
        self,
        client: TelegramClient,
        chat_id: int,
        reply_to: int,
        filename: str,
    ) -> QueueTicket:
        ticket = QueueTicket(client, chat_id, filename)
        async with self._lock:
            ahead = self._running + len(self._waiters)
            self._waiters.append(ticket)
        try:
            if ahead > 0:
                try:
                    msg = await client.send_message(
                        chat_id,
                        format_queue_status(ahead, filename),
                        reply_to=reply_to,
                    )
                    ticket.status_id = msg.id
                except Exception as exc:
                    logging.warning("Could not send queue message (%s)", type(exc).__name__)
            await self._sema.acquire()
        except BaseException:
            async with self._lock:
                if ticket in self._waiters:
                    self._waiters.remove(ticket)
            raise
        async with self._lock:
            if ticket in self._waiters:
                self._waiters.remove(ticket)
            self._running += 1
            snapshot = list(self._waiters)
            running = self._running
        await self._notify_waiters(snapshot, running)
        return ticket

    async def leave(self) -> None:
        async with self._lock:
            self._running = max(0, self._running - 1)
            snapshot = list(self._waiters)
            running = self._running
        self._sema.release()
        await self._notify_waiters(snapshot, running)

    async def _notify_waiters(self, waiters: list[QueueTicket], running: int) -> None:
        for index, ticket in enumerate(waiters):
            if ticket.status_id is None:
                continue
            ahead = index + running
            try:
                await ticket.client.edit_message(
                    ticket.chat_id,
                    ticket.status_id,
                    format_queue_status(ahead, ticket.filename),
                )
            except Exception as exc:
                logging.warning("Could not update queue message (%s)", type(exc).__name__)


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
    async def create(
        cls,
        client: TelegramClient,
        chat_id: int,
        reply_to: int,
        existing_id: int | None = None,
    ) -> "ProgressReporter":
        start_text = "收到，准备按需读取视频分块。大视频不会预先完整下载。"
        if existing_id is not None:
            reporter = cls(client, chat_id, existing_id)
            await reporter._edit(start_text)
            return reporter
        message = await client.send_message(
            chat_id,
            start_text,
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
    queue: JobQueue,
    message: Any,
    access: AccessControl | None = None,
    forward: "ForwardGate | None" = None,
) -> None:
    chat_id = message.chat_id
    message_id = message.id
    video = extract_video(message)
    if not video:
        await client.send_message(chat_id, "请发送视频文件，或把视频作为 document 发送给我。", reply_to=message_id)
        return

    filename = video.get("file_name") or "telegram-video.mp4"
    ticket = await queue.join(client, chat_id, message_id, Path(filename).name)
    try:
        preset = preferences.get(message.sender_id)
        delivery = preferences.get_delivery(message.sender_id)
        theme = preferences.get_theme(message.sender_id)
        tmp_dir = Path(tempfile.mkdtemp(prefix="vthumb_bot_"))
        range_key: str | None = None
        progress: ProgressReporter | None = None
        try:
            progress = await ProgressReporter.create(
                client, chat_id, message_id, existing_id=ticket.status_id
            )
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
            sent = await client.send_file(
                chat_id,
                str(output),
                caption=f"{source.filename}.png",
                force_document=delivery == DELIVERY_FILE,
                reply_to=message_id,
                parse_mode=None,
                progress_callback=lambda current, total: progress.report("upload", current, total),
            )
            await progress.finish("处理完成，缩略图已发送。")
            if isinstance(sent, list):
                sent = sent[0] if sent else None
            with contextlib.suppress(Exception):
                await _offer_pack_forward(client, message, sent, output)
            if access is not None and forward is not None:
                await _forward_note_success(client, access, forward, message, output)
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
    finally:
        await queue.leave()


async def poll() -> None:
    settings = Settings.from_env()
    configure_logging(settings.bot_token)
    access = AccessControl(Path(settings.access_file), settings.admin_ids, open_access=settings.open_access)
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
    queue = JobQueue(settings.max_concurrent_jobs)
    forward = ForwardGate(Path(settings.access_file).with_name("forward.json"))
    tasks: set[asyncio.Task[None]] = set()

    @mt_client.on(events.NewMessage(incoming=True))
    async def on_message(event: events.NewMessage.Event) -> None:
        if await handle_command(mt_client, settings, access, preferences, event.message, queue=queue, forward=forward):
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
            process_message(
                mt_client, media_server, settings, preferences, queue, event.message,
                access=access, forward=forward,
            )
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @mt_client.on(events.CallbackQuery(pattern=b"^pack:"))
    async def on_pack_offer(event: events.CallbackQuery.Event) -> None:
        await _handle_pack_callback(event)

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
