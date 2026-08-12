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


def format_size_mb(size: int | None) -> str:
    """Compact `668.76MB` form for the minimal-theme footer band."""
    if size is None or size <= 0:
        return "?MB"
    mb = size / 1024 / 1024
    return f"{mb:,.2f}MB"


def format_duration_short(seconds: float | None) -> str:
    """`00:30:12` form for the minimal-theme footer band.

    Returns `?:??:??` when seconds is missing or non-finite.
    """
    if seconds is None or seconds != seconds:  # NaN check
        return "?:??:??"
    seconds = max(seconds, 0)
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def format_fps_short(fps: str | None | float) -> str:
    """`60FPS` form. Accepts the `meta.fps` field which is stored as a string
    (e.g. `\"24.000\"`) in the current codebase; tolerant to None / floats."""
    if fps is None or fps == "":
        return "?FPS"
    try:
        v = float(fps)
    except (TypeError, ValueError):
        s = str(fps).strip()
        return f"{s}FPS" if s else "?FPS"
    if abs(v - round(v)) < 1e-3:
        return f"{int(round(v))}FPS"
    return f"{v:.2f}FPS"


def _format_meta_str(source, meta) -> str:
    """Bottom-right band text: `1920x1080 | 00:30:12 | 60FPS | 668.76MB`."""
    if meta.width and meta.height:
        resolution = f"{meta.width}x{meta.height}"
    else:
        resolution = "?x?"
    duration = format_duration_short(meta.duration)
    fps = format_fps_short(meta.fps)
    size_mb = format_size_mb(getattr(source, "size", None))
    return f"{resolution} | {duration} | {fps} | {size_mb}"


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


# Visual style selectors. Each preset bundles the sheet background, header text
# treatment, ringfence visibility, and overlay translucency for the timestamp.
# Adding a new theme means appending one entry here + one entry in
# user_preferences.THEMES / THEME_LABELS -- nothing else changes.
@dataclass(frozen=True)
class Theme:
    name: str
    sheet_bg: tuple[int, int, int]
    text_fill: tuple[int, int, int]
    text_stroke: int | None  # pixel width of black halo around text; None = no halo
    stroke_fill: tuple[int, int, int]
    tile_ringfence: bool
    draw_header: bool  # False = skip header block entirely (pure_image)
    # Minimal-mode fields (default keeps all legacy themes behaving exactly as before):
    header_mode: str = "rich"  # "rich" | "minimal"; "rich" = the original multi-line
                                # metadata block, "minimal" = filename-only single line.
    footer_mode: str = "none"  # "none" | "minimal"; "none" = no footer band, "minimal"
                                # = right-aligned single line on a slim bottom band.
    header_band_height: int = 0  # px height of top info band; 0 = no top band
    footer_band_height: int = 0  # px height of bottom info band; 0 = no bottom band
    header_band_bg: tuple[int, int, int] | None = None
                                # background fill of the top band (None = "fill == sheet_bg")
    footer_band_bg: tuple[int, int, int] | None = None
                                # background fill of the bottom band (None = "fill == sheet_bg")


THEME_POTPLAYER = Theme(
    name="potplayer",
    sheet_bg=(255, 255, 255),
    text_fill=(255, 255, 255),
    text_stroke=2,
    stroke_fill=(0, 0, 0),
    tile_ringfence=True,
    draw_header=True,
)
THEME_BLACK_BG = Theme(
    name="black_bg",
    sheet_bg=(0, 0, 0),
    text_fill=(255, 255, 255),
    text_stroke=None,  # white on black needs no black halo
    stroke_fill=(0, 0, 0),
    tile_ringfence=False,  # ringfence would be invisible against black anyway
    draw_header=True,
)
THEME_WHITE_BG = Theme(
    name="white_bg",
    sheet_bg=(255, 255, 255),
    text_fill=(0, 0, 0),
    text_stroke=None,
    stroke_fill=(0, 0, 0),
    tile_ringfence=False,
    draw_header=True,
)
THEME_PURE_IMAGE = Theme(
    name="pure_image",
    sheet_bg=(255, 255, 255),
    text_fill=(0, 0, 0),
    text_stroke=0,
    stroke_fill=(0, 0, 0),
    tile_ringfence=False,
    draw_header=False,  # no header block; only the N×N frame grid + timestamps
)
# Minimal themes: top + bottom bands whose fill matches the sheet color
# exactly so the user sees a single uniform black or white surface (band
# borders are invisible, but the band still reserves its vertical space
# and hosts its text glyphs). Earlier versions used slightly off shades
# (e.g. (28,28,28) on a (0,0,0) sheet) so the band was visible -- that made
# the band look like a gray bar against the black background, which the
# user reported as visually inconsistent with the rest of the sheet.
THEME_MINIMAL_BLACK = Theme(
    name="minimal_black",
    sheet_bg=(0, 0, 0),
    text_fill=(255, 255, 255),
    text_stroke=None,
    stroke_fill=(0, 0, 0),
    tile_ringfence=False,
    draw_header=True,
    header_mode="minimal",
    footer_mode="minimal",
    header_band_height=56,
    footer_band_height=44,
    header_band_bg=(0, 0, 0),
    footer_band_bg=(0, 0, 0),
)
THEME_MINIMAL_WHITE = Theme(
    name="minimal_white",
    sheet_bg=(255, 255, 255),
    text_fill=(0, 0, 0),
    text_stroke=None,
    stroke_fill=(0, 0, 0),
    tile_ringfence=False,
    draw_header=True,
    header_mode="minimal",
    footer_mode="minimal",
    header_band_height=56,
    footer_band_height=44,
    header_band_bg=(255, 255, 255),
    footer_band_bg=(255, 255, 255),
)
THEME_BY_NAME: dict[str, Theme] = {
    t.name: t
    for t in (
        THEME_POTPLAYER,
        THEME_BLACK_BG,
        THEME_WHITE_BG,
        THEME_PURE_IMAGE,
        THEME_MINIMAL_BLACK,
        THEME_MINIMAL_WHITE,
    )
}


def create_contact_sheet(
    source: SourceInfo,
    output_file: Path,
    *,
    count: int,
    cols: int,
    width: int,
    timeout: int,
    theme: Theme = THEME_POTPLAYER,
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
                    "scale=1280:-2",
                    "-q:v",
                    "2",
                    # Relax ffmpeg's strict-spec check so non-standard inputs that
                    # flag "Non full-range YUV is non-standard" (commonly seen on
                    # mjpeg/mjpeg-b frames inside MKV/MOV) still encode instead of
                    # throwing -22 (Invalid argument).
                    # ffmpeg 7.x emits a hint saying `set strict_std_compliance to
                    # at most unofficial`, but that exact option name is only
                    # accepted by newer ffmpeg builds. The legacy `-strict <int>`
                    # flag is the portable form: -1 = unofficial (relaxed),
                    # -2 = experimental (most lax), 0 = strict (default).
                    "-strict",
                    "-1",
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
        compose_contact_sheet(source, output_file, frames, times, meta, width, cols, theme)
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
    theme: Theme = THEME_POTPLAYER,
) -> None:
    margin = 10
    gap = 8
    # Stamp style is intentionally theme-independent: white text on a dark
    # translucent box with a thin black halo keeps the time code readable
    # against every theme's sheet color (potplayer white, black_bg black,
    # white_bg white, pure_image white). Mixing it with the theme would make
    # white-on-white-bg / black-on-black-bg stamps disappear.
    STAMP_FILL = "#ffffff"
    STAMP_STROKE_WIDTH = 2
    STAMP_STROKE_FILL = "#000000"
    STAMP_BOX_ALPHA = 140
    # Bigger header text: bumped from /70 to /52 (≈ 1.35× larger), min raised 13→18.
    header_font_size = max(18, round(sheet_width / 52))
    stamp_font_size = max(12, round(sheet_width / 72))
    line_height = math.ceil(header_font_size * 1.35)
    max_header_chars = max(20, math.floor(sheet_width / (header_font_size * 1.1)))
    tile_w = math.floor((sheet_width - margin * 2 - gap * (cols - 1)) / cols)
    aspect_ratio = meta.width / meta.height if meta.width > 0 and meta.height > 0 else 16 / 9
    tile_h = round(tile_w / aspect_ratio)
    rows = math.ceil(len(frames) / cols)

    header_font = load_font(header_font_size, bold=True) if theme.draw_header else None
    stamp_font = load_font(stamp_font_size, bold=True)

    # Compute header block height based on theme layout:
    #   - draw_header=False -> 0 (no top block, frames sit flush)
    #   - header_mode="minimal" -> header_band_height (one line of filename);
    #     grows taller to wrap a long filename (band grows vertically).
    #   - header_mode="rich" (default) -> the original multi-line metadata block
    minimal_filename_lines: list[str] = []
    if not theme.draw_header:
        header_height = 0
        lines = []
    elif theme.header_mode == "minimal":
        # Slim band; one line is enough; the band height drives the layout.
        # Wrap the filename to fit the sheet width (with side padding) -- if it
        # wraps to N lines, the band grows to host all N lines plus padding.
        assert header_font is not None  # draw_header=True implies header_font is set
        raw_filename = Path(source.filename).name if source.filename else ""
        side_pad = 16
        minimal_filename_lines = _wrap_filename_to_width(
            raw_filename, header_font, max(1, sheet_width - side_pad * 2)
        )
        # Use header_font_size line height (consistent with the rendering below).
        # Header band minimum: theme.header_band_height (one line).
        # Each additional line adds header_font_size * 1.35 px, with 8px top/bottom pad.
        lines_per_wrap = len(minimal_filename_lines)
        extra_lines = max(0, lines_per_wrap - 1)
        header_height = (
            theme.header_band_height
            + math.ceil(extra_lines * header_font_size * 1.35)
        )
        lines = []  # rich-mode lines aren't drawn in minimal mode
    else:
        lines = build_header_lines(source, meta, max_header_chars)
        header_height = int(line_height * len(lines) + 18)

    # Footer band height (used by the metadata band at the bottom):
    footer_height = theme.footer_band_height if theme.footer_mode != "none" else 0

    # Top padding: only `pure_image` (draw_header=False, header_mode !=
    # "minimal") needs a top margin because it has neither a top band
    # nor a header block. `minimal_*` themes have an explicit top band
    # so they own their vertical space already; `rich` themes
    # (potplayer / black_bg / white_bg) keep the legacy `top_padding =
    # margin` from before the minimal mode was introduced. The user
    # reported the resulting "all-sides white border except top" was
    # visible only for `pure_image`, so that's the one we change.
    if theme.header_mode == "minimal":
        top_padding = 0
    elif not theme.draw_header:
        # pure_image: reserve `margin` (10) at the top so frames are
        # centered with the same visual breathing room as the left/right
        # /bottom margins.
        top_padding = margin
    else:
        top_padding = margin

    # Bottom margin is reserved when there's no footer band (legacy themes).
    if theme.footer_mode == "none":
        bottom_padding = margin
    else:
        bottom_padding = 0

    sheet_height = (
        header_height
        + top_padding
        + rows * tile_h + gap * (rows - 1)
        + bottom_padding
        + footer_height
    )

    sheet = Image.new("RGB", (sheet_width, sheet_height), theme.sheet_bg)
    draw = ImageDraw.Draw(sheet)

    # ----- Top band (minimal modes) -----
    if theme.header_mode == "minimal" and theme.draw_header:
        assert header_font is not None  # narrowing for Pyright
        band_bg = theme.header_band_bg if theme.header_band_bg is not None else theme.sheet_bg
        # The fill below always paints, even if band_bg == sheet_bg; that's a no-op
        # visual but cheap, and lets future themes set "borderless header".
        draw.rectangle((0, 0, sheet_width, header_height), fill=band_bg)
        # Filename only. Use the basename if `filename` looks like a path; otherwise as-is.
        # If the filename is long enough that it wraps onto multiple lines
        # (handled in the height-compute phase above), draw each wrapped line
        # stacked vertically within the now-taller band. Vertically centered as
        # a whole -- N lines occupy N*line_height px total; top y starts at
        # (header_height - N*line_height) / 2.
        n_lines = max(1, len(minimal_filename_lines))
        band_line_height = math.ceil(header_font_size * 1.35)
        block_h = n_lines * band_line_height
        first_y = max(8, int((header_height - block_h) / 2))
        # font is bold, no halo (user requested no stroke)
        for index, line in enumerate(minimal_filename_lines):
            draw_outlined_text(
                draw,
                (16, first_y + index * band_line_height),
                line,
                header_font,
                tuple2str(theme.text_fill),
                0 if theme.text_stroke is None else theme.text_stroke,
                stroke_fill=tuple2str(theme.stroke_fill),
            )
    elif theme.draw_header:
        # Rich header (legacy): multi-line metadata block on white sheet area.
        stroke_width = 0 if theme.text_stroke is None else theme.text_stroke
        for index, line in enumerate(lines):
            assert header_font is not None
            draw_outlined_text(
                draw,
                (12, 8 + index * line_height),
                line,
                header_font,
                tuple2str(theme.text_fill),
                stroke_width,
                stroke_fill=tuple2str(theme.stroke_fill),
            )

    # Offset for the grid: top band + top_padding.
    frame_grid_y = header_height + top_padding

    # ----- Bottom band (minimal modes) -----
    if theme.footer_mode == "minimal" and footer_height > 0:
        band_bg = theme.footer_band_bg if theme.footer_band_bg is not None else theme.sheet_bg
        # Place the footer band flush against the bottom edge.
        footer_y0 = sheet_height - footer_height
        draw.rectangle((0, footer_y0, sheet_width, sheet_height), fill=band_bg)
        # Build the metadata string: 1920x1080 | 00:30:12 | 60FPS | 668.76MB
        meta_str = _format_meta_str(source, meta)
        # Footer band uses the same size as the header band so the user's eye reads
        # them at the same visual weight (per request: "right-bottom metadata
        # should be the same size as the top-left filename text").
        meta_font_size = header_font_size
        meta_font = load_font(meta_font_size, bold=True)
        # Right-align: measure the text first, then place (right_padding px from edge).
        right_padding = 18
        tb = draw.textbbox((0, 0), meta_str, font=meta_font)
        text_w = tb[2] - tb[0]
        text_h = tb[3] - tb[1]
        tx = sheet_width - right_padding - text_w
        # Center vertically in the band, then shift up by textbbox top (Pillow
        # metrics: textbbox top is usually negative for fonts that have a tall
        # ascender above the cap line).
        ty = int(footer_y0 + (footer_height - text_h) / 2) - tb[1]
        draw_outlined_text(
            draw,
            (tx, ty),
            meta_str,
            meta_font,
            tuple2str(theme.text_fill),
            0 if theme.text_stroke is None else theme.text_stroke,
            stroke_fill=tuple2str(theme.stroke_fill),
        )

    for index, frame in enumerate(frames):

        row = index // cols
        col = index % cols
        x = margin + col * (tile_w + gap)
        y = frame_grid_y + row * (tile_h + gap)
        with Image.open(frame) as img:
            img = img.convert("RGB").resize((tile_w, tile_h), Image.Resampling.LANCZOS)
            # Pad invisible: no black fill on themes that already have black bg,
            # and on white themes where the frame could legitimately have black bars.
            draw.rectangle((x, y, x + tile_w, y + tile_h), fill=theme.sheet_bg)
            sheet.paste(img, (x, y))
        if theme.tile_ringfence:
            draw.rectangle(
                (x, y, x + tile_w - 1, y + tile_h - 1),
                outline=(60, 60, 60),
                width=1,
            )

        stamp = format_time(times[index])
        left, top, right, bottom = draw.textbbox((0, 0), stamp, font=stamp_font, stroke_width=STAMP_STROKE_WIDTH)
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
            alpha=STAMP_BOX_ALPHA,
        )
        draw_outlined_text(
            draw,
            (tx, ty),
            stamp,
            stamp_font,
            STAMP_FILL,
            STAMP_STROKE_WIDTH,
            stroke_fill=STAMP_STROKE_FILL,
        )

    sheet.save(output_file, "PNG")


def tuple2str(rgb: tuple[int, int, int]) -> str:
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


# Common video file extensions we want to keep attached to the
# previous line (don't split right before `.mp4`, `.mkv`, etc.).
# Used by _wrap_filename_to_width to reject cut points that would
# orphan the extension onto the next line.
_VIDEO_EXTS = (
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts",
    ".flv", ".wmv", ".m4v", ".mpg", ".mpeg", ".m2ts",
    ".3gp", ".3g2", ".rm", ".rmvb", ".vob", ".mts",
)
# Without leading dot (used by orphan check to also match `p4` after
# the dot has been cut off -- prevents the `m`/`p4` orphan).
_VIDEO_EXTS_NODOT = tuple(e.lstrip(".") for e in _VIDEO_EXTS)


def _orphans_video_ext(remaining: str, cut: int) -> bool:
    """True if cutting at `cut` would leave only a video file
    extension on the next line (e.g. `.mp4`, `.mkv`). The caller
    should pick a different break point in that case.

    We check both the raw rest (which may include the leading dot)
    and the rest without the dot (in case the cut point landed
    after the dot). Either way, the next line is just the extension.
    """
    rest = remaining[cut:].lstrip()
    rest_lower = rest.lower()
    if rest_lower.startswith(_VIDEO_EXTS):
        return True
    # Strip a single leading dot and check again.
    if rest_lower.startswith("."):
        rest_lower = rest_lower[1:]
    return rest_lower.startswith(_VIDEO_EXTS_NODOT)


def _wrap_filename_to_width(
    filename: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_pixel_width: int,
) -> list[str]:
    """Wrap the filename to multiple lines, balanced around a target.

    Algorithm: for each line we aim for a target of about 75% of
    `max_pixel_width` (configurable via BAL_TARGET). The walk goes:

      1. Find the largest i such that `font.getlength(remaining[:i])` fits
         in `soft_max`. This is the upper bound on the line length.
      2. Within the [soft_min, soft_max] window, the rightmost
         whitespace is the preferred cut point -- it gives a balanced
         fill (no huge right-side gap) and a word boundary.
      3. If no whitespace in the window, hard-break at the position
         closest to the target (BAL_TARGET) -- never at the overflow,
         which would leave an orphan tail on the next line.
      4. CJK runs (no whitespace) get the midpoint treatment too, so
         long Chinese filenames are split into lines of similar width
         rather than a single long line + a 2-char tail.

    Returns at least one line. If the whole string fits in one line
    (no wrap needed), a single-element list is returned.
    """
    if not filename:
        return [""]

    # Cheap early-out: whole string fits in one line.
    try:
        if font.getlength(filename) <= max_pixel_width:
            return [filename]
    except AttributeError:
        pass

    BAL_TARGET = 0.75   # aim for ~75% full before breaking
    BAL_MIN    = 0.50   # never break below this ratio (avoid orphan lines)
    soft_max = max_pixel_width
    soft_min = int(max_pixel_width * BAL_MIN)
    target_w = max_pixel_width * BAL_TARGET

    lines: list[str] = []
    remaining = filename
    while remaining:
        # Whole remaining fits in one line -- done.
        if font.getlength(remaining) <= soft_max:
            lines.append(remaining)
            break

        # Single pass to find the rightmost valid break point in the
        # [soft_min, soft_max] window. We track multiple candidates
        # in priority order (best -> last_fitting). Each candidate is
        # immediately filtered through `_orphans_video_ext` -- if
        # cutting here would orphan the file extension onto the next
        # line, we DON'T record it as a candidate. The rightmost
        # non-orphan candidate of each kind wins.
        last_fitting = 0
        target_break = 0
        best_ws = 0
        # Track two dot-break candidates:
        #   best_dot_keep -- cut is AT the dot, line ends in `.`
        #   best_dot_drop -- cut is BEFORE the dot, line does NOT end in `.`
        # We prefer best_dot_drop when picking a cut, to avoid the ugly
        # "trailing dot at end of line" pattern.
        best_dot_keep = 0
        best_dot_drop = 0
        for i in range(1, len(remaining) + 1):
            chunk = remaining[:i]
            w = font.getlength(chunk)
            if w > soft_max:
                break
            last_fitting = i
            if w <= target_w:
                target_break = i
            if w >= soft_min and i < len(remaining):
                ch = remaining[i - 1]
                # Skip any break that would orphan the extension.
                if _orphans_video_ext(remaining, i):
                    continue
                if ch.isspace():
                    best_ws = i  # rightmost wins
                elif ch == ".":
                    best_dot_keep = i  # cut AT the dot, line ends in `.`
                    # Also record the drop variant: cut BEFORE the dot,
                    # so the line does NOT end in `.`. The next line
                    # starts with `.` which is the extension prefix or a
                    # mid-string separator.
                    if not _orphans_video_ext(remaining, i + 1):
                        best_dot_drop = i + 1

        # Decision priority: whitespace > dot-without-trailing-dot >
        # hard-break at target > last_fitting. We strongly prefer
        # `best_dot_drop` over `best_dot_keep` so the line does not
        # end in a dangling dot. The drop variant places the dot at
        # the START of the next line, which is visually less awkward.
        chosen_cut = (
            best_ws
            or best_dot_drop
            or best_dot_keep
            or target_break
            or last_fitting
        )
        # If last_fitting itself orphans (pathological: the only thing
        # that fits past soft_min is the extension), try the next-lower
        # position one char at a time.
        while chosen_cut > 0 and _orphans_video_ext(remaining, chosen_cut):
            chosen_cut -= 1
        cut = chosen_cut if chosen_cut > 0 else 1

        cut = chosen_cut if chosen_cut > 0 else 1
        # Skip leading whitespace on the next line for cleanliness
        line = remaining[:cut].rstrip()
        if line:
            lines.append(line)
        remaining = remaining[cut:].lstrip()
        if not remaining:
            break

    return lines



def build_header_lines(source: SourceInfo, meta: MediaInfo, max_chars: int) -> list[str]:
    # Build the multi-line header for rich-mode themes (potplayer /
    # black_bg / white_bg). Uses _wrap_filename_to_width for the
    # filename split so we get pixel-accurate, balanced wrap with
    # extension preservation -- not the brittle char-counting split
    # that produced the .m / p4 orphan.
    prefix = "文件名: "
    name = source.filename or ""
    sheet_width = 1920
    header_font_size = max(18, round(sheet_width / 52))
    char_w = header_font_size * 1.1
    available_chars = max(20, max_chars)
    max_pixel_width = int(available_chars * char_w)
    font = load_font(header_font_size, bold=True)
    # Wrap using the same algorithm as minimal mode.
    wrapped = _wrap_filename_to_width(name, font, max_pixel_width)
    if not wrapped:
        file_lines = [prefix + name]
    else:
        file_lines = [prefix + wrapped[0]]
        for cont in wrapped[1:]:
            file_lines.append(cont)

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
    stroke_fill: str | tuple[int, int, int] = "#000000",
) -> None:
    draw.text(
        xy,
        text,
        font=font,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
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
