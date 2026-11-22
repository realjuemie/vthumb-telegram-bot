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
from typing import Any, NamedTuple

from telethon import Button, TelegramClient, events, functions, types
from telethon.errors import FloodWaitError
from telethon.network.connection.tcpfull import ConnectionTcpFull
from telethon.sessions import SQLiteSession

from .access_control import AccessControl, GroupAccess
from .adsgram import consume_claims, fetch_ad, numeric_block_id
from .announce import Audience, announce_body, private_sender_id
from .delivery_store import AnnounceLedger, ForwardStore
from .flood_gate import EDIT as FLOOD_EDIT
from .flood_gate import MEDIA as FLOOD_MEDIA
from .flood_gate import FloodGate, describe_wait, resume_clock
from .mtproto_range import FetchBudgetExceeded, MTProtoRangeServer
from .remux import evaluate_copy_remux, probe_streams, remux_copy_mp4
from .settings import Settings
from .stars import PACKS, StarsGate, StarsLedger, precheck_error, refund_charge, send_pack_invoice
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
from .i18n import (
    DEFAULT_LANG,
    LANG_EN,
    LANG_LABELS,
    LANG_ZH,
    command_pairs,
    delivery_label as delivery_label_for,
    language_buttons,
    menu_actions,
    menu_label,
    menu_rows as localized_menu_rows,
    normalize_lang,
    preset_label,
    t,
    theme_label,
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

USER_COMMANDS = command_pairs(LANG_ZH, ads_enabled=True)
ADMIN_COMMANDS = command_pairs(LANG_ZH, include_admin=True, ads_enabled=True)[len(USER_COMMANDS):]
BOT_COMMANDS = USER_COMMANDS + ADMIN_COMMANDS

# Bottom reply keyboard in private chats. Labels in either language map to the
# same command, so a keyboard left over from the other language still works.
MENU_ACTIONS = menu_actions()

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


def permission_message(user_id: int | None, lang: str | None = None) -> str:
    return t(lang, "perm.denied", user_id=user_id or t(lang, "unknown_id"))


def quota_message(limit: int, community_url: str, ad_reward: int = 5, ads_enabled: bool = False, lang: str | None = None) -> str:
    text = t(lang, "quota.exhausted", limit=limit)
    if ads_enabled:
        text += t(lang, "quota.ad", reward=ad_reward)
    text += t(lang, "quota.contact")
    if community_url:
        text += f"\n{community_url}"
    return text


_audience: Audience | None = None
_preferences: UserPreferences | None = None
_stars: StarsGate | None = None
_star_ledger: StarsLedger | None = None
_announce_wait: set[int] = set()
_bonus_wait: set[int] = set()
_announce_drafts: dict[str, dict] = {}
BONUS_MAX = 100_000
GUEST_CLOSED_TEXT = t(LANG_ZH, "guest.closed")
ANNOUNCE_HINT = t(LANG_ZH, "announce.hint")
# Ad reward buttons stay off until they are turned back on. /ad still exists in code.
SHOW_AD_BUTTON = False


def stars_open() -> bool:
    return _stars is not None and _stars.enabled


def sponsor_pack_buttons(lang: str | None):
    rows = []
    for pack_id, pack in PACKS.items():
        rows.append([
            Button.inline(
                t(lang, "sponsor.pack", stars=pack["stars"], credits=pack["credits"]),
                data=f"star:{pack_id}".encode("ascii"),
            )
        ])
    return rows


def user_lang(user_id: int | None) -> str:
    if _preferences is None or user_id is None:
        return DEFAULT_LANG
    return _preferences.get_lang(user_id)


async def _offer_sponsor(client, access, guest_gate, message, lang, user_id) -> None:
    if not _is_private_message(message):
        await client.send_message(message.chat_id, t(lang, "sponsor.private"), reply_to=message.id)
        return
    if not stars_open():
        await client.send_message(message.chat_id, t(lang, "sponsor.off"), reply_to=message.id, buttons=main_menu(access.is_admin(user_id), lang))
        return
    if user_id is None or access.has_grant(user_id) or access.is_admin(user_id):
        await client.send_message(message.chat_id, t(lang, "sponsor.unlimited"), reply_to=message.id, buttons=main_menu(access.is_admin(user_id), lang))
        return
    if guests_closed(access, guest_gate, user_id):
        await client.send_message(message.chat_id, t(lang, "sponsor.closed_guest"), reply_to=message.id, buttons=main_menu(False, lang))
        return
    await client.send_message(message.chat_id, t(lang, "sponsor.pick"), reply_to=message.id, buttons=sponsor_pack_buttons(lang))


async def _credit_star_payment(client, message, task_log: TaskLog) -> None:
    action = getattr(message, "action", None)
    if not isinstance(action, types.MessageActionPaymentSentMe) or _star_ledger is None:
        return
    found = None
    payload = getattr(action, "payload", b"")
    from .stars import pack_from_payload
    found = pack_from_payload(payload)
    if found is None or getattr(action, "currency", "") != "XTR":
        return
    pack_id, pack = found
    if int(getattr(action, "total_amount", 0) or 0) != pack["stars"]:
        return
    charge = getattr(action, "charge", None)
    charge_id = getattr(charge, "id", None)
    user_id = getattr(message, "sender_id", None)
    if not charge_id or not user_id:
        logging.warning("star payment missing charge or sender")
        return
    if not _star_ledger.grant(str(charge_id), int(user_id), pack_id, pack["stars"], pack["credits"]):
        return
    balance = task_log.add_sponsor(int(user_id), pack["credits"])
    lang = user_lang(int(user_id))
    token = _star_ledger.token_for(str(charge_id))
    buttons = None
    if token:
        buttons = [[Button.inline(t(lang, "sponsor.refund"), data=f"sref:{token}".encode("ascii"))]]
    await client.send_message(
        int(user_id),
        t(lang, "sponsor.paid", credits=pack["credits"], balance=balance),
        buttons=buttons,
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
        await client.send_message(
            message.chat_id,
            ANNOUNCE_HINT,
            reply_to=message.id,
            buttons=_reply_menu(message, True),
        )
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


def _is_private_message(message: Any) -> bool:
    return getattr(message, "is_private", None) is True


_ads_enabled = False


def menu_rows(is_admin: bool, ads_enabled: bool | None = None, lang: str | None = None) -> tuple[tuple[str, ...], ...]:
    enabled = _ads_enabled if ads_enabled is None else ads_enabled
    return localized_menu_rows(is_admin, lang, ads_enabled=enabled, stars_enabled=stars_open())


def menu_action(text: str | None) -> str | None:
    if not text:
        return None
    return MENU_ACTIONS.get(text.strip())


def private_menu_action(message: Any) -> str | None:
    """Button taps are plain text. Ignore them on media and outside private chats."""
    if not _is_private_message(message):
        return None
    if getattr(message, "media", None) or extract_video(message):
        return None
    return menu_action(getattr(message, "raw_text", None))


def main_menu(is_admin: bool, lang: str | None = None):
    rows = []
    for row_index, row in enumerate(menu_rows(is_admin, ads_enabled=SHOW_AD_BUTTON, lang=lang)):
        buttons = []
        for col_index, label in enumerate(row):
            if row_index == 0 and col_index == 0:
                buttons.append(Button.text(label, resize=True))
            else:
                buttons.append(Button.text(label))
        rows.append(buttons)
    return rows


def _reply_menu(message: Any, is_admin: bool, buttons=None, lang: str | None = None):
    if buttons is not None or not _is_private_message(message):
        return buttons
    return main_menu(is_admin, lang)


class _AsCommand:
    """Make a button tap look like the slash command it stands for."""

    def __init__(self, message: Any, raw_text: str) -> None:
        self._message = message
        self.raw_text = raw_text

    def __getattr__(self, name: str):
        return getattr(self._message, name)


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
    import urllib.error
    import urllib.request

    body = json.dumps({"chat_id": chat_id, "message_id": message_id}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/deleteMessage",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"deleteMessage HTTP {exc.code}") from None
    if not payload.get("ok"):
        raise RuntimeError("deleteMessage rejected")


_bot_token = ""


async def delete_own_message(client: TelegramClient, chat_id: int, message_id: int) -> bool:
    """Remove a message this bot sent.

    In a group, Telethon delete_messages often returns without deleting the
    bot's own message. Bot API deleteMessage is the call that actually works.
    """
    if _bot_token:
        try:
            await asyncio.to_thread(_botapi_delete_sync, _bot_token, int(chat_id), int(message_id))
            return True
        except Exception as exc:
            logging.warning(
                "Bot API delete failed for %s/%s (%s)",
                chat_id,
                message_id,
                type(exc).__name__ if not str(exc).startswith("deleteMessage") else exc,
            )
    try:
        await client.delete_messages(chat_id, [int(message_id)])
        return True
    except Exception as exc:
        logging.warning("Could not delete message %s/%s (%s)", chat_id, message_id, type(exc).__name__)
        return False


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
    preferences: UserPreferences | None = None,
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
            lang=preferences.get_lang(message.sender_id) if preferences is not None else None,
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


class AnnounceResult(NamedTuple):
    """What a broadcast achieved. `wait` > 0 means Telegram stopped it."""

    sent: int
    failed: int
    unsent: int = 0
    wait: int = 0


async def _publish_announce(
    client: TelegramClient,
    text: str,
    recipients: list[int],
    audience: Audience | None = None,
) -> "AnnounceResult":
    # Telegram shares one send deadline across SendMessage and SendMedia, so a
    # blocked account drops every recipient. Trying anyway only extends it.
    wait = _flood.remaining(FLOOD_MEDIA)
    if wait > 0:
        return AnnounceResult(0, 0, len(recipients), wait)
    sent = 0
    failed = 0
    for index, user_id in enumerate(recipients):
        try:
            await client.send_message(user_id, text, parse_mode="md", link_preview=False)
            sent += 1
            if _announce_ledger is not None:
                _announce_ledger.mark(text, user_id)
        except FloodWaitError as exc:
            wait = _flood.note_error(exc, FLOOD_MEDIA)
            logging.warning(
                "Announce paused for %s; %s recipients not attempted",
                describe_wait(wait),
                len(recipients) - index,
            )
            return AnnounceResult(sent, failed, len(recipients) - index, wait)
        except Exception as exc:
            if audience is not None and type(exc).__name__ in {"PeerIdInvalidError", "UserIsBlockedError"}:
                audience.discard(user_id)
            logging.warning("announce delivery failed for %s", user_id, exc_info=True)
            failed += 1
        await asyncio.sleep(0.05)
    return AnnounceResult(sent, failed)


def command_user_id(text: str) -> int | None:
    parts = text.split()
    if len(parts) != 2:
        return None
    try:
        user_id = int(parts[1])
    except ValueError:
        return None
    return user_id if user_id > 0 else None


def delivery_label(delivery: str, lang: str | None = None) -> str:
    return delivery_label_for(delivery, lang)


def setting_text(preset: ThumbnailPreset, delivery: str, theme: str, lang: str | None = None) -> str:
    code = normalize_lang(lang)
    return (
        t(code, "setting.title") + "\n"
        + t(code, "setting.layout", preset=preset_label(preset.count, preset.cols, preset.rows, code)) + "\n"
        + t(code, "setting.delivery", delivery=delivery_label(delivery, code)) + "\n"
        + t(code, "setting.theme", theme=theme_label(theme, code)) + "\n"
        + t(code, "setting.lang", language=LANG_LABELS[code]) + "\n\n"
        + t(code, "setting.hint")
    )


def setting_buttons(
    current: ThumbnailPreset,
    delivery: str,
    theme: str,
    admin_forward: bool | None = None,
    stars_enabled: bool | None = None,
    lang: str | None = None,
) -> list[list[Button]]:
    code = normalize_lang(lang)
    buttons = []
    for preset in PRESETS:
        marker = "✓ " if preset == current else ""
        buttons.append(
            Button.inline(
                marker + preset_label(preset.count, preset.cols, preset.rows, code),
                data=f"setting:{preset.count}:{preset.cols}".encode("ascii"),
            )
        )
    media_marker = "✓ " if delivery == DELIVERY_MEDIA else ""
    file_marker = "✓ " if delivery == DELIVERY_FILE else ""
    delivery_buttons = [
        Button.inline(media_marker + delivery_label(DELIVERY_MEDIA, code), data=b"delivery:media"),
        Button.inline(file_marker + delivery_label(DELIVERY_FILE, code), data=b"delivery:file"),
    ]
    theme_buttons: list[Button] = []
    for theme_name in THEMES:
        marker = "✓ " if theme_name == theme else ""
        theme_buttons.append(
            Button.inline(
                marker + theme_label(theme_name, code),
                data=f"theme:{theme_name}".encode("ascii"),
            )
        )
    theme_rows = [theme_buttons[i : i + 2] for i in range(0, len(theme_buttons), 2)]
    lang_buttons = [
        Button.inline(("✓ " if code == LANG_ZH else "") + LANG_LABELS[LANG_ZH], data=b"lang:zh"),
        Button.inline(("✓ " if code == LANG_EN else "") + LANG_LABELS[LANG_EN], data=b"lang:en"),
    ]
    rows = [
        buttons[:2],
        buttons[2:],
        delivery_buttons,
        *theme_rows,
        lang_buttons,
    ]
    if admin_forward is not None:
        label = t(code, "fwdgate.on") if admin_forward else t(code, "fwdgate.off")
        rows.append([Button.inline(label, data=b"fwdgate:toggle")])
    if stars_enabled is not None:
        label = t(code, "stargate.on") if stars_enabled else t(code, "stargate.off")
        rows.append([Button.inline(label, data=b"stargate:toggle")])
    return rows


def _button_style(data: bytes, text: str) -> str | None:
    """Telegram only allows primary (blue), success (green), and danger (red)."""
    raw = data.decode("ascii", "replace")
    if raw.startswith("setting:"):
        return "primary"
    if raw.startswith("delivery:"):
        return "success"
    if raw.startswith("lang:"):
        return "success" if text.startswith("✓") else "primary"
    if raw.startswith(("fwdgate:", "stargate:")):
        if text.endswith("开") or text.lower().endswith("on"):
            return "success"
        return "danger"
    return None


def inline_keyboard_payload(rows) -> dict:
    keyboard = []
    for row in rows or []:
        api_row = []
        for button in row:
            inner = getattr(button, "button", button)
            data = getattr(inner, "data", b"") or b""
            if isinstance(data, str):
                data = data.encode()
            text = getattr(inner, "text", "") or ""
            item = {"text": text, "callback_data": data.decode("ascii", "replace")}
            style = _button_style(data, text)
            if style:
                item["style"] = style
            api_row.append(item)
        if api_row:
            keyboard.append(api_row)
    return {"inline_keyboard": keyboard}


async def _bot_api(method: str, payload: dict) -> bool:
    """One Bot API call. True on success, including 'message is not modified'."""
    if not _bot_token:
        return False
    import urllib.error
    import urllib.request

    body = json.dumps(payload).encode()
    token = _bot_token

    def _send() -> None:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()

    try:
        await asyncio.to_thread(_send)
        return True
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = exc.read().decode("utf-8", "replace")
        if "message is not modified" in detail:
            return True
        logging.warning("Bot API %s failed (HTTP %s)", method, exc.code)
        return False
    except Exception as exc:
        logging.warning("Bot API %s failed (%s)", method, type(exc).__name__)
        return False


async def show_colored_settings(chat_id: int, text: str, rows, *, message_id: int | None = None, reply_to: int | None = None) -> bool:
    """Send or edit the settings keyboard in one call, colors included.

    A Telethon edit cannot carry button color, so doing that first and painting
    after makes every switch flash back to the default gray.
    """
    payload = {
        "chat_id": int(chat_id),
        "text": text,
        "reply_markup": inline_keyboard_payload(rows),
    }
    if message_id:
        payload["message_id"] = int(message_id)
        return await _bot_api("editMessageText", payload)
    if reply_to:
        payload["reply_parameters"] = {"message_id": int(reply_to)}
    return await _bot_api("sendMessage", payload)


async def paint_inline_styles(chat_id: int, message_id: int, rows) -> None:
    if not rows or not message_id:
        return
    await _bot_api("editMessageReplyMarkup", {
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "reply_markup": inline_keyboard_payload(rows),
    })


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


def lang_from_callback(data: bytes) -> str | None:
    try:
        prefix, lang = data.decode("ascii").split(":", 1)
    except (UnicodeDecodeError, ValueError):
        return None
    if prefix not in {"lang", "pick"}:
        return None
    code = normalize_lang(lang)
    if lang not in {LANG_ZH, LANG_EN}:
        return None
    return code


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
    guest_gate: "GuestGate | None" = None,
) -> bool:
    command = command_name(message.raw_text)
    if command is None:
        return False
    if _is_group_message(message):
        mention = command_mention(message.raw_text)
        if mention and mention != await _bot_username(client):
            return True

    user_id = message.sender_id
    lang = preferences.get_lang(user_id)
    is_admin = access.is_admin(user_id)
    is_allowed = access.is_allowed(user_id)
    buttons = None
    if user_id is not None and command != "bonus":
        _bonus_wait.discard(user_id)

    if (
        command == "start"
        and _is_private_message(message)
        and user_id is not None
        and not preferences.has_chosen_lang(user_id)
    ):
        await client.send_message(
            message.chat_id,
            t(LANG_ZH, "pick.prompt"),
            reply_to=message.id,
            buttons=language_buttons(),
        )
        return True

    if command in {"start", "setting"} and _is_private_message(message) and user_id is not None:
        await sync_user_commands(client, user_id, lang, is_admin=is_admin, ads_enabled=SHOW_AD_BUTTON)

    if command == "start":
        if is_allowed:
            text = t(lang, "start.welcome")
        else:
            text = permission_message(user_id, lang)
    elif command == "help":
        if not is_allowed:
            text = permission_message(user_id, lang) + "\n\n" + t(lang, "perm.id_again")
        else:
            text = (
                t(lang, "help.lead")
                + t(lang, "help.1")
                + t(lang, "help.2")
                + t(lang, "help.3")
                + t(lang, "help.4", small=settings.small_file_full_read_mb)
                + t(
                    lang,
                    "help.5",
                    initial=f"{settings.max_source_fetch_ratio:.0%}",
                    hard=f"{settings.hard_source_fetch_ratio:.0%}",
                    cap=settings.max_source_fetch_mb,
                )
                + t(lang, "help.6")
                + t(lang, "help.7")
                + t(lang, "help.8")
                + t(lang, "help.9")
            )
            if settings.ads_live and SHOW_AD_BUTTON:
                text += t(lang, "help.10")
            if not _is_group_message(message):
                text += t(lang, "help.buttons")
            if is_admin:
                text += t(lang, "help.admin")
                if not _is_group_message(message):
                    text += t(lang, "help.admin_buttons")
            if not access.has_grant(user_id):
                text += t(lang, "help.guest", limit=settings.guest_daily_limit)
                if settings.ads_live and SHOW_AD_BUTTON:
                    text += t(lang, "help.guest_ad", reward=settings.ad_reward)
                if guests_closed(access, guest_gate, user_id):
                    text += "\n\n" + t(lang, "guest.closed")
    elif command == "id":
        text = t(lang, "id.line", user_id=user_id or t(lang, "unknown_id"))
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
            checkin=task_log.checkin_credits(user_id) if task_log is not None and user_id is not None else 0,
            gift=task_log.gift_credits(user_id) if task_log is not None and user_id is not None else 0,
            sponsor=task_log.sponsor_credits(user_id) if task_log is not None and user_id is not None else 0,
            checked_in=task_log.checked_in_on(user_id) if task_log is not None and user_id is not None else False,
            guest_open=not guests_closed(access, guest_gate, user_id),
            lang=lang,
        )
    elif command == "checkin":
        if guests_closed(access, guest_gate, user_id):
            text = t(lang, "guest.closed")
        elif task_log is None or user_id is None:
            text = t(lang, "checkin.unavailable")
        else:
            result = task_log.check_in(user_id)
            if result is None:
                text = t(lang, "checkin.already", stored=task_log.checkin_credits(user_id))
            else:
                gained, total = result
                text = t(lang, "checkin.ok", gained=gained, total=total, limit=settings.guest_daily_limit)
    elif command == "ad":
        if _is_group_message(message):
            return True
        if not SHOW_AD_BUTTON or not settings.ads_enabled:
            text = t(lang, "ad.disabled")
        elif not settings.ads_live:
            text = t(lang, "ad.not_live")
        elif guests_closed(access, guest_gate, user_id):
            text = t(lang, "guest.closed")
        else:
            await _send_watch_ad(client, settings, message, is_admin, lang)
            return True
    elif command == "status":
        if not is_allowed:
            text = permission_message(user_id, lang)
        else:
            preset = preferences.get(user_id)
            delivery = preferences.get_delivery(user_id)
            text = t(
                lang,
                "status.body",
                preset=preset_label(preset.count, preset.cols, preset.rows, lang),
                width=settings.thumb_width,
                delivery=delivery_label(delivery, lang),
                cache=settings.range_cache_mb,
                small=settings.small_file_full_read_mb,
                initial=f"{settings.max_source_fetch_ratio:.0%}",
                hard=f"{settings.hard_source_fetch_ratio:.0%}",
                cap=settings.max_source_fetch_mb,
                running=queue.running if queue else 0,
                waiting=queue.waiting if queue else 0,
                concurrency=queue.concurrency if queue else settings.max_concurrent_jobs,
            )
    elif command == "setting":
        if not is_allowed:
            text = permission_message(user_id, lang)
        else:
            preset = preferences.get(user_id)
            delivery = preferences.get_delivery(user_id)
            theme = preferences.get_theme(user_id)
            text = setting_text(preset, delivery, theme, lang)
            buttons = setting_buttons(
                preset,
                delivery,
                theme,
                admin_forward=forward.enabled if is_admin and forward is not None else None,
                stars_enabled=_stars.enabled if is_admin and _stars is not None else None,
                lang=lang,
            )
    elif command in {"add", "del", "users"}:
        if not is_admin:
            text = t(lang, "admin.only")
        elif command == "users":
            users = access.users()
            text = t(lang, "users.admins", ids=", ".join(str(item) for item in sorted(access.admin_ids)))
            text += "\n" + t(
                lang,
                "users.allowed",
                ids=", ".join(str(item) for item in users) if users else t(lang, "users.none"),
            )
        else:
            target_id = command_user_id(message.raw_text)
            if target_id is None:
                text = t(lang, "users.bad_format", command=command)
            elif command == "add":
                added = await access.add(target_id)
                text = t(lang, "users.added" if added else "users.already", user_id=target_id)
            elif target_id in access.admin_ids:
                text = t(lang, "users.admin_stuck")
            else:
                removed = await access.remove(target_id)
                text = t(lang, "users.removed" if removed else "users.missing", user_id=target_id)
    elif command == "guest":
        if not is_admin or guest_gate is None:
            text = t(lang, "admin.only")
        else:
            arg = ""
            parts = (message.raw_text or "").split()
            if len(parts) > 1:
                arg = parts[1].lower()
            if arg in {"off", "0", "false", "关"}:
                await guest_gate.set_enabled(False)
            elif arg in {"on", "1", "true", "开"}:
                await guest_gate.set_enabled(True)
            elif arg in {"toggle", "切换"}:
                await guest_gate.set_enabled(not guest_gate.enabled)
            known = len(ungranted_user_ids(access, task_log, preferences))
            text = guest_switch_text(guest_gate.enabled, known, lang)
    elif command == "bonus":
        if not is_admin or task_log is None:
            text = t(lang, "admin.only")
        else:
            amount = bonus_amount(message.raw_text)
            bonus_labels = {"/bonus", menu_label("bonus", LANG_ZH), menu_label("bonus", LANG_EN)}
            if amount is None and (message.raw_text or "").strip() in bonus_labels:
                if user_id is not None:
                    _bonus_wait.add(user_id)
                text = t(lang, "bonus.ask", max=BONUS_MAX)
            elif amount is None:
                text = t(lang, "bonus.bad", max=BONUS_MAX)
            else:
                _bonus_wait.discard(user_id)
                targets = ungranted_user_ids(access, task_log, preferences)
                count = task_log.add_stored_many(targets, amount)
                text = t(lang, "bonus.done", count=count, amount=amount)
    elif command == "merge":
        if guests_closed(access, guest_gate, user_id):
            text = t(lang, "guest.closed")
        else:
            await _merge_handle(client, settings, access, preferences, message)
            return True
    elif command == "announce":
        if not is_admin:
            text = t(lang, "admin.only")
        else:
            await _begin_announce(client, message)
            return True
    elif command == "sponsor":
        await _offer_sponsor(client, access, guest_gate, message, lang, user_id)
        return True
    elif command == "group":
        await _handle_group_command(client, access, groups, message)
        return True
    elif command == "thumb":
        await _handle_thumb_command(client, access, groups, task_log, message, enqueue, preferences)
        return True
    else:
        if _is_group_message(message):
            return True
        text = t(lang, "unknown_command")
    if command == "setting" and buttons is not None and await show_colored_settings(
        message.chat_id, text, buttons, reply_to=message.id,
    ):
        return True
    sent = await client.send_message(
        message.chat_id,
        text,
        reply_to=message.id,
        buttons=_reply_menu(message, is_admin, buttons, lang),
    )
    if command == "setting" and buttons is not None and getattr(sent, "id", None):
        await paint_inline_styles(message.chat_id, sent.id, buttons)
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
    lang = preferences.get_lang(user_id)
    # Allow any authorized user + admin to use /merge. (Same gate as
    # /add etc. is admin-only; /merge is general utility.)
    if not access.is_allowed(user_id) and not access.is_admin(user_id):
        await client.send_message(
            message.chat_id,
            permission_message(user_id, lang),
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
                message.chat_id, t(lang, "merge.none"), reply_to=message.id,
            )
        if state["task"] and not state["task"].done():
            state["task"].cancel()
        return await client.send_message(
            message.chat_id, t(lang, "merge.cancelled"), reply_to=message.id,
        )

    # ── status ──
    if arg == "status":
        state = _pending_merge.get(user_id)
        if not state:
            return await client.send_message(
                message.chat_id, t(lang, "merge.none_now"), reply_to=message.id,
            )
        remain = state["expected_count"] - state["received_count"]
        return await client.send_message(
            message.chat_id,
            t(lang, "merge.status", got=state["received_count"], expected=state["expected_count"], remain=remain),
            reply_to=message.id,
        )

    # ── set N (or default) ──
    if user_id in _pending_merge:
        return await client.send_message(
            message.chat_id,
            t(lang, "merge.busy"),
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
                t(lang, "merge.usage", lo=MERGE_MIN_COUNT, hi=MERGE_MAX_COUNT),
                reply_to=message.id,
            )
        if n < MERGE_MIN_COUNT or n > MERGE_MAX_COUNT:
            return await client.send_message(
                message.chat_id,
                t(lang, "merge.bad_n", lo=MERGE_MIN_COUNT, hi=MERGE_MAX_COUNT),
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
        "lang": lang,
    }
    _pending_merge[user_id] = state

    notify = await client.send_message(
        message.chat_id,
        t(lang, "merge.armed", n=n),
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
            t(state.get("lang"), "merge.wait_next", got=recv, expected=expected, next=recv + 1),
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
        t(state.get("lang"), "merge.soon", got=recv, expected=expected),
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
                t(state.get("lang"), "merge.empty"),
            )
            return

        for batch_start in range(0, len(media), BATCH):
            if _flood.remaining(FLOOD_MEDIA) > 0:
                break
            batch = media[batch_start:batch_start + BATCH]
            try:
                await client.send_file(
                    chat_id,
                    batch,
                    force_document=needs_file_album,
                )
                sent += len(batch)
            except FloodWaitError as exc:
                _flood.note_error(exc, FLOOD_MEDIA)
                break
            except Exception as e:
                err = str(e).lower()
                logging.warning(f"{MERGE_LOG_PREFIX} uid={user_id} batch failed: {e}")
                if _is_flood_error(e):
                    _flood.note_error(e, FLOOD_MEDIA)
                    break
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
                        t(state.get("lang"), "merge.batch_fail", start=batch_start + 1, end=batch_start + len(batch), error=e),
                    )
                    continue
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    mlang = state.get("lang")
    summary = [t(mlang, "merge.summary", expected=state["expected_count"], photos=len(photos), videos=len(videos))]
    if sent:
        summary.append(t(mlang, "merge.sent", sent=sent, total=len(media)))
    if failed_indices:
        idx_list = ", ".join(str(i) for i in failed_indices)
        summary.append("")
        summary.append(t(mlang, "merge.stale", indexes=idx_list))
    try:
        await client.send_message(chat_id, "\n".join(summary))
    except Exception:
        pass

    # Best-effort: delete the original "OK waiting" reply so the chat
    # is tidy.
    notify_id = state.get("notify_msg_id")
    if notify_id is not None:
        try:
            await delete_own_message(client, chat_id, int(notify_id))
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


class GuestGate:
    """Whether non-granted users may generate and check in. Default on."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.enabled = self._load()
        self.lock = asyncio.Lock()

    def _load(self) -> bool:
        if not self.path.exists():
            return True
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return bool(payload.get("enabled", True))
        except (OSError, ValueError, TypeError):
            return True

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


def guests_closed(access: AccessControl, guest_gate: "GuestGate | None", user_id: int | None) -> bool:
    if guest_gate is None or guest_gate.enabled:
        return False
    return not access.has_grant(user_id)


def bonus_amount(text: str | None) -> int | None:
    parts = (text or "").split()
    if len(parts) < 2:
        return None
    return _parse_bonus(parts[1])


def plain_bonus_amount(text: str | None) -> int | None:
    return _parse_bonus((text or "").strip())


def _parse_bonus(raw: str) -> int | None:
    if not raw.isdigit():
        return None
    value = int(raw)
    if value < 1 or value > BONUS_MAX:
        return None
    return value


def ungranted_user_ids(access: AccessControl, task_log: "TaskLog | None", preferences: UserPreferences | None) -> list[int]:
    found: set[int] = set()
    if _audience is not None:
        found.update(int(item) for item in _audience.ids())
    if task_log is not None:
        found.update(task_log.known_user_ids())
    if preferences is not None:
        found.update(int(item) for item in getattr(preferences, "values", {}))
        found.update(int(item) for item in getattr(preferences, "delivery_modes", {}))
        found.update(int(item) for item in getattr(preferences, "theme_modes", {}))
        found.update(int(item) for item in getattr(preferences, "languages", {}))
    return sorted(user_id for user_id in found if user_id > 0 and not access.has_grant(user_id))


def guest_switch_text(enabled: bool, known: int, lang: str | None = None) -> str:
    state = t(lang, "guest.open") if enabled else t(lang, "guest.shut")
    return t(lang, "guest.switch", state=state, known=known)


def account_info_text(
    sender: Any,
    user_id: int | None,
    *,
    granted: bool,
    is_admin: bool,
    used: int,
    limit: int,
    checkin: int = 0,
    gift: int = 0,
    sponsor: int = 0,
    checked_in: bool = False,
    guest_open: bool = True,
    lang: str | None = None,
    stored: int = 0,
) -> str:
    del stored
    name = ""
    username = None
    if sender is not None:
        username = getattr(sender, "username", None)
        first = (getattr(sender, "first_name", None) or "").strip()
        last = (getattr(sender, "last_name", None) or "").strip()
        name = " ".join(part for part in (first, last) if part)
    if is_admin:
        guest_line, grant_line, admin_line = (
            t(lang, "info.role_guest"),
            t(lang, "info.role_grant"),
            t(lang, "info.role_admin_current"),
        )
    elif granted:
        guest_line, grant_line, admin_line = (
            t(lang, "info.role_guest"),
            t(lang, "info.role_grant_current"),
            t(lang, "info.role_admin"),
        )
    else:
        guest_line, grant_line, admin_line = (
            t(lang, "info.role_guest_current"),
            t(lang, "info.role_grant"),
            t(lang, "info.role_admin"),
        )
    lines = [
        t(lang, "info.title"),
        "",
        t(lang, "info.name", name=name or t(lang, "info.unset")),
        t(lang, "info.username", name=f"@{username}") if username else t(lang, "info.no_username"),
        t(lang, "info.id", user_id=user_id or t(lang, "unknown_id")),
        "",
        t(lang, "info.groups"),
        guest_line,
        grant_line,
        admin_line,
        "",
        t(lang, "info.credits"),
    ]
    if is_admin or granted:
        lines.append(t(lang, "info.unlimited"))
        lines.append(t(lang, "info.unlimited_note"))
    else:
        remaining = max(int(limit) - int(used), 0)
        status = t(lang, "info.checked") if checked_in else t(lang, "info.not_checked")
        lines.append(t(lang, "info.daily", remaining=remaining, limit=limit))
        lines.append(t(lang, "info.checkin", balance=checkin, status=status))
        lines.append(t(lang, "info.gift", balance=gift))
        lines.append(t(lang, "info.sponsor", balance=sponsor))
        lines.append(t(lang, "info.spend_order"))
        lines.append(t(lang, "info.reuse_note"))
        if not guest_open:
            lines.append(t(lang, "info.closed"))
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


def format_forward_notice(label: str, user_id: int | None, count: int, lang: str | None = None) -> str:
    safe = html.escape(label or str(user_id or t(lang, "unknown_id")), quote=True)
    if user_id:
        return t(lang, "forward.done", label=safe, user_id=int(user_id), count=count)
    return t(lang, "forward.done_plain", label=safe, count=count)


_pending_forward: dict[int, dict] = {}
_flood_notice_until: dict[int, float] = {}
_forward_store: ForwardStore | None = None
_announce_ledger: AnnounceLedger | None = None
_spool_root: Path | None = None


def _persist_forwards() -> None:
    if _forward_store is not None:
        _forward_store.save(_pending_forward)


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
    keep_dir = (_spool_root or Path(tempfile.gettempdir())) / f"fwd-{actor_id}-{secrets.token_hex(4)}"
    keep_dir.mkdir(parents=True, exist_ok=True)
    kept: Path | None = None
    try:
        dest = keep_dir / png_path.name
        shutil.copy2(png_path, dest)
        kept = dest
    except OSError as exc:
        logging.warning("%s copy sheet failed: %s", FORWARD_LOG_PREFIX, exc)
        shutil.rmtree(keep_dir, ignore_errors=True)
        keep_dir = (_spool_root or Path(tempfile.gettempdir())) / f"fwd-{actor_id}-{secrets.token_hex(4)}"
        keep_dir.mkdir(parents=True, exist_ok=True)
    sender = None
    with contextlib.suppress(Exception):
        sender = await message.get_sender()
    label = requester_label or format_user_label(sender, actor_id)
    state = _pending_forward.setdefault(
        actor_id,
        {"items": [], "dirs": [], "label": label, "task": None},
    )
    state["label"] = label
    state["items"].append({
        "message": message,
        "png": str(kept) if kept is not None else None,
        "source_chat_id": getattr(message, "chat_id", None),
        "source_message_id": getattr(message, "id", None),
    })
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
    _persist_forwards()


async def _forward_flush(client: TelegramClient, access: AccessControl, user_id: int) -> None:
    state = _pending_forward.pop(user_id, None)
    if not state:
        return
    if _flood.remaining(FLOOD_MEDIA) > 0:
        _pending_forward[user_id] = state
        _persist_forwards()
        _arm_forward_retry(client, access, user_id)
        return
    items = state["items"]
    label = state.get("label") or str(user_id)
    count = len(items)
    pngs = [item["png"] for item in items if item.get("png") and Path(item["png"]).exists()]
    videos = []
    for item in items:
        message = item.get("message")
        if message is None and item.get("source_chat_id") and item.get("source_message_id"):
            message = await _saved_message(client, int(item["source_chat_id"]), int(item["source_message_id"]))
        if message is not None:
            videos.append(message)
    batch: list = [str(path) for path in pngs]
    batch.extend(videos)
    chunks = [batch[index:index + 10] for index in range(0, len(batch), 10)]
    sent_admins = set(state.get("sent_admins") or [])
    deferred = False
    for admin_id in sorted(access.admin_ids):
        if admin_id in sent_admins:
            continue
        if _flood.remaining(FLOOD_MEDIA) > 0:
            deferred = True
            break
        notice = format_forward_notice(label, user_id, count, user_lang(admin_id))
        try:
            await client.send_message(admin_id, notice, parse_mode="html")
        except FloodWaitError as exc:
            _flood.note_error(exc, FLOOD_MEDIA)
            deferred = True
            break
        except Exception as exc:
            logging.warning("%s notify admin %s failed: %s", FORWARD_LOG_PREFIX, admin_id, type(exc).__name__)
            sent_admins.add(admin_id)
            continue
        for chunk in chunks:
            if not chunk:
                continue
            if _flood.remaining(FLOOD_MEDIA) > 0:
                deferred = True
                break
            try:
                await client.send_file(admin_id, chunk[0] if len(chunk) == 1 else chunk)
            except FloodWaitError as exc:
                _flood.note_error(exc, FLOOD_MEDIA)
                deferred = True
                break
            except Exception as exc:
                if not _album_may_split(exc):
                    logging.warning("%s album to admin %s failed: %s", FORWARD_LOG_PREFIX, admin_id, type(exc).__name__)
                    continue
                for piece in chunk:
                    if _flood.remaining(FLOOD_MEDIA) > 0:
                        deferred = True
                        break
                    try:
                        await client.send_file(admin_id, piece)
                    except FloodWaitError as inner:
                        _flood.note_error(inner, FLOOD_MEDIA)
                        deferred = True
                        break
                    except Exception as inner:
                        logging.warning("%s piece to admin %s failed: %s", FORWARD_LOG_PREFIX, admin_id, type(inner).__name__)
            if deferred:
                break
        if deferred:
            break
        sent_admins.add(admin_id)
    if deferred:
        state["sent_admins"] = list(sent_admins)
        _pending_forward[user_id] = state
        _persist_forwards()
        _arm_forward_retry(client, access, user_id)
        return
    for item in items:
        png = item.get("png")
        if png:
            Path(png).unlink(missing_ok=True)
    for directory in state.get("dirs", []):
        shutil.rmtree(directory, ignore_errors=True)
    _persist_forwards()


def _album_may_split(exc: BaseException) -> bool:
    """Only a media-group shape error may be retried one file at a time."""
    name = type(exc).__name__
    return name in {"MediaInvalidError", "PhotoInvalidError", "FileReferenceExpiredError"}


def _arm_forward_retry(client: TelegramClient, access: AccessControl, user_id: int) -> None:
    wait = _flood.remaining(FLOOD_MEDIA) + 1

    async def _later() -> None:
        await asyncio.sleep(wait)
        await _forward_flush(client, access, user_id)

    asyncio.create_task(_later())


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


def pack_offer_prompt(is_file_video: bool, remux_max_mb: int, *, allow_remux: bool = False, lang: str | None = None) -> str:
    del remux_max_mb, allow_remux
    if is_file_video:
        return t(lang, "pack.file")
    return t(lang, "pack.inline")


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
            except FloodWaitError as exc:
                _flood.note_error(exc, FLOOD_MEDIA)
                return False
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
    except FloodWaitError as exc:
        _flood.note_error(exc, FLOOD_MEDIA)
        return False
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
    lang: str | None = None,
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
        "lang": lang,
    }
    buttons = [
        [
            Button.inline(t(lang, "pack.yes"), f"pack:y:{token}".encode()),
            Button.inline(t(lang, "pack.no"), f"pack:n:{token}".encode()),
        ]
    ]
    await client.send_message(
        source_msg.chat_id,
        pack_offer_prompt(is_file_video, remux_max_mb, lang=lang),
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
        await event.answer(t(user_lang(event.sender_id), "pack.expired_toast"), alert=True)
        return
    action, token = parts[1], parts[2]
    offer = _pack_offers.get(token)
    plang = (offer or {}).get("lang") or user_lang(event.sender_id)
    if offer is None or offer.get("expires", 0) < time.time():
        _drop_pack_offer(token)
        await event.answer(t(plang, "pack.offer_expired_toast"), alert=True)
        with contextlib.suppress(Exception):
            await event.edit(t(plang, "pack.offer_expired"))
        return
    if event.sender_id != offer["user_id"]:
        await event.answer(t(plang, "pack.not_yours"), alert=True)
        return
    if action == "n":
        _drop_pack_offer(token)
        await event.answer(t(plang, "pack.ok"))
        with contextlib.suppress(Exception):
            await event.edit(t(plang, "pack.kept"))
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
        await event.answer(t(plang, "pack.as_file_toast"))
        with contextlib.suppress(Exception):
            await event.edit(
                t(plang, "pack.as_file"),
                buttons=None,
            )
        progress_stage = "pack"
    else:
        await event.answer(t(plang, "pack.merging_toast"))
        with contextlib.suppress(Exception):
            await event.edit(t(plang, "pack.merging"), buttons=None)
        progress_stage = "pack_media"
    sheet_file = offer.get("sheet_path")
    sheet = None
    if use_files and sheet_file and Path(sheet_file).exists():
        sheet = str(sheet_file)
    progress = ProgressReporter(event.client, offer["chat_id"], event.message_id, plang)
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
        await progress.finish(t(plang, "pack.fail"))
        return
    _drop_pack_offer(token)
    await progress.finish(
        t(plang, "pack.done_file")
        if progress_stage == "pack"
        else t(plang, "pack.done_media")
    )


# Module-level merge state. One pending merge per user at a time.
# Dict keyed by sender_id, value is a dict (see _merge_handle).
_pending_merge: dict[int, dict] = {}


async def sync_user_commands(
    client: TelegramClient,
    user_id: int,
    lang: str | None,
    *,
    is_admin: bool,
    ads_enabled: bool = False,
) -> None:
    """Lock this private chat's slash menu to the in-bot language.

    Telegram otherwise picks the menu from the phone language. Setting the same
    list on every lang_code for this peer stops the phone language from winning.
    """
    try:
        peer = await client.get_input_entity(int(user_id))
        commands = [
            types.BotCommand(command=name, description=description)
            for name, description in command_pairs(lang, include_admin=is_admin, ads_enabled=ads_enabled)
        ]
        for code in ("", LANG_ZH, LANG_EN):
            await client(
                functions.bots.SetBotCommandsRequest(
                    scope=types.BotCommandScopePeer(peer),
                    lang_code=code,
                    commands=commands,
                )
            )
    except Exception:
        logging.warning("Could not set command menu for %s", user_id)


async def set_bot_commands(client: TelegramClient, admin_ids: frozenset[int] | set[int] = frozenset(), ads_enabled: bool = False) -> None:
    def listed(pairs):
        return [types.BotCommand(command=name, description=description) for name, description in pairs]

    # Default scope still follows the phone language, for people who have not
    # opened a private chat since the switch. A private /start or language change
    # then locks that chat to the in-bot language.
    for code in ("", LANG_ZH, LANG_EN):
        catalog = LANG_EN if code == LANG_EN else LANG_ZH
        await client(
            functions.bots.SetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(),
                lang_code=code,
                commands=listed(command_pairs(catalog, ads_enabled=ads_enabled)),
            )
        )
    for admin_id in admin_ids:
        admin_lang = _preferences.get_lang(admin_id) if _preferences is not None else LANG_ZH
        await sync_user_commands(client, int(admin_id), admin_lang, is_admin=True, ads_enabled=ads_enabled)


async def _send_watch_ad(client: TelegramClient, settings: Settings, message: Any, is_admin: bool, lang: str | None = None) -> None:
    chat_id = message.chat_id
    user_id = message.sender_id
    block = numeric_block_id(settings.adsgram_block_id)
    if not block or not settings.adsgram_token or user_id is None:
        await client.send_message(
            chat_id,
            t(lang, "ad.not_connected"),
            reply_to=message.id,
            buttons=_reply_menu(message, is_admin, lang=lang),
        )
        return
    try:
        ad = await asyncio.to_thread(fetch_ad, block, settings.adsgram_token, int(user_id))
    except Exception:
        logging.exception("Could not fetch ad")
        ad = None
    if not ad:
        await client.send_message(
            chat_id,
            "暂时没有广告，请稍后再试。",
            reply_to=message.id,
            buttons=_reply_menu(message, is_admin),
        )
        return
    buttons = [[Button.url(ad["button_name"], ad["click_url"])]]
    if ad["reward_url"]:
        buttons.append([Button.url(ad["button_reward_name"], ad["reward_url"])])
    sent = False
    if ad["image_url"]:
        try:
            await client.send_file(
                chat_id,
                ad["image_url"],
                caption=ad["text_html"],
                parse_mode="html",
                buttons=buttons,
                noforwards=True,
                reply_to=message.id,
            )
            sent = True
        except Exception:
            logging.exception("Could not send ad image")
    if not sent:
        await client.send_message(
            chat_id,
            ad["text_html"],
            parse_mode="html",
            link_preview=False,
            buttons=buttons,
            noforwards=True,
            reply_to=message.id,
        )
    await client.send_message(
        chat_id,
        f"先点广告，再点领取。到账 {settings.ad_reward} 次后我会告诉你。",
        reply_to=message.id,
        buttons=_reply_menu(message, is_admin),
    )


async def _claim_loop(client: TelegramClient, settings: Settings, task_log: TaskLog, access: AccessControl) -> None:
    if not settings.adsgram_claims:
        return
    path = Path(settings.adsgram_claims)
    state = path.with_name("claims.state.json")
    while True:
        try:
            user_ids = await asyncio.to_thread(consume_claims, path, state, int(time.time()))
            for user_id in user_ids:
                task_log.add_stored_many([user_id], settings.ad_reward)
                total = task_log.gift_credits(user_id)
                try:
                    await client.send_message(
                        user_id,
                        t(user_lang(user_id), "ad.credited", reward=settings.ad_reward, total=total),
                        buttons=main_menu(access.is_admin(user_id), user_lang(user_id)),
                    )
                except Exception:
                    logging.info("Could not notify ad reward for %s", user_id)
        except Exception:
            logging.exception("ad claim poll failed")
        await asyncio.sleep(5)


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


def format_progress(stage: str, current: int, total: int, lang: str | None = None) -> str:
    label = t(lang, f"progress.{stage}") if f"progress.{stage}" in {
        "progress.metadata", "progress.frames", "progress.compose", "progress.prepare_upload",
        "progress.upload", "progress.pack", "progress.pack_media", "progress.remux_dl", "progress.remux",
    } else t(lang, "progress.working")
    ratio = min(1.0, max(0.0, current / total)) if total > 0 else 0.0
    width = 12
    filled = round(ratio * width)
    if 0 < ratio < 1:
        filled = min(width - 1, max(1, filled))
    bar = "▰" * filled + "▱" * (width - filled)
    percent = round(ratio * 100)
    if stage == "frames" and total > 0:
        detail = f"{current}/{total}"
    elif stage in {"upload", "pack", "pack_media", "remux_dl"} and total > 0:
        detail = f"{_fmt_mb(current)} / {_fmt_mb(total)}"
    else:
        detail = ""
    lines = [label, f"{bar}  {percent}%"]
    if detail:
        lines.append(detail)
    return "\n".join(lines)


def short_filename(name: str) -> str:
    name = (name or "video").strip() or "video"
    return name[:77] + "..." if len(name) > 80 else name


def format_queue_status(ahead: int, filename: str, lang: str | None = None) -> str:
    name = short_filename(filename)
    if ahead <= 0:
        return t(lang, "queue.turn")
    return t(lang, "queue.joined", ahead=ahead, name=name)


def format_queue_batch(waiting: int, lang: str | None = None) -> str:
    """One line for a whole chat, not one reply per file."""
    count = max(1, int(waiting))
    return t(lang, "queue.batch", count=count)


# Quiet period so a burst of files is counted before the one notice goes out.
QUEUE_BATCH_QUIET = 2.0


class QueueTicket:
    __slots__ = ("client", "chat_id", "filename", "status_id", "last_ahead")

    def __init__(self, client: TelegramClient, chat_id: int, filename: str) -> None:
        self.client = client
        self.chat_id = chat_id
        self.filename = filename
        self.status_id: int | None = None
        self.last_ahead: int | None = None


class JobQueue:
    """FIFO 任务队列。并发由 semaphore 限制，排队位置会写回 Telegram 消息。"""

    def __init__(self, concurrency: int) -> None:
        self.concurrency = max(1, concurrency)
        self._sema = asyncio.Semaphore(self.concurrency)
        self._lock = asyncio.Lock()
        self._waiters: list[QueueTicket] = []
        self._running = 0
        self._batches: dict[int, dict] = {}
        self.batch_quiet = QUEUE_BATCH_QUIET

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
        lang: str | None = None,
    ) -> QueueTicket:
        ticket = QueueTicket(client, chat_id, filename)
        async with self._lock:
            ahead = self._running + len(self._waiters)
            self._waiters.append(ticket)
            if ahead > 0:
                self._bump_batch_locked(chat_id, client, reply_to, quote_top_id, peer, lang)
        # Claim the slot before any Telegram await. The summary is sent later,
        # after a short quiet period, and must not affect who runs first.
        if ahead > 0:
            self._arm_batch(chat_id)
        try:
            await self._sema.acquire()
        except BaseException:
            async with self._lock:
                if ticket in self._waiters:
                    self._waiters.remove(ticket)
                    if ahead > 0:
                        self._drop_batch_locked(chat_id)
            if ahead > 0:
                self._arm_batch(chat_id)
            raise
        async with self._lock:
            if ticket in self._waiters:
                self._waiters.remove(ticket)
                if ahead > 0:
                    self._drop_batch_locked(chat_id)
            self._running += 1
        if ahead > 0:
            self._arm_batch(chat_id)
        return ticket

    def _bump_batch_locked(
        self,
        chat_id: int,
        client: TelegramClient,
        reply_to: int,
        quote_top_id: int | None,
        peer: Any,
        lang: str | None = None,
    ) -> None:
        state = self._batches.get(chat_id)
        if state is None:
            state = {
                "waiting": 0,
                "client": client,
                "reply_to": reply_to,
                "quote_top_id": quote_top_id,
                "peer": peer,
                "task": None,
                "message_id": None,
                "last_text": "",
                "lang": lang,
            }
            self._batches[chat_id] = state
        state["waiting"] += 1
        state["client"] = client
        if state["waiting"] == 1 and state.get("message_id") is None:
            state["reply_to"] = reply_to
            state["quote_top_id"] = quote_top_id
            state["peer"] = peer
            state["lang"] = lang

    def _drop_batch_locked(self, chat_id: int) -> None:
        state = self._batches.get(chat_id)
        if state is None:
            return
        state["waiting"] = max(0, int(state["waiting"]) - 1)

    def _arm_batch(self, chat_id: int, delay: float | None = None) -> None:
        state = self._batches.get(chat_id)
        if state is None:
            return
        current = asyncio.current_task()
        task = state.get("task")
        if task is not None and not task.done() and task is not current:
            task.cancel()
        state["task"] = asyncio.create_task(self._wait_batch(chat_id, self.batch_quiet if delay is None else delay))

    async def _wait_batch(self, chat_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        try:
            await self._flush_batch(chat_id)
        except Exception:
            logging.exception("queue summary failed")

    async def _flush_batch(self, chat_id: int) -> None:
        state = self._batches.get(chat_id)
        if state is None:
            return
        waiting = int(state["waiting"])
        client = state["client"]
        if waiting <= 0:
            message_id = state.get("message_id")
            self._batches.pop(chat_id, None)
            if message_id:
                await delete_own_message(client, chat_id, int(message_id))
            return
        text = format_queue_batch(waiting, state.get("lang"))
        if state.get("message_id") and text == state.get("last_text"):
            return
        if state.get("message_id"):
            if _flood.remaining(FLOOD_EDIT) > 0:
                return
            try:
                await client.edit_message(chat_id, state["message_id"], text)
            except FloodWaitError as exc:
                _flood.note_error(exc, FLOOD_EDIT)
                logging.warning("Queue summary edit was refused; not retrying")
                return
            except Exception as exc:
                logging.warning("Could not update queue summary (%s)", type(exc).__name__)
                return
            state["last_text"] = text
            return
        wait = _flood.remaining(FLOOD_MEDIA)
        if wait > 0:
            self._arm_batch(chat_id, wait + 1)
            return
        try:
            if state.get("peer") is not None:
                msg = await _send_text_quoting(
                    client, state["peer"], text, state["reply_to"], state.get("quote_top_id"),
                )
            else:
                msg = await client.send_message(chat_id, text, reply_to=state.get("reply_to"))
        except FloodWaitError as exc:
            _flood.note_error(exc, FLOOD_MEDIA)
            logging.warning("Queue summary was refused; not retrying")
            return
        except Exception as exc:
            logging.warning("Could not send queue summary (%s)", type(exc).__name__)
            return
        state["message_id"] = getattr(msg, "id", None)
        state["last_text"] = text

    async def leave(self) -> None:
        async with self._lock:
            self._running = max(0, self._running - 1)
        self._sema.release()


_flood: FloodGate = FloodGate()

# Seconds between two progress-bar edits. Each edit of the same message counts
# against Telegram's anti-flood, and bots get about 20 edits per minute per
# chat: the old 1.2 s loop (50/min) is what triggered the first flood.
PROGRESS_EDIT_INTERVAL = 4.0


def _is_flood_error(exc: BaseException) -> bool:
    return isinstance(exc, FloodWaitError)


async def _send_flood_notice(
    client: TelegramClient,
    message: Any,
    wait: int,
    is_admin: bool,
) -> None:
    """Tell this chat once when sends resume. A refused notice is not retried."""
    del is_admin
    chat_id = getattr(message, "chat_id", None)
    clear_at = time.time() + max(0, int(wait))
    if chat_id is not None and _flood_notice_until.get(int(chat_id), 0) >= clear_at - 60:
        logging.warning(
            "Declined a generation; notice already sent, still blocking for %s",
            describe_wait(wait),
        )
        return
    logging.warning(
        "Declined a generation; Telegram still blocking sends for %s",
        describe_wait(wait),
    )
    if chat_id is None:
        return
    text = (
        t(None, "flood.notice", when=resume_clock(wait))
    )
    try:
        await client.send_message(chat_id, text, reply_to=getattr(message, "id", None))
    except FloodWaitError as exc:
        _flood.note_error(exc, FLOOD_MEDIA)
        logging.warning("Flood notice was refused; not retrying")
        return
    except Exception:
        logging.exception("flood notice failed")
        return
    _flood_notice_until[int(chat_id)] = clear_at


class ProgressReporter:
    def __init__(self, client: TelegramClient, chat_id: int, message_id: int, lang: str | None = None) -> None:
        self.client = client
        self.chat_id = chat_id
        self.message_id = message_id
        self.lang = lang or DEFAULT_LANG
        self.loop = asyncio.get_running_loop()
        self.state: tuple[str, int, int] = ("metadata", 0, 1)
        self.last_text = ""
        self.dirty = True
        self.last_edit_at = 0.0
        self.task = asyncio.create_task(self._run())

    @classmethod
    async def create(
        cls,
        client: TelegramClient,
        chat_id: int,
        reply_to: int,
        existing_id: int | None = None,
        buttons=None,
        lang: str | None = None,
    ) -> "ProgressReporter":
        start_text = format_progress("metadata", 0, 1, lang)
        if existing_id is not None:
            reporter = cls(client, chat_id, existing_id, lang)
            await reporter._edit(start_text)
            return reporter
        # Reply keyboards make this message uneditable (MessageIdInvalidError),
        # so the progress bar never appears. Keep the keyboard off this message.
        message = await client.send_message(
            chat_id,
            start_text,
            reply_to=reply_to,
        )
        return cls(client, chat_id, message.id, lang)

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
        await self._flush(force=True)

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
            await delete_own_message(self.client, self.chat_id, self.message_id)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(1.2)
            await self._flush()

    async def _flush(self, force: bool = False) -> None:
        if not self.dirty:
            return
        # Every edit of the same message counts against Telegram's anti-flood.
        # A progress bar refreshed every 1.2 s for several minutes per job was
        # what pushed this account into an 800 s EditMessageRequest wait, so
        # refresh at most once per PROGRESS_EDIT_INTERVAL.
        if not force and time.monotonic() - self.last_edit_at < PROGRESS_EDIT_INTERVAL:
            return
        sent = await self._edit(format_progress(*self.state, self.lang))
        if sent:
            self.dirty = False

    async def _edit(self, text: str) -> bool:
        if text == self.last_text:
            return True
        # Retrying an edit while Telegram still enforces a FloodWait pushes the
        # deadline further out, so stay quiet until it expires.
        if _flood.remaining(FLOOD_EDIT) > 0:
            return False
        try:
            await self.client.edit_message(self.chat_id, self.message_id, text)
            self.last_text = text
            self.last_edit_at = time.monotonic()
            return True
        except FloodWaitError as exc:
            wait = _flood.note_error(exc, FLOOD_EDIT)
            logging.warning("Progress edits paused for %s", describe_wait(wait))
            return False
        except Exception as exc:
            logging.warning("Could not update progress message (%s: %s)", type(exc).__name__, exc)
            return False


def user_error_message(exc: Exception, lang: str | None = None) -> str:
    if isinstance(exc, FetchBudgetExceeded):
        fetched_mb = exc.fetched / 1024 / 1024
        limit_mb = exc.budget / 1024 / 1024
        return t(lang, "error.budget", fetched=f"{fetched_mb:.1f}", limit=f"{limit_mb:.1f}")
    if isinstance(exc, subprocess.TimeoutExpired):
        return t(lang, "error.timeout")
    if isinstance(exc, subprocess.CalledProcessError):
        err = (exc.stderr or "").strip()
        if not err and exc.stdout:
            err = (exc.stdout or "").strip()
        excerpt = err[:600].replace("\r\n", "\n")
        suffix = t(lang, "error.ffmpeg", excerpt=excerpt) if excerpt else t(lang, "error.no_ffmpeg")
        return t(lang, "error.unreadable", suffix=suffix)
    return t(lang, "error.failed")


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
    lang: str | None = None,
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
    text = t(lang, "reuse.prompt")
    if mention_html:
        text = f"{mention_html}\n{text}"
    await client.send_message(
        message.chat_id,
        text,
        buttons=[[
            Button.inline(t(lang, "reuse.yes"), f"reuse:y:{token}".encode()),
            Button.inline(t(lang, "reuse.no"), f"reuse:n:{token}".encode()),
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
    cleanup_msg_ids: list[int] | None = None,
) -> None:
    chat_id = message.chat_id
    message_id = message.id
    reply_id = quote_msg_id or message_id
    menu = main_menu(bool(access and access.is_admin(message.sender_id))) if _is_private_message(message) else None
    video = extract_video(message)
    if not video:
        await client.send_message(
            chat_id,
            "请发送视频文件，或把视频作为 document 发送给我。",
            reply_to=message_id,
            buttons=menu,
        )
        return

    filename = video.get("file_name") or "telegram-video.mp4"
    pref_id = preference_user_id if preference_user_id is not None else message.sender_id
    sheet_lang = preferences.get_lang(pref_id)
    peer = await _input_peer(client, message, chat_id) if quote_msg_id else None
    ticket = await queue.join(
        client, chat_id, reply_id, Path(filename).name,
        quote_top_id=quote_top_id, peer=peer, lang=sheet_lang,
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
                    format_progress("metadata", 0, 1, sheet_lang),
                    reply_id,
                    quote_top_id,
                )
                progress = ProgressReporter(client, chat_id, opening.id, sheet_lang)
            else:
                progress = await ProgressReporter.create(
                    client, chat_id, reply_id, existing_id=ticket.status_id, lang=sheet_lang,
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
                lang=sheet_lang,
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
                for extra_id in cleanup_msg_ids or []:
                    if extra_id and int(extra_id) != progress.message_id:
                        await delete_own_message(client, chat_id, int(extra_id))
            else:
                await progress.finish(t(sheet_lang, "done.sheet"))
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
                        lang=sheet_lang,
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
            flood_wait = _flood.note_error(exc, FLOOD_MEDIA) if _is_flood_error(exc) else 0
            if flood_wait:
                # Not a bug in the job: Telegram asked us to stop sending.
                logging.warning(
                    "Telegram rate-limited sends for message %s; sends paused for %s",
                    message_id,
                    describe_wait(flood_wait),
                )
            else:
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
                if flood_wait:
                    error_text = (
                        t(sheet_lang, "error.flood")
                        + f"{describe_wait(flood_wait, sheet_lang)}。"
                    )
                    # Nothing was delivered, so do not eat the caller's slot.
                    payer = preference_user_id if preference_user_id is not None else message.sender_id
                    if (
                        task_log is not None
                        and payer is not None
                        and access is not None
                        and not access.has_grant(payer)
                    ):
                        restored = task_log.refund(payer)
                        if restored == "sponsor" and _star_ledger is not None:
                            _star_ledger.note_unspend(payer, 1)
                else:
                    error_text = t(sheet_lang, "error.prefix") + user_error_message(exc, sheet_lang)
                if progress:
                    await progress.finish(error_text)
                elif quote_msg_id and peer is not None:
                    await _send_text_quoting(client, peer, error_text, reply_id, quote_top_id)
                else:
                    await client.send_message(chat_id, error_text, reply_to=message_id)
                if quote_msg_id:
                    for extra_id in cleanup_msg_ids or []:
                        if extra_id and (progress is None or int(extra_id) != progress.message_id):
                            await delete_own_message(client, chat_id, int(extra_id))
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
    global _bot_token
    _bot_token = settings.bot_token
    access = AccessControl(Path(settings.access_file), settings.admin_ids, open_access=settings.open_access)
    preferences = UserPreferences(Path(settings.preferences_file))
    global _preferences
    _preferences = preferences
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
        flood_sleep_threshold=0,
    )
    _install_fast_upload(mt_client)
    media_server = MTProtoRangeServer(
        mt_client,
        cache_bytes=settings.range_cache_mb * 1024 * 1024,
    )
    queue = JobQueue(settings.max_concurrent_jobs)
    forward = ForwardGate(Path(settings.access_file).with_name("forward.json"))
    guest_gate = GuestGate(Path(settings.access_file).with_name("guest.json"))
    global _stars, _star_ledger
    _stars = StarsGate(Path(settings.access_file).with_name("stars_gate.json"))
    _star_ledger = StarsLedger(Path(settings.access_file).with_name("stars_payments.json"))
    task_log = TaskLog(
        Path(settings.access_file).with_name("task_log.json"),
        daily_limit=settings.guest_daily_limit,
    )
    global _audience
    _audience = Audience(Path(settings.access_file).with_name("audience.json"))
    known = set(_audience.ids())
    known.update(task_log.known_user_ids())
    known.update(preferences.known_user_ids())
    await preferences.adopt_existing(known)
    global _flood
    _flood = FloodGate(Path(settings.access_file).with_name("flood.json"))
    global _forward_store, _announce_ledger, _spool_root
    data_dir = Path(settings.access_file).parent
    _spool_root = data_dir / "admin_spool"
    _spool_root.mkdir(parents=True, exist_ok=True)
    _forward_store = ForwardStore(data_dir / "forward_outbox.json")
    _announce_ledger = AnnounceLedger(data_dir / "announce_ledger.json")
    for pending_user, pending_state in _forward_store.load().items():
        _pending_forward[pending_user] = pending_state
        _arm_forward_retry(mt_client, access, pending_user)
    flood_left = _flood.remaining(FLOOD_MEDIA)
    if flood_left:
        logging.warning(
            "Telegram is still rate-limiting sends: %s left. New jobs will be declined until then.",
            describe_wait(flood_left),
        )
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
        cleanup_msg_ids: list[int] | None = None,
    ) -> None:
        user_id = preference_user_id if preference_user_id is not None else message.sender_id
        wait = _flood.remaining(FLOOD_MEDIA)
        if wait > 0:
            # Do not consume a slot or spend two minutes reading the video when
            # the finished sheet cannot be sent anyway.
            is_admin = bool(access is not None and user_id is not None and access.is_admin(user_id))
            asyncio.create_task(_send_flood_notice(mt_client, message, wait, is_admin))
            return
        if user_id is not None and not access.has_grant(user_id):
            task_log.consume(user_id)
            spent = (task_log._get(user_id).get("spend") or [None])[-1]
            if spent == "sponsor" and _star_ledger is not None:
                _star_ledger.note_spend(user_id, 1)
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
                cleanup_msg_ids=cleanup_msg_ids,
            )
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @mt_client.on(events.NewMessage(incoming=True))
    async def on_message(event: events.NewMessage.Event) -> None:
        if isinstance(getattr(event.message, "action", None), types.MessageActionPaymentSentMe):
            await _credit_star_payment(mt_client, event.message, task_log)
            return
        sender = private_sender_id(bool(event.is_private), event.sender_id)
        if sender and _audience is not None:
            _audience.note(sender)
        action = private_menu_action(event.message)
        if action:
            _bonus_wait.discard(event.sender_id)
            if not action.startswith("/announce"):
                _announce_wait.discard(event.sender_id)
            if await handle_command(
                mt_client, settings, access, preferences, _AsCommand(event.message, action),
                queue=queue, forward=forward, task_log=task_log,
                groups=groups, enqueue=_start_generation, guest_gate=guest_gate,
            ):
                return
        if await handle_command(
            mt_client, settings, access, preferences, event.message,
            queue=queue, forward=forward, task_log=task_log,
            groups=groups, enqueue=_start_generation, guest_gate=guest_gate,
        ):
            return
        if event.is_group:
            return
        if event.is_private and event.sender_id in _bonus_wait and access.is_admin(event.sender_id):
            if getattr(event.message, "media", None) or extract_video(event.message):
                _bonus_wait.discard(event.sender_id)
            else:
                amount = plain_bonus_amount(event.raw_text)
                if amount is None:
                    await mt_client.send_message(
                        event.chat_id,
                        f"请发送 1 到 {BONUS_MAX} 的整数，或点其他按钮取消。",
                        reply_to=event.message.id,
                        buttons=main_menu(True),
                    )
                    return
                _bonus_wait.discard(event.sender_id)
                targets = ungranted_user_ids(access, task_log, preferences)
                count = task_log.add_stored_many(targets, amount)
                lang = preferences.get_lang(event.sender_id)
                await mt_client.send_message(
                    event.chat_id,
                    t(lang, "bonus.done", count=count, amount=amount),
                    reply_to=event.message.id,
                    buttons=main_menu(True, lang),
                )
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
                    buttons=main_menu(False) if event.is_private else None,
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
        if guests_closed(access, guest_gate, event.sender_id):
            await mt_client.send_message(
                event.chat_id,
                GUEST_CLOSED_TEXT,
                reply_to=event.message.id,
                buttons=main_menu(False) if event.is_private else None,
            )
            return
        record = task_log.lookup(key, content_key(event.message))
        if record:
            await _offer_previous_result(
                mt_client, event.message, record, key or "",
                lang=preferences.get_lang(event.sender_id),
            )
            return
        if not access.has_grant(event.sender_id) and not task_log.can_generate(event.sender_id):
            await mt_client.send_message(
                event.chat_id,
                quota_message(settings.guest_daily_limit, settings.community_url, settings.ad_reward, settings.ads_live and SHOW_AD_BUTTON),
                reply_to=event.message.id,
                buttons=main_menu(access.is_admin(event.sender_id)) if event.is_private else None,
            )
            if stars_open() and event.is_private and not access.has_grant(event.sender_id):
                await mt_client.send_message(
                    event.chat_id,
                    t(preferences.get_lang(event.sender_id), "sponsor.pick"),
                    buttons=sponsor_pack_buttons(preferences.get_lang(event.sender_id)),
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
        if guests_closed(access, guest_gate, event.sender_id):
            await event.answer("非授权用户暂时不能使用", alert=True)
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
                done = t(user_lang(event.sender_id), "reuse.sent_group")
            else:
                ok = await _forward_saved_result(mt_client, message.chat_id, offer["record"])
                done = t(user_lang(event.sender_id), "reuse.sent_private")
            await event.answer("已发送" if ok else "发送失败")
            if ok and offer.get("group"):
                await delete_own_message(mt_client, event.chat_id, event.message_id)
            else:
                with contextlib.suppress(Exception):
                    await event.edit(
                        done if ok else "之前的记录已失效，无法发送。可以再发一次并选择重新生成。",
                        buttons=None,
                    )
            return
        if not access.has_grant(requester) and not task_log.can_generate(requester):
            await event.answer("今天的次数已用完，可先签到" + (("或看广告" if settings.ads_live else "") if SHOW_AD_BUTTON else ""), alert=True)
            with contextlib.suppress(Exception):
                await event.edit(
                    quota_message(settings.guest_daily_limit, settings.community_url, settings.ad_reward, settings.ads_live and SHOW_AD_BUTTON),
                    buttons=sponsor_pack_buttons(user_lang(requester)) if stars_open() and not access.has_grant(requester) else None,
                )
            return
        _drop_reuse_offer(token)
        await event.answer("开始重新生成")
        note = "好的，重新生成。" if access.has_grant(requester) else "好的，重新生成。这次会计入次数。"
        with contextlib.suppress(Exception):
            await event.edit(note, buttons=None)
        kwargs = dict(offer.get("enqueue_kwargs") or {})
        extra = [int(item) for item in (kwargs.get("cleanup_msg_ids") or []) if item]
        if event.message_id:
            extra.append(int(event.message_id))
        kwargs["cleanup_msg_ids"] = extra
        _start_generation(message, **kwargs)

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
            and user_id not in (_announce_ledger.already_sent(text) if _announce_ledger is not None else set())
        ]
        await event.answer("开始发布")
        with contextlib.suppress(Exception):
            await event.edit(f"正在发布公告… 共 {len(recipients)} 人", buttons=None)
        result = await _publish_announce(mt_client, text, recipients, _audience)
        if result.wait:
            if result.sent:
                summary = (
                    f"公告只发出一部分。成功 {result.sent} 人，失败 {result.failed} 人，"
                    f"还有 {result.unsent} 人没发。\n"
                    f"账号还在被 Telegram 限制，预计还要等 {describe_wait(result.wait)}，"
                    "之后再发一次即可。"
                )
            else:
                summary = (
                    "公告没有发出。账号还在被 Telegram 限制，"
                    f"预计还要等 {describe_wait(result.wait)}。"
                )
        else:
            summary = f"公告已发布。成功 {result.sent} 人，失败 {result.failed} 人。"
        with contextlib.suppress(Exception):
            await event.edit(summary)

    @mt_client.on(events.CallbackQuery(pattern=b"^pack:"))
    async def on_pack_offer(event: events.CallbackQuery.Event) -> None:
        await _handle_pack_callback(event)

    @mt_client.on(events.CallbackQuery(pattern=b"^pick:"))
    async def on_pick_lang(event: events.CallbackQuery.Event) -> None:
        user_id = event.sender_id
        chosen = lang_from_callback(event.data)
        if user_id is None or chosen is None:
            await event.answer(t(DEFAULT_LANG, "setting.bad"), alert=True)
            return
        await preferences.set_lang(user_id, chosen)
        is_admin = access.is_admin(user_id)
        await sync_user_commands(mt_client, user_id, chosen, is_admin=is_admin, ads_enabled=SHOW_AD_BUTTON)
        welcome = t(chosen, "start.welcome") if access.is_allowed(user_id) else permission_message(user_id, chosen)
        await event.answer(t(chosen, "pick.saved"))
        with contextlib.suppress(Exception):
            await event.edit(t(chosen, "pick.saved"), buttons=None)
        await mt_client.send_message(
            event.chat_id,
            welcome,
            buttons=main_menu(is_admin, chosen),
        )

    @mt_client.on(events.CallbackQuery(pattern=b"^(setting|delivery|theme|fwdgate|lang|stargate):"))
    async def on_setting(event: events.CallbackQuery.Event) -> None:
        user_id = event.sender_id
        lang = preferences.get_lang(user_id)
        if not access.is_allowed(user_id):
            await event.answer(t(lang, "perm.none"), alert=True)
            return
        if user_id is None:
            await event.answer(t(lang, "setting.bad"), alert=True)
            return
        changed = False
        preset = preferences.get(user_id)
        delivery = preferences.get_delivery(user_id)
        theme = preferences.get_theme(user_id)
        admin_forward = forward.enabled if access.is_admin(user_id) and forward is not None else None
        keyboard_lang = None
        if event.data.startswith(b"fwdgate:"):
            if not access.is_admin(user_id) or forward is None:
                await event.answer(t(lang, "setting.no_right"), alert=True)
                return
            await forward.set_enabled(not forward.enabled)
            admin_forward = forward.enabled
            await event.answer(t(lang, "fwdgate.opened") if forward.enabled else t(lang, "fwdgate.closed"))
            rows = setting_buttons(preset, delivery, theme, admin_forward=admin_forward, stars_enabled=_stars.enabled if access.is_admin(user_id) and _stars is not None else None, lang=lang)
            text = setting_text(preset, delivery, theme, lang)
            if not await show_colored_settings(event.chat_id, text, rows, message_id=event.message_id):
                await event.edit(text, buttons=rows)
                await paint_inline_styles(event.chat_id, event.message_id, rows)
            return
        if event.data.startswith(b"stargate:"):
            if not access.is_admin(user_id) or _stars is None:
                await event.answer(t(lang, "setting.no_right"), alert=True)
                return
            await _stars.set_enabled(not _stars.enabled)
            await event.answer(t(lang, "stargate.opened") if _stars.enabled else t(lang, "stargate.closed"))
            rows = setting_buttons(
                preset,
                delivery,
                theme,
                admin_forward=admin_forward,
                stars_enabled=_stars.enabled,
                lang=lang,
            )
            text = setting_text(preset, delivery, theme, lang)
            if not await show_colored_settings(event.chat_id, text, rows, message_id=event.message_id):
                await event.edit(text, buttons=rows)
                await paint_inline_styles(event.chat_id, event.message_id, rows)
            return
        if event.data.startswith(b"lang:"):
            selected_lang = lang_from_callback(event.data)
            if selected_lang is None:
                await event.answer(t(lang, "setting.bad"), alert=True)
                return
            changed = selected_lang != lang
            lang = await preferences.set_lang(user_id, selected_lang)
            keyboard_lang = lang if changed else None
        elif event.data.startswith(b"setting:"):
            selected = setting_from_callback(event.data)
            if selected is None:
                await event.answer(t(lang, "setting.bad"), alert=True)
                return
            changed = selected != preset
            preset = await preferences.set(user_id, selected.count, selected.cols)
        elif event.data.startswith(b"theme:"):
            selected_theme = theme_from_callback(event.data)
            if selected_theme is None:
                await event.answer(t(lang, "setting.bad_theme"), alert=True)
                return
            changed = selected_theme != theme
            theme = await preferences.set_theme(user_id, selected_theme)
        else:
            selected_delivery = delivery_from_callback(event.data)
            if selected_delivery is None:
                await event.answer(t(lang, "setting.bad_delivery"), alert=True)
                return
            changed = selected_delivery != delivery
            delivery = await preferences.set_delivery(user_id, selected_delivery)
        await event.answer(t(lang, "setting.saved") if changed else t(lang, "setting.same"))
        if changed:
            rows = setting_buttons(preset, delivery, theme, admin_forward=admin_forward, stars_enabled=_stars.enabled if access.is_admin(user_id) and _stars is not None else None, lang=lang)
            text = setting_text(preset, delivery, theme, lang)
            if not await show_colored_settings(event.chat_id, text, rows, message_id=event.message_id):
                await event.edit(text, buttons=rows)
                await paint_inline_styles(event.chat_id, event.message_id, rows)
        if keyboard_lang:
            await sync_user_commands(
                mt_client, user_id, keyboard_lang, is_admin=access.is_admin(user_id), ads_enabled=SHOW_AD_BUTTON,
            )
            await mt_client.send_message(
                event.chat_id,
                t(keyboard_lang, "lang.switched"),
                buttons=main_menu(access.is_admin(user_id), keyboard_lang),
            )

    @mt_client.on(events.CallbackQuery(pattern=b"^star:"))
    async def on_star_buy(event: events.CallbackQuery.Event) -> None:
        lang = preferences.get_lang(event.sender_id)
        raw = (event.data or b"").decode("ascii", "replace")
        pack_id = raw.split(":", 1)[1] if ":" in raw else ""
        if not event.is_private:
            await event.answer(t(lang, "sponsor.private"), alert=True)
            return
        if pack_id not in PACKS:
            await event.answer(t(lang, "sponsor.off"), alert=True)
            return
        if not stars_open():
            await event.answer(t(lang, "sponsor.off"), alert=True)
            return
        if access.has_grant(event.sender_id) or access.is_admin(event.sender_id):
            await event.answer(t(lang, "sponsor.unlimited"), alert=True)
            return
        if guests_closed(access, guest_gate, event.sender_id):
            await event.answer(t(lang, "sponsor.closed_guest"), alert=True)
            return
        await event.answer()
        pack = PACKS[pack_id]
        title = t(lang, "sponsor.pack", stars=pack["stars"], credits=pack["credits"])
        try:
            await send_pack_invoice(mt_client, event.sender_id, pack_id, title, t(lang, "sponsor.pick"))
        except Exception as exc:
            logging.warning("star invoice failed (%s)", type(exc).__name__)
            await mt_client.send_message(event.chat_id, t(lang, "sponsor.refund_fail"))

    @mt_client.on(events.CallbackQuery(pattern=b"^sref:"))
    async def on_star_refund(event: events.CallbackQuery.Event) -> None:
        lang = user_lang(event.sender_id)
        token = (event.data or b"").decode("ascii", "replace").split(":", 1)[-1]
        found = _star_ledger.by_token(token) if _star_ledger is not None else None
        if found is None or int(found[1].get("user_id") or 0) != int(event.sender_id or 0):
            await event.answer(t(lang, "pack.not_yours"), alert=True)
            return
        charge_id, item = found
        if not _star_ledger.can_refund(charge_id, event.sender_id):
            await event.answer(t(lang, "sponsor.used"), alert=True)
            return
        try:
            await refund_charge(mt_client, event.sender_id, charge_id)
        except Exception as exc:
            logging.warning("star refund failed (%s)", type(exc).__name__)
            await event.answer(t(lang, "sponsor.refund_fail"), alert=True)
            return
        marked = _star_ledger.mark_refunded(charge_id)
        if marked is not None:
            task_log.remove_sponsor(event.sender_id, int(marked.get("credits") or 0))
        await event.answer(t(lang, "sponsor.refunded", stars=item.get("stars"), credits=item.get("credits")))
        with contextlib.suppress(Exception):
            await event.edit(t(lang, "sponsor.refunded", stars=item.get("stars"), credits=item.get("credits")), buttons=None)

    @mt_client.on(events.Raw)
    async def on_star_precheckout(update) -> None:
        if not isinstance(update, types.UpdateBotPrecheckoutQuery):
            return
        error = precheck_error(
            update.payload,
            update.currency,
            update.total_amount,
            enabled=stars_open(),
        )
        await mt_client(
            functions.messages.SetBotPrecheckoutResultsRequest(
                query_id=update.query_id,
                success=error is None,
                error=error,
            )
        )

    @mt_client.on(events.NewMessage)
    async def on_star_paid(event: events.NewMessage.Event) -> None:
        if isinstance(getattr(event.message, "action", None), types.MessageActionPaymentSentMe):
            await _credit_star_payment(mt_client, event.message, task_log)

    claim_task: asyncio.Task[None] | None = None
    try:
        await mt_client.start(bot_token=settings.bot_token)
        await set_bot_commands(mt_client, settings.admin_ids, SHOW_AD_BUTTON)
        global _ads_enabled
        _ads_enabled = SHOW_AD_BUTTON
        claim_task = asyncio.create_task(_claim_loop(mt_client, settings, task_log, access))
        tasks.add(claim_task)
        await media_server.start()
        logging.info(
            "vthumb Telegram bot started with MTProto range reading. upload_part_size_kb=%s tcp_keepalive=on",
            UPLOAD_PART_SIZE_KB,
        )
        await mt_client.run_until_disconnected()
    finally:
        if claim_task is not None:
            claim_task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await media_server.stop()
        await mt_client.disconnect()


if __name__ == "__main__":
    asyncio.run(poll())
