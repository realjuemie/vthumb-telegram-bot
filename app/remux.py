from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

ALLOWED_VIDEO_CODECS = frozenset({"h264"})
ALLOWED_AUDIO_CODECS = frozenset({"aac"})


def evaluate_copy_remux(streams: list[dict[str, Any]]) -> tuple[bool, str]:
    """Return whether streams can be remuxed into a Telegram-playable MP4 without re-encoding."""
    videos = [item for item in streams if str(item.get("codec_type") or "") == "video"]
    audios = [item for item in streams if str(item.get("codec_type") or "") == "audio"]
    if not videos:
        return False, "没有视频轨，无法转成可播放消息。"
    video_codec = str(videos[0].get("codec_name") or "").strip().lower()
    if video_codec not in ALLOWED_VIDEO_CODECS:
        label = video_codec or "未知"
        return False, f"视频编码是 {label}，需要 H.264 才能无损封装。不会重新压缩。"
    if audios:
        audio_codec = str(audios[0].get("codec_name") or "").strip().lower()
        if audio_codec not in ALLOWED_AUDIO_CODECS:
            return False, f"音频编码是 {audio_codec}，需要 AAC 才能无损封装。不会重新压缩。"
    return True, "ok"


def remux_copy_args(src: Path, dst: Path) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(dst),
    ]


def probe_streams(path: Path, timeout: int = 30) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "ffprobe failed").strip()
        raise RuntimeError(err[:400] or "ffprobe failed")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise RuntimeError("ffprobe 返回了无法识别的信息") from error
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        return []
    return [item for item in streams if isinstance(item, dict)]


def remux_copy_mp4(src: Path, dst: Path, timeout: int = 90) -> None:
    result = subprocess.run(
        remux_copy_args(src, dst),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0 or not dst.is_file() or dst.stat().st_size <= 0:
        err = (result.stderr or result.stdout or "ffmpeg remux failed").strip()
        raise RuntimeError(err[:400] or "ffmpeg remux failed")
