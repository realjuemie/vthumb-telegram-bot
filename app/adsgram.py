"""AdsGram chat ads. Credits are granted only from the reward callback log."""

import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

AD_API = "https://api.adsgram.ai/advbot"
CLAIM_WINDOW = 60


def numeric_block_id(raw: str) -> str:
    text = (raw or "").strip()
    if text.lower().startswith("bot-"):
        text = text[4:]
    return text if text.isdigit() else ""


def ad_request_url(block_id: str, token: str, user_id: int, language: str = "zh") -> str:
    query = urlencode(
        {
            "tgid": int(user_id),
            "blockid": numeric_block_id(block_id),
            "language": language or "zh",
            "token": token,
        }
    )
    return f"{AD_API}?{query}"


def parse_ad(payload: object) -> dict[str, str] | None:
    if not isinstance(payload, dict):
        return None
    click = str(payload.get("click_url") or "").strip()
    if not click.startswith("https://"):
        return None
    reward = str(payload.get("reward_url") or "").strip()
    if not reward.startswith("https://"):
        reward = ""
    image = str(payload.get("image_url") or "").strip()
    if not image.startswith("https://"):
        image = ""
    return {
        "text_html": str(payload.get("text_html") or "").strip() or "赞助消息",
        "image_url": image,
        "button_name": str(payload.get("button_name") or "").strip() or "查看",
        "click_url": click,
        "button_reward_name": str(payload.get("button_reward_name") or "").strip() or "领取奖励",
        "reward_url": reward,
    }


def fetch_ad(block_id: str, token: str, user_id: int, timeout: float = 8) -> dict[str, str] | None:
    block = numeric_block_id(block_id)
    if not block or not token or user_id is None:
        return None
    request = Request(
        ad_request_url(block, token, user_id),
        headers={"User-Agent": "vthumb-bot"},
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return parse_ad(payload)


def consume_claims(path: Path, state_path: Path, now: int, window: int = CLAIM_WINDOW) -> list[int]:
    """Return user ids for new reward callbacks. Same user inside `window` seconds counts once."""
    state = {"offset": 0, "recent": {}}
    if state_path.is_file():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state["offset"] = int(loaded.get("offset") or 0)
                recent = loaded.get("recent") or {}
                if isinstance(recent, dict):
                    state["recent"] = {str(k): int(v) for k, v in recent.items()}
        except (OSError, ValueError, TypeError):
            state = {"offset": 0, "recent": {}}
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    offset = int(state["offset"])
    if offset > len(lines) or offset < 0:
        offset = 0
    recent: dict[str, int] = dict(state["recent"])
    granted: list[int] = []
    for line in lines[offset:]:
        offset += 1
        try:
            item = json.loads(line)
            user_id = int(item["userid"])
            ts = int(item.get("ts") or now)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            continue
        if user_id <= 0:
            continue
        last = recent.get(str(user_id))
        if last is not None and ts - last < window:
            continue
        recent[str(user_id)] = ts
        granted.append(user_id)
    recent = {key: ts for key, ts in recent.items() if now - int(ts) < window}
    state_path.write_text(
        json.dumps({"offset": offset, "recent": recent}, ensure_ascii=False),
        encoding="utf-8",
    )
    return granted
