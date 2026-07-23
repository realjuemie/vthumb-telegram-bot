import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: str
    video_codec: str
    audio_codec: str


@dataclass(frozen=True)
class SourceInfo:
    url: str
    filename: str
    size: int | None


def format_time(seconds: float) -> str:
    if seconds < 0 or math.isnan(seconds):
        seconds = 0
    total = int(math.floor(seconds))
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02}:{minutes:02}:{secs:02}"


def format_size(size: int | None) -> str:
    if size is None:
        return "UNKNOWN"
    mb = size / 1024 / 1024
    return f"{mb:,.2f}MB({size:,} bytes)"


def sample_time(duration: float, index: int, count: int) -> float:
    default_time = duration * index / (count + 1)
    if index != 1:
        return default_time
    early_time = max(duration * 0.02, min(3.0, duration * 0.1))
    return min(default_time, early_time)


def run_process(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def get_media_info(url: str, timeout: int) -> MediaInfo:
    result = run_process(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            url,
        ],
        timeout,
    )
    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video:
        raise RuntimeError("No video stream found.")

    rotation = _read_rotation(video)
    display_width = int(video.get("width") or 0)
    display_height = int(video.get("height") or 0)
    if rotation in (90, 270):
        display_width, display_height = display_height, display_width

    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise RuntimeError("Cannot determine video duration.")

    return MediaInfo(
        duration=duration,
        width=display_width,
        height=display_height,
        fps=_format_fps(video.get("avg_frame_rate")),
        video_codec=str(video.get("codec_name") or "UNKNOWN").upper(),
        audio_codec=str((audio or {}).get("codec_name") or "NONE").upper(),
    )


def create_contact_sheet(
    source: SourceInfo,
    output_file: Path,
    *,
    count: int,
    cols: int,
    width: int,
    timeout: int,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> Path:
    if progress_callback:
        progress_callback("metadata", 0, 1)
    meta = get_media_info(source.url, timeout)
    if progress_callback:
        progress_callback("metadata", 1, 1)
    tmp_dir = Path(tempfile.mkdtemp(prefix="vthumb_"))
    try:
        frames: list[Path] = []
        times: list[float] = []
        for index in range(1, count + 1):
            t = sample_time(meta.duration, index, count)
            frame = tmp_dir / f"frame_{index:03}.jpg"
            run_process(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{t:.3f}",
                    "-i",
                    source.url,
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=640:-2",
                    "-q:v",
                    "3",
                    "-y",
                    str(frame),
                ],
                timeout,
            )
            if frame.exists():
                frames.append(frame)
                times.append(t)
            if progress_callback:
                progress_callback("frames", index, count)

        if not frames:
            raise RuntimeError("FFmpeg did not produce any frames.")

        if progress_callback:
            progress_callback("compose", 0, 1)
        compose_contact_sheet(source, output_file, frames, times, meta, width, cols)
        if progress_callback:
            progress_callback("compose", 1, 1)
        return output_file
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def compose_contact_sheet(
    source: SourceInfo,
    output_file: Path,
    frames: list[Path],
    times: list[float],
    meta: MediaInfo,
    sheet_width: int,
    cols: int,
) -> None:
    margin = 10
    gap = 8
    header_font_size = max(13, round(sheet_width / 70))
    stamp_font_size = max(12, round(sheet_width / 72))
    line_height = math.ceil(header_font_size * 1.35)
    max_header_chars = max(20, math.floor(sheet_width / (header_font_size * 1.1)))
    tile_w = math.floor((sheet_width - margin * 2 - gap * (cols - 1)) / cols)
    aspect_ratio = meta.width / meta.height if meta.width > 0 and meta.height > 0 else 16 / 9
    tile_h = round(tile_w / aspect_ratio)
    rows = math.ceil(len(frames) / cols)

    header_font = load_font(header_font_size, bold=True)
    stamp_font = load_font(stamp_font_size, bold=True)
    lines = build_header_lines(source, meta, max_header_chars)
    header_height = int(line_height * len(lines) + 18)
    sheet_height = header_height + margin + rows * tile_h + gap * (rows - 1) + margin

    sheet = Image.new("RGB", (sheet_width, sheet_height), "white")
    draw = ImageDraw.Draw(sheet)
    for index, line in enumerate(lines):
        draw_outlined_text(draw, (12, 8 + index * line_height), line, header_font, "white", 2)

    for index, frame in enumerate(frames):
        row = index // cols
        col = index % cols
        x = margin + col * (tile_w + gap)
        y = header_height + margin + row * (tile_h + gap)
        with Image.open(frame) as img:
            img = img.convert("RGB").resize((tile_w, tile_h), Image.Resampling.LANCZOS)
            draw.rectangle((x, y, x + tile_w, y + tile_h), fill="black")
            sheet.paste(img, (x, y))
        draw.rectangle((x, y, x + tile_w - 1, y + tile_h - 1), outline=(60, 60, 60), width=1)

        stamp = format_time(times[index])
        left, top, right, bottom = draw.textbbox((0, 0), stamp, font=stamp_font, stroke_width=2)
        stamp_w = right - left
        stamp_h = bottom - top
        pad_x = 9
        pad_y = 5
        box_w = stamp_w + pad_x * 2
        box_h = stamp_h + pad_y * 2
        box_left = x + (tile_w - box_w) / 2
        box_top = y + tile_h - 4 - box_h
        box_right = box_left + box_w
        box_bottom = box_top + box_h
        tx = box_left + pad_x - left
        ty = box_top + pad_y - top
        fill_translucent_rect(
            sheet,
            (box_left, box_top, box_right, box_bottom),
            alpha=150,
        )
        draw_outlined_text(draw, (tx, ty), stamp, stamp_font, "white", 2)

    sheet.save(output_file, "PNG")


def build_header_lines(source: SourceInfo, meta: MediaInfo, max_chars: int) -> list[str]:
    prefix = "文件名: "
    name = source.filename
    file_lines = []
    while len(name) > max_chars:
        file_lines.append(prefix + name[:max_chars])
        prefix = ""
        name = name[max_chars:]
    file_lines.append(prefix + name)

    return file_lines + [
        f"大小: {format_size(source.size)}",
        f"分辨率: {meta.width}x{meta.height}({meta.fps} fps)",
        f"视频解码器: {meta.video_codec} 音频解码器: {meta.audio_codec}",
        f"时长: {format_time(meta.duration)}",
    ]


def draw_outlined_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: str,
    stroke_width: int,
) -> None:
    draw.text(
        xy,
        text,
        font=font,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill=(0, 0, 0),
    )


def fill_translucent_rect(
    image: Image.Image,
    box: tuple[float, float, float, float],
    *,
    alpha: int,
) -> None:
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rectangle(box, fill=(0, 0, 0, alpha))
    image.paste(Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB"))


def load_font(size: int, *, bold: bool) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/msyh.ttc",
    ]
    if not bold:
        candidates = [p.replace("Bold", "Regular").replace("msyhbd", "msyh") for p in candidates]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def _read_rotation(video: dict) -> int:
    rotation = 0
    for side_data in video.get("side_data_list") or []:
        if "rotation" in side_data:
            rotation = int(side_data["rotation"])
            break
    tags = video.get("tags") or {}
    if rotation == 0 and tags.get("rotate"):
        rotation = int(tags["rotate"])
    return rotation % 360


def _format_fps(value: str | None) -> str:
    if not value or "/" not in value:
        return ""
    num, den = value.split("/", 1)
    try:
        fps = float(num) / float(den)
    except (ValueError, ZeroDivisionError):
        return ""
    return f"{fps:.2f}".rstrip("0").rstrip(".")
