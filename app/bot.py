import asyncio
import contextlib
import html
import json
import logging
import secrets
import shutil
import subprocess
import socket
import tempfile
import time
from pathlib import Path
from typing import Any

from telethon import Button, TelegramClient, events, functions, types
from telethon.network.connection.tcpfull import ConnectionTcpFull
from telethon.sessions import SQLiteSession

from .access_control import AccessControl, GroupAccess
from .announce import Audience, announce_body, private_sender_id
from .mtproto_range import FetchBudgetExceeded, MTProtoRangeServer
from .remux import evaluate_copy_remux, probe_streams, remux_copy_mp4
from .settings import Settings
from .task_log import TaskLog, content_key, file_key
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
UPLOAD_PART_SIZE_KB = 512


class ConnectionTcpKeepalive(ConnectionTcpFull):
    """Keep NAT from dropping long Telegram uploads (~90s idle reset)."""

    async def _connect(self, timeout=None, ssl=None):
        await super()._connect(timeout=timeout, ssl=ssl)
        sock = self._writer.get_extra_info("socket") if self._writer else None
        if sock is None:
            return
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
        if idle is not None:
            sock.setsockopt(socket.IPPROTO_TCP, idle, 20)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)


def _install_fast_upload(client: TelegramClient) -> None:
    original = client.upload_file

    async def upload_file(file, *args, part_size_kb=None, **kwargs):
        if part_size_kb is None:
            part_size_kb = UPLOAD_PART_SIZE_KB
        return await original(file, *args, part_size_kb=part_size_kb, **kwargs)

    client.upload_file = upload_file  # type: ignore[method-assign]

BOT_COMMANDS = (
    ("start", "开始使用并查看视频发送方式"),
    ("help", "查看使用说明和自适应读取策略"),
    ("status", "查看缩略图设置和读取预算"),
    ("setting", "选择每个用户独立的缩略图预设"),
    ("id", "查看自己的 Telegram 用户 ID"),
    ("info", "查看账号、授权状态和今日剩余次数"),
    ("checkin", "每日签到，获得可储存的使用次数"),
    ("add", "管理员：添加授权用户"),
    ("del", "管理员：删除授权用户"),
    ("users", "管理员：查看授权用户"),
    ("announce", "管理员：预览并发布 Markdown 公告"),
    ("group", "管理员：在群里授权该群使用缩略图"),
    ("thumb", "回复一条视频，生成缩略图"),
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


def command_mention(text: str | None) -> str | None:
    """Return the @username a command is addressed to, if any."""
    if not text or not text.startswith("/"):
        return None
    token = text.split(None, 1)[0]
    if "@" not in token:
        return None
    mention = token.split("@", 1)[1].strip().lower()
    return mention or None


def permission_message(user_id: int | None) -> str:
    return f"你还没有使用权限。\n你的 Telegram ID：{user_id or '未知'}\n请将此 ID 提供给管理员添加。"


def quota_message(limit: int, community_url: str) -> str:
    text = (
        f"今天的 {limit} 次生成已经用完，储存次数也没有了。\n"
        "发送 /checkin 每天可再获得 1 到 10 次。\n"
        "请通过社群群聊联系开发者升级权限。"
    )
    if community_url:
        text += f"\n{community_url}"
    return text


_audience: Audience | None = None
_announce_wait: set[int] = set()
_announce_drafts: dict[str, dict] = {}
ANNOUNCE_HINT = (
    "请发送公告内容。支持 Markdown：\n"
    "**粗体**、__斜体__、`代码`、[文字](链接)"
)


async def _show_announce_preview(client: TelegramClient, admin_id: int, chat_id: int, text: str, reply_to: int) -> None:
    if len(text) > 4000:
        await client.send_message(chat_id, "公告太长，请控制在 4000 字以内。", reply_to=reply_to)
        return
    token = secrets.token_hex(4)
    _announce_drafts[token] = {"admin_id": admin_id, "chat_id": chat_id, "text": text}
    _announce_wait.discard(admin_id)
    try:
        await client.send_message(
            chat_id,
            "公告预览，确认后才会发给用户。\n\n" + text,
            parse_mode="md",
            link_preview=False,
            buttons=[[
                Button.inline("确认发布", f"ann:y:{token}".encode()),
                Button.inline("重新编辑", f"ann:e:{token}".encode()),
            ]],
            reply_to=reply_to,
        )
    except Exception as exc:
        _announce_drafts.pop(token, None)
        await client.send_message(
            chat_id,
            f"Markdown 无法预览，请修改后再发。\n{type(exc).__name__}: {exc}",
            reply_to=reply_to,
        )


async def _begin_announce(client: TelegramClient, message: Any) -> None:
    body = announce_body(message.raw_text)
    if not body:
        if message.sender_id:
            _announce_wait.add(message.sender_id)
        await client.send_message(message.chat_id, ANNOUNCE_HINT, reply_to=message.id)
        return
    await _show_announce_preview(client, message.sender_id, message.chat_id, body, message.id)


def _quote_ids(command: Any) -> tuple[int | None, int | None]:
    """Message id the command replied to, plus forum topic id when present."""
    header = getattr(command, "reply_to", None)
    msg_id = getattr(header, "reply_to_msg_id", None)
    top_id = getattr(header, "reply_to_top_id", None)
    return (int(msg_id) if msg_id else None, int(top_id) if top_id else None)


def _input_reply(msg_id: int, top_id: int | None = None) -> types.InputReplyToMessage:
    return types.InputReplyToMessage(
        reply_to_msg_id=int(msg_id),
        top_msg_id=int(top_id) if top_id else None,
    )


async def _input_peer(client: TelegramClient, message: Any, chat_id: int):
    getter = getattr(message, "get_input_chat", None)
    if getter is not None:
        try:
            peer = await getter()
            if peer is not None:
                return peer
        except Exception:
            logging.warning("group input peer lookup failed", exc_info=True)
    return await client.get_input_entity(chat_id)


async def _send_text_quoting(
    client: TelegramClient,
    peer: Any,
    text: str,
    msg_id: int,
    top_id: int | None = None,
):
    request = functions.messages.SendMessageRequest(
        peer=peer,
        message=text,
        reply_to=_input_reply(msg_id, top_id),
    )
    return client._get_response_message(request, await client(request), peer)


async def _send_file_quoting(
    client: TelegramClient,
    peer: Any,
    path: str,
    *,
    caption: str,
    force_document: bool,
    msg_id: int,
    top_id: int | None = None,
    progress_callback=None,
):
    _handle, media, _image = await client._file_to_media(
        path,
        force_document=force_document,
        progress_callback=progress_callback,
    )
    request = functions.messages.SendMediaRequest(
        peer=peer,
        media=media,
        message=caption or "",
        reply_to=_input_reply(msg_id, top_id),
    )
    return client._get_response_message(request, await client(request), peer)


def _is_group_message(message: Any) -> bool:
    return bool(getattr(message, "is_group", False))


_bot_username_cache: str | None = None


async def _bot_username(client: TelegramClient) -> str | None:
    global _bot_username_cache
    if _bot_username_cache:
        return _bot_username_cache
    me = await client.get_me()
    username = (getattr(me, "username", None) or "").strip().lower()
    _bot_username_cache = username or None
    return _bot_username_cache


def _multipart_body(fields: dict[str, str], file_field: str, filename: str, data: bytes, mime: str) -> tuple[str, bytes]:
    boundary = "----vthumb" + secrets.token_hex(8)
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        )
    chunks.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n"
        ).encode()
    )
    chunks.append(data)
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    return boundary, b"".join(chunks)


def _botapi_send_file_sync(
    token: str,
    chat_id: int,
    path: str,
    *,
    caption: str,
    reply_to: int,
    force_document: bool,
    thread_id: int | None = None,
) -> tuple[int | None, bool, str | None]:
    import json
    import urllib.error
    import urllib.request

    method = "sendDocument" if force_document else "sendPhoto"
    field = "document" if force_document else "photo"
    suffix = Path(path).suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    fields = {
        "chat_id": str(chat_id),
        "caption": caption or "",
        "reply_parameters": json.dumps({
            "message_id": int(reply_to),
            "allow_sending_without_reply": False,
        }),
        "parse_mode": "HTML",
    }
    if thread_id:
        fields["message_thread_id"] = str(int(thread_id))
    boundary, body = _multipart_body(fields, field, Path(path).name, Path(path).read_bytes(), mime)
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            description = json.loads(raw).get("description") or raw
        except json.JSONDecodeError:
            description = raw
        return None, False, str(description)[:300]
    result = payload.get("result") or {}
    reply = result.get("reply_to_message") or {}
    return result.get("message_id"), bool(reply.get("message_id")), None


class _BotApiMessage:
    def __init__(self, chat_id: int, message_id: int, has_reply: bool) -> None:
        self.chat_id = chat_id
        self.id = message_id
        self.reply_to = object() if has_reply else None


async def _quote_for_video(client: TelegramClient, video: Any):
    """Reply target that Telegram will actually keep.

    A forwarded channel post cannot be quoted by its copy in the group.
    Quote the original channel message instead.
    """
    fwd = getattr(video, "fwd_from", None)
    origin = getattr(fwd, "from_id", None) if fwd else None
    post_id = getattr(fwd, "channel_post", None) if fwd else None
    if origin is not None and post_id:
        try:
            peer = await client.get_input_entity(origin)
            return types.InputReplyToMessage(
                reply_to_msg_id=int(post_id),
                reply_to_peer_id=peer,
            )
        except Exception:
            logging.warning("could not resolve forwarded video for quote", exc_info=True)
    msg_id = getattr(video, "id", None)
    if not msg_id:
        return None
    return types.InputReplyToMessage(reply_to_msg_id=int(msg_id))


async def _send_media_quoting(
    client: TelegramClient,
    chat_id: int,
    path: str,
    *,
    caption: str,
    force_document: bool,
    video: Any,
    progress_callback=None,
):
    peer = await client.get_input_entity(chat_id)
    reply = await _quote_for_video(client, video)
    text, entities = await client._parse_message_text(caption or "", "html")
    _handle, media, _image = await client._file_to_media(
        path,
        force_document=force_document,
        progress_callback=progress_callback,
    )
    request = functions.messages.SendMediaRequest(
        peer=peer,
        media=media,
        message=text or "",
        entities=entities,
        reply_to=reply,
    )
    return client._get_response_message(request, await client(request), peer)


def _html_mention(user_id: int | None, label: str | None) -> str:
    if not user_id:
        return ""
    name = html.escape((label or str(user_id)).lstrip("@") or str(user_id))
    return f'<a href="tg://user?id={int(user_id)}">{name}</a>'


def _botapi_delete_sync(token: str, chat_id: int, message_id: int) -> None:
    import json
    import urllib.request

    body = json.dumps({"chat_id": chat_id, "message_id": message_id}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/deleteMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20):
        return


async def _handle_group_command(
    client: TelegramClient,
    access: AccessControl,
    groups: GroupAccess | None,
    message: Any,
) -> None:
    if not access.is_admin(message.sender_id):
        await client.send_message(message.chat_id, "此命令仅限管理员使用。", reply_to=message.id)
        return
    if not _is_group_message(message):
        await client.send_message(message.chat_id, "请在要授权的群里发送 /group。", reply_to=message.id)
        return
    if groups is None:
        await client.send_message(message.chat_id, "群授权还没准备好。", reply_to=message.id)
        return
    arg = ""
    parts = (message.raw_text or "").split()
    if len(parts) > 1:
        arg = parts[1].lower()
    chat_id = int(message.chat_id)
    if arg in {"off", "0", "false", "关"}:
        removed = await groups.remove(chat_id)
        text = "已取消这个群的缩略图授权。" if removed else "这个群本来就没有授权。"
    else:
        added = await groups.add(chat_id)
        text = "已授权这个群。授权用户或管理员回复视频并发送 /thumb 即可生成。" if added else "这个群已经授权过了。"
    await client.send_message(message.chat_id, text, reply_to=message.id)


async def _handle_thumb_command(
    client: TelegramClient,
    access: AccessControl,
    groups: GroupAccess | None,
    task_log: TaskLog | None,
    message: Any,
    enqueue,
) -> None:
    if not _is_group_message(message):
        await client.send_message(message.chat_id, "请在已授权的群里回复视频，再发送 /thumb。", reply_to=message.id)
        return
    if groups is None or not groups.is_authorized(message.chat_id):
        await client.send_message(message.chat_id, "这个群还没有授权。请管理员在群里发送 /group。", reply_to=message.id)
        return
    if not access.has_grant(message.sender_id):
        await client.send_message(message.chat_id, "只有授权用户或管理员可以在群里生成缩略图。", reply_to=message.id)
        return
    if not getattr(message, "is_reply", False):
        await client.send_message(message.chat_id, "请回复一条视频，再发送 /thumb。", reply_to=message.id)
        return
    try:
        replied = await message.get_reply_message()
    except Exception:
        replied = None
    if replied is None or extract_video(replied) is None:
        await client.send_message(message.chat_id, "请回复一条视频，再发送 /thumb。", reply_to=message.id)
        return
    if enqueue is None:
        await client.send_message(message.chat_id, "任务队列还没准备好。", reply_to=message.id)
        return
    header_id, quote_top_id = _quote_ids(message)
    sender = getattr(message, "sender", None)
    mention_label = format_user_label(sender, message.sender_id)
    job_kwargs = dict(
        preference_user_id=message.sender_id,
        offer_pack=False,
        quote_msg_id=header_id or getattr(replied, "id", None),
        quote_top_id=quote_top_id,
        command_msg_id=message.id,
        header_msg_id=header_id,
        mention_user_id=message.sender_id,
        mention_label=mention_label,
    )
    key = file_key(replied)
    record = task_log.lookup(key, content_key(replied)) if task_log is not None and key else None
    if record:
        await _offer_previous_result(
            client,
            replied,
            record,
            key or "",
            requester_id=message.sender_id,
            enqueue_kwargs=job_kwargs,
            mention_html=_html_mention(message.sender_id, mention_label),
        )
        return
    enqueue(replied, **job_kwargs)


def _announce_edit_text(message: Any) -> str | None:
    if extract_video(message) is not None:
        return None
    text = (getattr(message, "raw_text", None) or "").strip()
    if not text or text.startswith("/"):
        return None
    return text


async def refresh_private_audience(client: TelegramClient, audience: Audience) -> int:
    """Replace the announcement list with users who already have a private chat."""
    found: set[int] = set()
    async for dialog in client.iter_dialogs():
        if not dialog.is_user:
            continue
        entity = dialog.entity
        if entity is None or getattr(entity, "bot", False):
            continue
        user_id = int(getattr(entity, "id", 0) or 0)
        if user_id > 0:
            found.add(user_id)
    if not found:
        logging.warning("private audience refresh found no users; keeping the saved list")
        return len(audience.ids())
    audience.replace(found)
    logging.info("announcement audience limited to %s private chats", len(found))
    return len(found)


async def _publish_announce(
    client: TelegramClient,
    text: str,
    recipients: list[int],
    audience: Audience | None = None,
) -> tuple[int, int]:
    sent = 0
    failed = 0
    for user_id in recipients:
        try:
            await client.send_message(user_id, text, parse_mode="md", link_preview=False)
            sent += 1
        except Exception as exc:
            if audience is not None and type(exc).__name__ == "PeerIdInvalidError":
                audience.discard(user_id)
            logging.warning("announce delivery failed for %s", user_id, exc_info=True)
            failed += 1
        await asyncio.sleep(0.05)
    return sent, failed


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


def setting_buttons(
    current: ThumbnailPreset,
    delivery: str,
    theme: str,
    admin_forward: bool | None = None,
) -> list[list[Button]]:
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
    rows = [
        buttons[:2],
        buttons[2:],
        delivery_buttons,
        *theme_rows,
    ]
    if admin_forward is not None:
        label = "任务转发管理员：开" if admin_forward else "任务转发管理员：关"
        rows.append([Button.inline(label, data=b"fwdgate:toggle")])
    return rows


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
    task_log: "TaskLog | None" = None,
    groups: "GroupAccess | None" = None,
    enqueue=None,
) -> bool:
    command = command_name(message.raw_text)
    if command is None:
        return False
    if _is_group_message(message):
        mention = command_mention(message.raw_text)
        if mention and mention != await _bot_username(client):
            return True

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
                "3. 使用 /info 查看账号、授权状态和今日剩余次数。\n"
                f"4. {settings.small_file_full_read_mb}MB 以内按需读取，必要时可读取完整范围。\n"
                f"5. 大视频初始读取预算 {settings.max_source_fetch_ratio:.0%}，"
                f"按需提升至 {settings.hard_source_fetch_ratio:.0%}，"
                f"且不超过 {settings.max_source_fetch_mb}MB。\n"
                "6. 已读取分块会临时缓存，任务结束后自动删除。\n"
                "7. 多人同时发送会按到达顺序排队，轮到你时自动开始。\n"
                "8. 以文件发送的视频，缩略图完成后可合并成一条文件消息。\n"
                "9. 已授权的群里，回复视频并发送 /thumb。任务和私聊共用队列。"
            )
            if is_admin:
                text += "\n\n管理员命令：\n/add 用户ID\n/del 用户ID\n/users\n/announce 公告内容\n/group 在群里授权该群\n/thumb 回复视频生成缩略图"
            if not access.has_grant(user_id):
                text += (
                    f"\n\n未授权每天可新生成 {settings.guest_daily_limit} 次。"
                    "使用以前的生成结果不计入次数。"
                    " /checkin 每天可额外获得 1 到 10 次，存在储存次数里。"
                )
    elif command == "id":
        text = f"你的 Telegram ID：{user_id or '未知'}"
    elif command == "info":
        sender = getattr(message, "sender", None)
        if sender is None and hasattr(message, "get_sender"):
            try:
                sender = await message.get_sender()
            except Exception:
                sender = None
        used = task_log.used_today(user_id) if task_log is not None and user_id is not None else 0
        limit = task_log.daily_limit if task_log is not None else settings.guest_daily_limit
        text = account_info_text(
            sender,
            user_id,
            granted=access.has_grant(user_id),
            is_admin=is_admin,
            used=used,
            limit=limit,
            stored=task_log.stored_credits(user_id) if task_log is not None and user_id is not None else 0,
        )
    elif command == "checkin":
        if task_log is None or user_id is None:
            text = "签到暂时不可用。"
        else:
            result = task_log.check_in(user_id)
            if result is None:
                text = f"今天已经签到过了。储存次数：{task_log.stored_credits(user_id)}"
            else:
                gained, total = result
                text = (
                    f"签到成功，获得 {gained} 次。储存次数：{total}\n"
                    f"这些次数可以留着，不占每天的 {settings.guest_daily_limit} 次。"
                )
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
            buttons = setting_buttons(
                preset,
                delivery,
                theme,
                admin_forward=forward.enabled if is_admin and forward is not None else None,
            )
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
    elif command == "announce":
        if not is_admin:
            text = "此命令仅限管理员使用。"
        else:
            await _begin_announce(client, message)
            return True
    elif command == "group":
        await _handle_group_command(client, access, groups, message)
        return True
    elif command == "thumb":
        await _handle_thumb_command(client, access, groups, task_log, message, enqueue)
        return True
    else:
        if _is_group_message(message):
            return True
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
        f"  支持: 图片 / 视频 / 文件视频 / 动图 / 圆视频。\n"
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
    """Return photo/animation/video/video_note/file_video/document, or ''."""
    if getattr(message, "video", None):
        return "video"
    if getattr(message, "video_note", None):
        return "video_note"
    if getattr(message, "animation", None):
        return "animation"
    if getattr(message, "photo", None):
        return "photo"
    if extract_video(message):
        return "file_video"
    document = getattr(message, "document", None)
    if document is None:
        return ""
    mime = ""
    file_info = getattr(message, "file", None)
    if file_info is not None:
        mime = (getattr(file_info, "mime_type", None) or "")
    mime = (mime or getattr(document, "mime_type", None) or "").lower()
    if mime.startswith("image/"):
        return "photo"
    return "document"


def _merge_raw_file(message: Any):
    return (
        getattr(message, "video", None)
        or getattr(message, "video_note", None)
        or getattr(message, "animation", None)
        or getattr(message, "document", None)
    )


async def _merge_as_documents(client: TelegramClient, messages: list) -> tuple[list, Path | None]:
    """Turn a mixed photo+document batch into files Telegram can group."""
    files: list = []
    tmp_dir: Path | None = None
    for message in messages:
        raw = _merge_raw_file(message)
        if raw is not None:
            files.append(raw)
            continue
        if getattr(message, "photo", None) is None:
            continue
        if tmp_dir is None:
            tmp_dir = Path(tempfile.mkdtemp(prefix="vthumb_merge_"))
        dest = tmp_dir / f"{getattr(message, 'id', len(files))}.jpg"
        await client.download_media(message, file=str(dest))
        files.append(str(dest))
    return files, tmp_dir


def _merge_input_document(message: Any):
    doc = _merge_raw_file(message)
    if doc is None:
        return None
    from telethon.tl.types import InputDocument, InputMediaDocument
    return InputMediaDocument(InputDocument(doc.id, doc.access_hash, doc.file_reference))


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
    photos = [m for m in msgs if _merge_classify(m) in {"photo", "animation"}]
    videos = [
        m for m in msgs
        if _merge_classify(m) in {"video", "video_note", "file_video", "document"}
    ]
    needs_file_album = any(
        _merge_classify(m) in {"file_video", "document"} for m in msgs
    )

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
    logging.info(
        f"{MERGE_LOG_PREFIX} uid={user_id} firing: "
        f"{len(photos)} photo/animation + {len(videos)} video, file_album={needs_file_album}"
    )

    BATCH = 10
    sent = 0
    failed_indices: list[int] = []
    tmp_dir: Path | None = None
    media: list = []
    try:
        if needs_file_album:
            media, tmp_dir = await _merge_as_documents(client, photos + videos)
        else:
            from telethon.tl.types import InputMediaPhoto, InputPhoto
            media = []
            for m in photos:
                if getattr(m, "photo", None) is not None:
                    ph = m.photo
                    media.append(InputMediaPhoto(InputPhoto(ph.id, ph.access_hash, ph.file_reference)))
                else:
                    wrapped = _merge_input_document(m)
                    if wrapped is not None:
                        media.append(wrapped)
            for m in videos:
                wrapped = _merge_input_document(m)
                if wrapped is not None:
                    media.append(wrapped)

        if not media:
            await client.send_message(
                chat_id,
                "✗ 没有可合并的媒体 (全部 file_id 缺失)。请重新 /merge。",
            )
            return

        for batch_start in range(0, len(media), BATCH):
            batch = media[batch_start:batch_start + BATCH]
            try:
                await client.send_file(
                    chat_id,
                    batch,
                    force_document=needs_file_album,
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
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)

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


def account_info_text(
    sender: Any,
    user_id: int | None,
    *,
    granted: bool,
    is_admin: bool,
    used: int,
    limit: int,
    stored: int = 0,
) -> str:
    name = ""
    username = None
    if sender is not None:
        username = getattr(sender, "username", None)
        first = (getattr(sender, "first_name", None) or "").strip()
        last = (getattr(sender, "last_name", None) or "").strip()
        name = " ".join(part for part in (first, last) if part)
    lines = [
        "账号信息",
        f"名字：{name or '未设置'}",
        f"用户名：@{username}" if username else "用户名：无",
        f"Telegram ID：{user_id or '未知'}",
    ]
    if is_admin:
        lines.append("授权用户：是（管理员）")
        lines.append("今日剩余次数：不限")
    elif granted:
        lines.append("授权用户：是")
        lines.append("今日剩余次数：不限")
    else:
        remaining = max(int(limit) - int(used), 0)
        lines.append("授权用户：否")
        lines.append(f"今日剩余次数：{remaining}/{limit}")
        lines.append(f"储存次数：{stored}")
        lines.append("使用以前的生成记录不计入次数。每日签到获得的次数会存在这里。")
    return "\n".join(lines)


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


async def _forward_note_success(
    client: TelegramClient,
    access: AccessControl,
    gate: ForwardGate,
    message: Any,
    png_path: Path,
    requester_id: int | None = None,
    requester_label: str | None = None,
) -> None:
    if not gate.enabled:
        return
    actor_id = requester_id if requester_id is not None else getattr(message, "sender_id", None)
    if actor_id is None or access.is_admin(actor_id):
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
    label = requester_label or format_user_label(sender, actor_id)
    state = _pending_forward.setdefault(
        actor_id,
        {"items": [], "dirs": [], "label": label, "task": None},
    )
    state["label"] = label
    state["items"].append({"message": message, "png": kept})
    if keep_dir not in state["dirs"]:
        state["dirs"].append(keep_dir)
    logging.info("%s queued for admin uid=%s", FORWARD_LOG_PREFIX, actor_id)

    def _fire():
        return _forward_flush(client, access, actor_id)

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
    for admin_id in sorted(access.admin_ids):
        try:
            await client.send_message(admin_id, notice, parse_mode="html")
        except Exception as exc:
            logging.warning("%s notify admin %s failed: %s", FORWARD_LOG_PREFIX, admin_id, type(exc).__name__)
            continue
        batch: list = [str(path) for path in pngs]
        batch.extend(videos)
        if not batch:
            continue
        try:
            await client.send_file(admin_id, batch[:10])
        except Exception as exc:
            logging.warning("%s album to admin %s failed: %s", FORWARD_LOG_PREFIX, admin_id, exc)
            for piece in batch:
                with contextlib.suppress(Exception):
                    await client.send_file(admin_id, piece)

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


def _is_file_sent_video(message: Any) -> bool:
    return extract_video(message) is not None and getattr(message, "video", None) is None


def pack_offer_prompt(is_file_video: bool, remux_max_mb: int, *, allow_remux: bool = False) -> str:
    del remux_max_mb, allow_remux
    if is_file_video:
        return (
            "这个视频是按文件发送的，Telegram 不能直接内嵌播放。\n"
            "要不要把缩略图和原文件合并成一条？合并后仍是文件形式。"
        )
    return "要不要把缩略图和原视频合并成一条消息，方便转发？"


def _source_file_size(message: Any) -> int:
    video = extract_video(message) or {}
    size = video.get("file_size")
    return int(size) if isinstance(size, int) and size > 0 else 0


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
    result_msg: Any | None = None,
    as_files: bool = False,
    progress: "ProgressReporter | None" = None,
) -> bool:
    """Reuse existing media IDs when both items are viewable; re-upload only for files."""
    source_file = _source_file_media(source_msg)

    def _on_upload(current: int, total: int) -> None:
        if progress is not None:
            progress.report("pack" if as_files else "pack_media", current, total)

    if not as_files:
        thumb = _message_to_input_media(result_msg) if result_msg is not None else None
        video = _message_to_input_media(source_msg)
        attempts = []
        if thumb is not None and video is not None:
            attempts.append([thumb, video])
        if result_msg is not None:
            attempts.append([result_msg, source_msg])
        for album in attempts:
            try:
                await client.send_file(chat_id, album, force_document=False)
                return True
            except Exception as exc:
                logging.warning("pack-forward reuse failed: %s", exc)
        return False

    if source_file is None or not sheet:
        return False
    try:
        await client.send_file(
            chat_id,
            [sheet, source_file],
            force_document=True,
            progress_callback=_on_upload,
        )
        return True
    except Exception as exc:
        logging.warning("pack-forward file album failed: %s", exc)
        return False


async def _fallback_merge_as_files(
    event: events.CallbackQuery.Event,
    offer: dict,
    *,
    reason: str,
    progress: "ProgressReporter | None" = None,
    already_answered: bool = False,
) -> bool:
    """When copy-remux is impossible, pack sheet + original as a file album."""
    if not already_answered:
        with contextlib.suppress(Exception):
            await event.answer("无法无损封装，改为按文件合并")
    sheet_file = offer.get("sheet_path")
    sheet = str(sheet_file) if sheet_file and Path(sheet_file).exists() else None
    if progress is None:
        progress = ProgressReporter(event.client, offer["chat_id"], event.message_id)
        await progress.show_now("pack", 0, 1)
    else:
        progress.report("pack", 0, 1)
        await progress.show_now("pack", 0, 1)
    note = reason.rstrip("。．. ") + "。改为按文件合并发送。"
    with contextlib.suppress(Exception):
        await event.edit(note, buttons=None)
    sent_ok = await _send_pack_album(
        event.client,
        offer["chat_id"],
        sheet,
        offer["source"],
        result_msg=offer.get("result"),
        as_files=True,
        progress=progress,
    )
    if sent_ok:
        await progress.finish("无法转成可播放视频，已按文件合并成一条，可长按转发。")
    else:
        await progress.finish(note + " 文件合并也失败了，请稍后重试。")
    return sent_ok


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
    queue: "JobQueue | None" = None,
    remux_max_mb: int = 1024,
    allow_remux: bool = False,
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
    is_file_video = _is_file_sent_video(source_msg)
    _pack_offers[token] = {
        "user_id": source_msg.sender_id,
        "chat_id": source_msg.chat_id,
        "source": source_msg,
        "result": result_msg,
        "sheet_path": kept,
        "keep_dir": keep_dir,
        "queue": queue,
        "is_file_video": is_file_video,
        "allow_remux": False,
        "remux_max_mb": remux_max_mb,
        "file_size": _source_file_size(source_msg),
        "expires": time.time() + PACK_OFFER_TTL_SEC,
    }
    buttons = [
        [
            Button.inline("合并为一条", f"pack:y:{token}".encode()),
            Button.inline("不用了", f"pack:n:{token}".encode()),
        ]
    ]
    await client.send_message(
        source_msg.chat_id,
        pack_offer_prompt(is_file_video, remux_max_mb),
        buttons=buttons,
        reply_to=getattr(result_msg, "id", None),
    )


async def _remux_pack_and_send(event: events.CallbackQuery.Event, offer: dict, token: str) -> None:
    remux_max_mb = int(offer.get("remux_max_mb") or 1024)
    file_size = int(offer.get("file_size") or 0)
    limit = remux_max_mb * 1024 * 1024
    if file_size <= 0 or file_size > limit:
        await _fallback_merge_as_files(
            event,
            offer,
            reason=f"这个文件超过 {remux_max_mb}MB，不能无损封装",
        )
        _drop_pack_offer(token)
        return
    keep_dir_probe = offer.get("keep_dir")
    disk_root = Path(keep_dir_probe) if keep_dir_probe else Path(tempfile.gettempdir())
    free = shutil.disk_usage(disk_root).free
    if free < int(file_size * 2.2) + 64 * 1024 * 1024:
        await _fallback_merge_as_files(
            event,
            offer,
            reason="磁盘空间不够，不能无损封装",
        )
        _drop_pack_offer(token)
        return
    await event.answer("开始无损封装，请稍候")
    with contextlib.suppress(Exception):
        await event.edit("正在无损封装成可播放视频，请稍候…", buttons=None)
    keep_dir = offer.get("keep_dir")
    if keep_dir is None:
        keep_dir = Path(tempfile.mkdtemp(prefix="vthumb_remux_"))
        offer["keep_dir"] = keep_dir
    keep_dir = Path(keep_dir)
    src_path = keep_dir / "source.bin"
    dst_path = keep_dir / "playable.mp4"
    sheet_file = offer.get("sheet_path")
    queue = offer.get("queue")
    progress = ProgressReporter(event.client, offer["chat_id"], event.message_id)
    joined = False
    try:
        if queue is not None:
            await queue.join(event.client, offer["chat_id"], event.message_id, "remux.mp4")
            joined = True
        await progress.show_now("remux_dl", 0, file_size)

        def _on_dl(current: int, total: int) -> None:
            progress.report("remux_dl", current, total or file_size)

        await event.client.download_media(
            offer["source"],
            file=str(src_path),
            progress_callback=_on_dl,
        )
        await progress.show_now("remux", 0, 1)
        streams = await asyncio.to_thread(probe_streams, src_path)
        ok, reason = evaluate_copy_remux(streams)
        if not ok:
            await _fallback_merge_as_files(
                event,
                offer,
                reason=reason,
                progress=progress,
                already_answered=True,
            )
            return
        await asyncio.to_thread(remux_copy_mp4, src_path, dst_path)
        with contextlib.suppress(OSError):
            src_path.unlink()
        await progress.show_now("pack_media", 0, max(1, dst_path.stat().st_size))
        album: list[str] = []
        if sheet_file and Path(sheet_file).exists():
            album.append(str(sheet_file))
        album.append(str(dst_path))
        sent_ok = False
        try:
            await event.client.send_file(
                offer["chat_id"],
                album,
                force_document=False,
                supports_streaming=True,
                progress_callback=lambda current, total: progress.report("pack_media", current, total),
            )
            sent_ok = True
        except Exception as exc:
            logging.warning("remux album failed: %s", exc)
            try:
                kwargs: dict[str, Any] = {
                    "force_document": False,
                    "supports_streaming": True,
                    "progress_callback": lambda current, total: progress.report("pack_media", current, total),
                }
                if sheet_file and Path(sheet_file).exists():
                    kwargs["thumb"] = str(sheet_file)
                await event.client.send_file(offer["chat_id"], str(dst_path), **kwargs)
                sent_ok = True
            except Exception:
                logging.exception("remux video send failed")
        if not sent_ok:
            await _fallback_merge_as_files(
                event,
                offer,
                reason="封装后的视频发送失败",
                progress=progress,
                already_answered=True,
            )
            return
        await progress.finish("已封装为可播放视频并合并发送。")
    except Exception:
        logging.exception("remux-pack crashed")
        await _fallback_merge_as_files(
            event,
            offer,
            reason="无损封装失败，不能直接复制进可播放格式",
            progress=progress,
            already_answered=True,
        )
    finally:
        if joined and queue is not None:
            await queue.leave()
        _drop_pack_offer(token)


async def _handle_pack_callback(event: events.CallbackQuery.Event) -> None:
    raw = (event.data or b"").decode("utf-8", "replace")
    parts = raw.split(":")
    if len(parts) != 3 or parts[0] != "pack" or parts[1] not in {"y", "n", "r"}:
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
    if action == "r":
        action = "y"
    source = offer["source"]
    result = offer["result"]
    source_is_file = getattr(source, "video", None) is None
    result_is_file = (
        getattr(result, "photo", None) is None
        and getattr(result, "document", None) is not None
    )
    use_files = source_is_file or result_is_file
    if use_files:
        await event.answer("将以文件形式发送，请稍候")
        with contextlib.suppress(Exception):
            await event.edit(
                "图片无法和文件视频合成相册，将以文件形式发送（仍是一条消息）。\n"
                "大文件可能较慢，请稍候…",
                buttons=None,
            )
        progress_stage = "pack"
    else:
        await event.answer("正在合并，请稍候")
        with contextlib.suppress(Exception):
            await event.edit("正在把缩略图和视频合成一条消息…", buttons=None)
        progress_stage = "pack_media"
    sheet_file = offer.get("sheet_path")
    sheet = None
    if use_files and sheet_file and Path(sheet_file).exists():
        sheet = str(sheet_file)
    progress = ProgressReporter(event.client, offer["chat_id"], event.message_id)
    try:
        await progress.show_now(progress_stage, 0, 1)
        sent_ok = await _send_pack_album(
            event.client,
            offer["chat_id"],
            sheet,
            source,
            result_msg=result,
            as_files=use_files,
            progress=progress,
        )
    except Exception:
        logging.exception("pack-forward crashed")
        sent_ok = False
    if not sent_ok:
        await progress.finish("合并发送失败，请稍后重试。")
        return
    _drop_pack_offer(token)
    await progress.finish(
        "已合并为一条文件消息，可长按转发。"
        if progress_stage == "pack"
        else "已合并为一条消息，可长按转发。"
    )


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


def _png_to_jpeg(src: Path, dst: Path) -> None:
    """Turn the sheet into RGB JPEG so Telethon/Pillow skips the slow PNG re-encode."""
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-q:v",
            "2",
            str(dst),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def format_progress(stage: str, current: int, total: int) -> str:
    labels = {
        "metadata": "正在读取视频信息",
        "frames": "正在截取视频帧",
        "compose": "正在合成缩略图",
        "prepare_upload": "正在准备上传图片",
        "upload": "正在上传成品图片",
        "pack": "正在以文件形式发送（图片+原视频）",
        "pack_media": "正在合并为一条媒体消息",
        "remux_dl": "正在下载原文件以便封装",
        "remux": "正在无损封装为可播放视频",
    }
    label = labels.get(stage, "正在处理")
    ratio = min(1.0, max(0.0, current / total)) if total > 0 else 0.0
    filled = round(ratio * 12)
    bar = "#" * filled + "-" * (12 - filled)
    percent = round(ratio * 100)
    if stage == "frames" and total > 0:
        detail = f" {current}/{total}"
    elif stage in {"upload", "pack", "pack_media", "remux_dl"} and total > 0:
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
        *,
        quote_top_id: int | None = None,
        peer: Any = None,
    ) -> QueueTicket:
        ticket = QueueTicket(client, chat_id, filename)
        async with self._lock:
            ahead = self._running + len(self._waiters)
            self._waiters.append(ticket)
        try:
            if ahead > 0:
                try:
                    if peer is not None:
                        msg = await _send_text_quoting(
                            client,
                            peer,
                            format_queue_status(ahead, filename),
                            reply_to,
                            quote_top_id,
                        )
                    else:
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

    async def dismiss(self) -> None:
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        with contextlib.suppress(Exception):
            await self.client.delete_messages(self.chat_id, [self.message_id])

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


_reuse_offers: dict[str, dict] = {}
REUSE_TTL_SEC = 60 * 60


def _drop_reuse_offer(token: str) -> dict | None:
    return _reuse_offers.pop(token, None)


def _purge_reuse_offers() -> None:
    now = time.time()
    expired = [key for key, item in _reuse_offers.items() if item.get("expires", 0) < now]
    for key in expired:
        _drop_reuse_offer(key)


async def _offer_previous_result(
    client: TelegramClient,
    message: Any,
    record: dict,
    key: str,
    requester_id: int | None = None,
    enqueue_kwargs: dict | None = None,
    mention_html: str | None = None,
) -> None:
    _purge_reuse_offers()
    token = secrets.token_hex(4)
    owner = requester_id if requester_id is not None else message.sender_id
    _reuse_offers[token] = {
        "user_id": owner,
        "message": message,
        "record": record,
        "file_key": key,
        "expires": time.time() + REUSE_TTL_SEC,
        "enqueue_kwargs": enqueue_kwargs or {},
        "group": requester_id is not None,
        "mention_html": mention_html or "",
        "reply_to": getattr(message, "id", None),
    }
    text = "这个视频已经有生成记录，别人生成过的也可以用。\n要直接使用之前的缩略图，还是重新生成？"
    if mention_html:
        text = f"{mention_html}\n{text}"
    await client.send_message(
        message.chat_id,
        text,
        buttons=[[
            Button.inline("使用之前的", f"reuse:y:{token}".encode()),
            Button.inline("重新生成", f"reuse:n:{token}".encode()),
        ]],
        reply_to=message.id,
        parse_mode="html" if mention_html else None,
    )


async def _saved_message(client: TelegramClient, chat_id: int, message_id: int):
    found = await client.get_messages(int(chat_id), ids=int(message_id))
    if isinstance(found, list):
        return found[0] if found else None
    return found


async def _forward_saved_result(client: TelegramClient, chat_id: int, record: dict) -> bool:
    """Send the saved sheet and source video as one album, including another user's result."""
    try:
        image = await _saved_message(client, record["image_chat_id"], record["image_message_id"])
        video = await _saved_message(client, record["source_chat_id"], record["source_message_id"])
    except Exception:
        logging.warning("saved result lookup failed", exc_info=True)
        return False
    if image is None or video is None:
        return False
    if await _send_pack_album(client, chat_id, None, video, result_msg=image, as_files=False):
        return True
    try:
        await client.send_file(chat_id, [image, video], force_document=True)
        return True
    except Exception:
        logging.warning("saved result album failed", exc_info=True)
        return False


async def _send_saved_sheet(
    client: TelegramClient,
    chat_id: int,
    record: dict,
    *,
    reply_to: int,
    mention_html: str = "",
    video: Any = None,
) -> bool:
    try:
        image = await _saved_message(client, record["image_chat_id"], record["image_message_id"])
    except Exception:
        logging.warning("saved sheet lookup failed", exc_info=True)
        return False
    if image is None:
        return False
    caption = mention_html or ""
    try:
        await client.send_file(
            chat_id,
            image,
            caption=caption or None,
            reply_to=int(reply_to),
            parse_mode="html" if mention_html else None,
        )
        return True
    except Exception:
        logging.warning("saved sheet reply failed", exc_info=True)
        return False


async def process_message(
    client: TelegramClient,
    media_server: MTProtoRangeServer,
    settings: Settings,
    preferences: UserPreferences,
    queue: JobQueue,
    message: Any,
    access: AccessControl | None = None,
    forward: "ForwardGate | None" = None,
    task_log: TaskLog | None = None,
    preference_user_id: int | None = None,
    offer_pack: bool = True,
    quote_msg_id: int | None = None,
    quote_top_id: int | None = None,
    command_msg_id: int | None = None,
    header_msg_id: int | None = None,
    mention_user_id: int | None = None,
    mention_label: str | None = None,
) -> None:
    chat_id = message.chat_id
    message_id = message.id
    reply_id = quote_msg_id or message_id
    video = extract_video(message)
    if not video:
        await client.send_message(chat_id, "请发送视频文件，或把视频作为 document 发送给我。", reply_to=message_id)
        return

    filename = video.get("file_name") or "telegram-video.mp4"
    peer = await _input_peer(client, message, chat_id) if quote_msg_id else None
    ticket = await queue.join(
        client, chat_id, reply_id, Path(filename).name,
        quote_top_id=quote_top_id, peer=peer,
    )
    try:
        pref_id = preference_user_id if preference_user_id is not None else message.sender_id
        preset = preferences.get(pref_id)
        delivery = preferences.get_delivery(pref_id)
        theme = preferences.get_theme(pref_id)
        tmp_dir = Path(tempfile.mkdtemp(prefix="vthumb_bot_"))
        range_key: str | None = None
        progress: ProgressReporter | None = None
        try:
            if quote_msg_id and ticket.status_id is None and peer is not None:
                opening = await _send_text_quoting(
                    client,
                    peer,
                    "收到，准备按需读取视频分块。大视频不会预先完整下载。",
                    reply_id,
                    quote_top_id,
                )
                progress = ProgressReporter(client, chat_id, opening.id)
            else:
                progress = await ProgressReporter.create(
                    client, chat_id, reply_id, existing_id=ticket.status_id
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
            send_path = output
            caption_name = f"{source.filename}.png"
            if delivery != DELIVERY_FILE:
                jpeg = output.with_suffix(".jpg")
                await progress.show_now("prepare_upload", 0, 1)
                try:
                    await asyncio.to_thread(_png_to_jpeg, output, jpeg)
                    if jpeg.is_file() and jpeg.stat().st_size > 0:
                        send_path = jpeg
                        caption_name = f"{source.filename}.jpg"
                except Exception:
                    logging.warning("sheet jpeg convert failed, uploading png", exc_info=True)
            output_size = send_path.stat().st_size
            await progress.show_now("upload", 0, output_size)
            if quote_msg_id:
                mention = _html_mention(mention_user_id, mention_label)
                sheet_caption = f"{mention}\n{html.escape(caption_name)}" if mention else caption_name
                sent = await client.send_file(
                    chat_id,
                    str(send_path),
                    caption=sheet_caption,
                    force_document=delivery == DELIVERY_FILE,
                    reply_to=int(quote_msg_id),
                    parse_mode="html",
                    progress_callback=lambda current, total: progress.report("upload", current, total),
                )
                reply_hdr = getattr(sent, "reply_to", None) if not isinstance(sent, list) else None
                logging.info(
                    "group sheet reply target=%s attached=%s",
                    quote_msg_id,
                    getattr(reply_hdr, "reply_to_msg_id", None) if reply_hdr else None,
                )
            else:
                sent = await client.send_file(
                    chat_id,
                    str(send_path),
                    caption=caption_name,
                    force_document=delivery == DELIVERY_FILE,
                    reply_to=message_id,
                    parse_mode=None,
                    progress_callback=lambda current, total: progress.report("upload", current, total),
                )
            if quote_msg_id:
                await progress.dismiss()
            else:
                await progress.finish("处理完成，缩略图已发送。")
            if isinstance(sent, list):
                sent = sent[0] if sent else None
            saved_key = file_key(message)
            if task_log is not None and saved_key and sent is not None and getattr(sent, "id", None):
                task_log.remember(
                    saved_key,
                    source_chat_id=chat_id,
                    source_message_id=message_id,
                    image_chat_id=getattr(sent, "chat_id", None) or chat_id,
                    image_message_id=sent.id,
                    content_key=content_key(message),
                )
            if offer_pack:
                with contextlib.suppress(Exception):
                    await _offer_pack_forward(
                        client,
                        message,
                        sent,
                        output,
                        queue=queue,
                        remux_max_mb=settings.remux_max_mb,
                    )
            if access is not None and forward is not None:
                await _forward_note_success(
                    client, access, forward, message, output,
                    requester_id=preference_user_id,
                    requester_label=mention_label,
                )
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
                elif quote_msg_id and peer is not None:
                    await _send_text_quoting(client, peer, error_text, reply_id, quote_top_id)
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
    session_path = Path(settings.access_file).with_name("mtproto")
    session = SQLiteSession(str(session_path))
    # DC1 is much faster for 512KB chunk reads than a fresh login on DC5.
    if not session.auth_key:
        session.set_dc(1, "149.154.175.53", 443)
    mt_client = TelegramClient(
        session,
        settings.api_id,
        settings.api_hash,
        proxy=settings.mt_proxy,
        receive_updates=True,
        connection=ConnectionTcpKeepalive,
        auto_reconnect=True,
    )
    _install_fast_upload(mt_client)
    media_server = MTProtoRangeServer(
        mt_client,
        cache_bytes=settings.range_cache_mb * 1024 * 1024,
    )
    queue = JobQueue(settings.max_concurrent_jobs)
    forward = ForwardGate(Path(settings.access_file).with_name("forward.json"))
    task_log = TaskLog(
        Path(settings.access_file).with_name("task_log.json"),
        daily_limit=settings.guest_daily_limit,
    )
    global _audience
    _audience = Audience(Path(settings.access_file).with_name("audience.json"))
    groups = GroupAccess(Path(settings.access_file).with_name("groups.json"))
    tasks: set[asyncio.Task[None]] = set()

    def _start_generation(
        message: Any,
        *,
        preference_user_id: int | None = None,
        offer_pack: bool = True,
        quote_msg_id: int | None = None,
        quote_top_id: int | None = None,
        command_msg_id: int | None = None,
        header_msg_id: int | None = None,
        mention_user_id: int | None = None,
        mention_label: str | None = None,
    ) -> None:
        user_id = preference_user_id if preference_user_id is not None else message.sender_id
        if user_id is not None and not access.has_grant(user_id):
            task_log.consume(user_id)
        task = asyncio.create_task(
            process_message(
                mt_client, media_server, settings, preferences, queue, message,
                access=access, forward=forward, task_log=task_log,
                preference_user_id=preference_user_id,
                offer_pack=offer_pack,
                quote_msg_id=quote_msg_id,
                quote_top_id=quote_top_id,
                command_msg_id=command_msg_id,
                header_msg_id=header_msg_id,
                mention_user_id=mention_user_id,
                mention_label=mention_label,
            )
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @mt_client.on(events.NewMessage(incoming=True))
    async def on_message(event: events.NewMessage.Event) -> None:
        sender = private_sender_id(bool(event.is_private), event.sender_id)
        if sender and _audience is not None:
            _audience.note(sender)
        if await handle_command(
            mt_client, settings, access, preferences, event.message,
            queue=queue, forward=forward, task_log=task_log,
            groups=groups, enqueue=_start_generation,
        ):
            return
        if event.is_group:
            return
        if event.sender_id in _announce_wait:
            edited = _announce_edit_text(event.message)
            if edited:
                _announce_wait.discard(event.sender_id)
                await _show_announce_preview(
                    mt_client, event.sender_id, event.chat_id, edited, event.message.id
                )
                return
        # /merge short-circuit: if a merge session is active for this
        # sender, collect the media and DO NOT process_message() it.
        if await _merge_collect_media(
            mt_client, settings, access, preferences, event.message
        ):
            return
        video = extract_video(event.message)
        if video is None:
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
                    access=access, forward=forward, task_log=task_log,
                )
            )
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            return
        key = file_key(event.message)
        record = task_log.lookup(key, content_key(event.message))
        if record:
            await _offer_previous_result(mt_client, event.message, record, key or "")
            return
        if not access.has_grant(event.sender_id) and not task_log.can_generate(event.sender_id):
            await mt_client.send_message(
                event.chat_id,
                quota_message(settings.guest_daily_limit, settings.community_url),
                reply_to=event.message.id,
            )
            return
        _start_generation(event.message)

    @mt_client.on(events.CallbackQuery(pattern=b"^reuse:"))
    async def on_reuse(event: events.CallbackQuery.Event) -> None:
        raw = (event.data or b"").decode("utf-8", "replace")
        parts = raw.split(":")
        if len(parts) != 3 or parts[0] != "reuse" or parts[1] not in {"y", "n"}:
            await event.answer("按钮已失效。", alert=True)
            return
        action, token = parts[1], parts[2]
        offer = _reuse_offers.get(token)
        if offer is None or offer.get("expires", 0) < time.time():
            _drop_reuse_offer(token)
            await event.answer("这条询问已过期。", alert=True)
            return
        if event.sender_id != offer["user_id"]:
            await event.answer("这不是你的任务。", alert=True)
            return
        message = offer["message"]
        requester = offer["user_id"]
        if action == "y":
            _drop_reuse_offer(token)
            if offer.get("group"):
                ok = await _send_saved_sheet(
                    mt_client,
                    message.chat_id,
                    offer["record"],
                    reply_to=offer.get("reply_to") or message.id,
                    mention_html=offer.get("mention_html") or "",
                    video=message,
                )
                done = "已把之前的缩略图发给你，并引用了原视频。这次不计入生成次数。"
            else:
                ok = await _forward_saved_result(mt_client, message.chat_id, offer["record"])
                done = "已把之前的图片和视频合并成一条发给你。这次不计入生成次数。"
            await event.answer("已发送" if ok else "发送失败")
            with contextlib.suppress(Exception):
                await event.edit(
                    done if ok else "之前的记录已失效，无法发送。可以再发一次并选择重新生成。",
                    buttons=None,
                )
            return
        if not access.has_grant(requester) and not task_log.can_generate(requester):
            await event.answer("今天的次数已用完，可先 /checkin", alert=True)
            with contextlib.suppress(Exception):
                await event.edit(quota_message(settings.guest_daily_limit, settings.community_url), buttons=None)
            return
        _drop_reuse_offer(token)
        await event.answer("开始重新生成")
        note = "好的，重新生成。" if access.has_grant(requester) else "好的，重新生成。这次会计入次数。"
        with contextlib.suppress(Exception):
            await event.edit(note, buttons=None)
        _start_generation(message, **(offer.get("enqueue_kwargs") or {}))

    @mt_client.on(events.CallbackQuery(pattern=b"^ann:"))
    async def on_announce(event: events.CallbackQuery.Event) -> None:
        raw = (event.data or b"").decode("utf-8", "replace")
        parts = raw.split(":")
        if len(parts) != 3 or parts[0] != "ann" or parts[1] not in {"y", "e"}:
            await event.answer("按钮已失效。", alert=True)
            return
        action, token = parts[1], parts[2]
        draft = _announce_drafts.get(token)
        if draft is None or event.sender_id != draft.get("admin_id") or not access.is_admin(event.sender_id):
            await event.answer("这不是你的公告。", alert=True)
            return
        if action == "e":
            _announce_drafts.pop(token, None)
            _announce_wait.add(event.sender_id)
            await event.answer("请发送新内容")
            with contextlib.suppress(Exception):
                await event.edit(ANNOUNCE_HINT, buttons=None)
            return
        text = draft["text"]
        publisher = draft["admin_id"]
        _announce_drafts.pop(token, None)
        recipients = [
            user_id
            for user_id in (_audience.ids() if _audience is not None else [])
            if user_id != publisher
        ]
        await event.answer("开始发布")
        with contextlib.suppress(Exception):
            await event.edit(f"正在发布公告… 共 {len(recipients)} 人", buttons=None)
        sent, failed = await _publish_announce(mt_client, text, recipients, _audience)
        with contextlib.suppress(Exception):
            await event.edit(f"公告已发布。成功 {sent} 人，失败 {failed} 人。")

    @mt_client.on(events.CallbackQuery(pattern=b"^pack:"))
    async def on_pack_offer(event: events.CallbackQuery.Event) -> None:
        await _handle_pack_callback(event)

    @mt_client.on(events.CallbackQuery(pattern=b"^(setting|delivery|theme|fwdgate):"))
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
        admin_forward = forward.enabled if access.is_admin(user_id) and forward is not None else None
        if event.data.startswith(b"fwdgate:"):
            if not access.is_admin(user_id) or forward is None:
                await event.answer("没有权限。", alert=True)
                return
            await forward.set_enabled(not forward.enabled)
            admin_forward = forward.enabled
            await event.answer("已打开" if forward.enabled else "已关闭")
            await event.edit(
                setting_text(preset, delivery, theme),
                buttons=setting_buttons(preset, delivery, theme, admin_forward=admin_forward),
            )
            return
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
                buttons=setting_buttons(preset, delivery, theme, admin_forward=admin_forward),
            )

    try:
        await mt_client.start(bot_token=settings.bot_token)
        await refresh_private_audience(mt_client, _audience)
        await set_bot_commands(mt_client)
        await media_server.start()
        logging.info(
            "vthumb Telegram bot started with MTProto range reading. upload_part_size_kb=%s tcp_keepalive=on",
            UPLOAD_PART_SIZE_KB,
        )
        await mt_client.run_until_disconnected()
    finally:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await media_server.stop()
        await mt_client.disconnect()


if __name__ == "__main__":
    asyncio.run(poll())
