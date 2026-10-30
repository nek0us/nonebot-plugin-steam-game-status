from __future__ import annotations

import hashlib
import html
import io
import json
import base64
import asyncio
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse


STEAM_CARD_ANIMATION_FRAME_COUNT = 12
STEAM_CARD_ANIMATION_FRAME_DURATION_MS = 120
STEAM_CARD_ANIMATION_CAPTURE_INTERVAL_MS = 80
STEAM_CARD_ANIMATION_CAPTURE_DURATION_MS = 4000
STEAM_CARD_VIEWPORT_WIDTH = 356
STEAM_CARD_VIEWPORT_HEIGHT = 88
STEAM_CARD_COMPACT_VIEWPORT_WIDTH = 260
STEAM_CARD_COMPACT_VIEWPORT_HEIGHT = 72
STEAM_CARD_WIDE_VIEWPORT_WIDTH = 406
STEAM_CARD_WIDE_VIEWPORT_HEIGHT = 88
STEAM_CARD_LONG_GAME_NAME_LENGTH = 16
STEAM_GAME_BACKGROUND_URL_TEMPLATE = "https://cdn.akamai.steamstatic.com/steam/apps/{appid}/library_hero.jpg"
STEAM_STORE_ITEM_ASSETS_BASE_URL = "https://shared.fastly.steamstatic.com/store_item_assets/"
_BACKGROUND_CACHE: dict[str, tuple[float, str]] = {}
_BACKGROUND_LAST_VALID: dict[str, str] = {}
_BACKGROUND_LOCKS: dict[str, asyncio.Lock] = {}
_BACKGROUND_SUCCESS_TTL = 24 * 60 * 60
_BACKGROUND_FAILURE_TTL = 5 * 60


def get_steam_card_layout(game_name: str, *, dynamic: bool = False) -> tuple[str, int, int]:
    if dynamic:
        return "compact", STEAM_CARD_COMPACT_VIEWPORT_WIDTH, STEAM_CARD_COMPACT_VIEWPORT_HEIGHT
    if len(game_name) > STEAM_CARD_LONG_GAME_NAME_LENGTH:
        return "wide", STEAM_CARD_WIDE_VIEWPORT_WIDTH, STEAM_CARD_WIDE_VIEWPORT_HEIGHT
    return "", STEAM_CARD_VIEWPORT_WIDTH, STEAM_CARD_VIEWPORT_HEIGHT


def is_animated_image_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".gif")


def build_steam_game_background_url(appid: str) -> str:
    if not appid:
        return ""
    return STEAM_GAME_BACKGROUND_URL_TEMPLATE.format(appid=appid)


def _parse_store_item_background(payload: dict, appid: str) -> str:
    """Extract a library hero URL for the requested app from GetItems response."""
    if not isinstance(payload, dict):
        return ""
    response = payload.get("response", {})
    if not isinstance(response, dict):
        return ""
    items = response.get("store_items", [])
    if not isinstance(items, list):
        return ""
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("appid", item.get("id"))
        if str(item_id) != appid or item.get("success") not in (1, True, "1"):
            continue
        assets = item.get("assets")
        if not isinstance(assets, dict):
            continue
        filename = assets.get("library_hero") or assets.get("library_hero_2x")
        url_format = assets.get("asset_url_format")
        if (
            not isinstance(filename, str)
            or not filename
            or not isinstance(url_format, str)
            or not url_format
            or "${FILENAME}" not in url_format
        ):
            continue
        # Steam formats can contain either an explicit filename slot or a
        # relative asset path. Prefix only relative paths; preserve query tokens.
        url = url_format.replace("${FILENAME}", filename)
        if not urlparse(url).scheme:
            url = STEAM_STORE_ITEM_ASSETS_BASE_URL + url.lstrip("/")
        return url
    return ""


async def _fetch_steam_game_background_url(appid: str) -> str:
    from nonebot.internal.driver import Request
    from .config import get_steam_api_domain
    from .model import SafeResponse
    from .utils import http_client

    request_data = {
        "ids": [{"appid": int(appid)}],
        "context": {"language": "english", "country_code": "US"},
        "data_request": {"include_assets": True},
    }
    url = f"https://{get_steam_api_domain()}/IStoreBrowseService/GetItems/v1/?{urlencode({'input_json': json.dumps(request_data, separators=(',', ':'))})}"
    async with http_client() as client:
        response = SafeResponse(await client.request(Request("GET", url, timeout=5)))
    if response.status_code != 200:
        return ""
    content = response.content
    if not isinstance(content, (str, bytes)):
        return ""
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return ""
    return _parse_store_item_background(payload, appid)


async def resolve_steam_game_background_url(appid: str) -> str:
    """Resolve the current Steam hero asset, falling back to the legacy URL."""
    appid = str(appid).strip()
    if not appid.isdecimal() or int(appid) <= 0:
        return ""
    appid = str(int(appid))
    now = time.monotonic()
    cached = _BACKGROUND_CACHE.get(appid)
    if cached and cached[0] > now:
        return cached[1]

    lock = _BACKGROUND_LOCKS.setdefault(appid, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = _BACKGROUND_CACHE.get(appid)
        if cached and cached[0] > now:
            return cached[1]
        try:
            resolved = await _fetch_steam_game_background_url(appid)
        except Exception:
            resolved = ""
        if resolved:
            _BACKGROUND_LAST_VALID[appid] = resolved
            result = resolved
            ttl = _BACKGROUND_SUCCESS_TTL
        else:
            result = _BACKGROUND_LAST_VALID.get(appid) or build_steam_game_background_url(appid)
            ttl = _BACKGROUND_FAILURE_TTL
        _BACKGROUND_CACHE[appid] = (time.monotonic() + ttl, result)
        return result


def _append_card_class(card_class: str, extra_class: str) -> str:
    return f"{card_class} {extra_class}".strip()


def build_steam_card_cache_key(
    *,
    avatar_url: str,
    player_name: str,
    action_text: str,
    game_name: str,
    template_digest: str,
    card_class: str = "",
    frame_count: int = STEAM_CARD_ANIMATION_FRAME_COUNT,
    frame_duration_ms: int = STEAM_CARD_ANIMATION_FRAME_DURATION_MS,
    capture_interval_ms: int = STEAM_CARD_ANIMATION_CAPTURE_INTERVAL_MS,
    capture_duration_ms: int = STEAM_CARD_ANIMATION_CAPTURE_DURATION_MS,
    preserve_avatar_timing: bool = False,
    max_avatar_frames: int = 120,
    background_url: str = "",
) -> str:
    cache_data = {
        "avatar_url": avatar_url,
        "player_name": player_name,
        "action_text": action_text,
        "game_name": game_name,
        "template_digest": template_digest,
        "card_class": card_class,
        "frame_count": frame_count,
        "frame_duration_ms": frame_duration_ms,
        "capture_interval_ms": capture_interval_ms,
        "capture_duration_ms": capture_duration_ms,
        "preserve_avatar_timing": preserve_avatar_timing,
        "max_avatar_frames": max_avatar_frames,
        "background_url": background_url,
    }
    cache_json = json.dumps(cache_data, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(cache_json.encode("utf8")).hexdigest()


def render_steam_card_template(
    *,
    template_html: str,
    avatar_url: str,
    player_name: str,
    action_text: str,
    game_name: str,
    card_class: str = "",
    background_url: str = "",
) -> str:
    background_style = ""
    if background_url:
        background_style = f"--game-background-url: url('{html.escape(background_url, quote=True)}');"

    return (
        template_html
        .replace("{{ card_class }}", html.escape(card_class))
        .replace("{{ background_style }}", background_style)
        .replace("{{ avatar_url }}", html.escape(avatar_url, quote=True))
        .replace("{{ player_name }}", html.escape(player_name))
        .replace("{{ action_text }}", html.escape(action_text))
        .replace("{{ game_name }}", html.escape(game_name))
    )


async def render_steam_card(
    avatar_url: str,
    player_name: str,
    game_name: str,
    action_text: str,
    background_url: str = "",
    avatar_grayscale: bool = False,
    background_grayscale: bool = False,
    stopped: bool = False,
) -> Optional[bytes]:
    from nonebot.log import logger
    from nonebot_plugin_htmlrender import template_to_pic
    from .cache import get_cached_image_url

    try:
        avatar_url = await get_cached_image_url(avatar_url, grayscale=avatar_grayscale)
        if background_url:
            background_url = await get_cached_image_url(
                background_url, grayscale=background_grayscale
            )
        template_path = str(Path(__file__).parent / "templates")

        if not (Path(__file__).parent / "templates" / "steam_card.html").exists():
            logger.warning("Steam卡片模板文件 steam_card.html 不存在，跳过渲染")
            return None

        card_class, viewport_width, viewport_height = get_steam_card_layout(game_name)
        if background_url:
            card_class = _append_card_class(card_class, "game-bg")
            if background_grayscale:
                card_class = _append_card_class(card_class, "grayscale-background")
        if stopped:
            card_class = _append_card_class(card_class, "stopped")
        if avatar_grayscale:
            card_class = _append_card_class(card_class, "grayscale-avatar")
        background_style = ""
        if background_url:
            background_style = f"--game-background-url: url('{html.escape(background_url, quote=True)}');"
        pic_data = await template_to_pic(
            template_path=template_path,
            template_name="steam_card.html",
            templates={
                "card_class": card_class,
                "avatar_url": avatar_url,
                "player_name": player_name,
                "action_text": action_text,
                "game_name": game_name,
                "background_style": background_style,
            },
            pages={
                "viewport": {"width": viewport_width, "height": viewport_height},
                "base_url": f"file://{template_path}",
            },
            wait=1,
        )
        return pic_data
    except Exception as e:
        logger.error(f"渲染 Steam 卡片失败: {e}")
        return None


def _image_to_png_data_url(image: PILImage.Image) -> str:
    output = io.BytesIO()
    image.save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _limit_avatar_gif_frames(
    frames: list[tuple[PILImage.Image, int]],
    max_frames: int,
) -> list[tuple[PILImage.Image, int]]:
    if max_frames <= 0 or len(frames) <= max_frames:
        return frames

    limited_frames = []
    for index in range(max_frames):
        start = round(index * len(frames) / max_frames)
        end = round((index + 1) * len(frames) / max_frames)
        if end <= start:
            end = start + 1
        duration = sum(frame_duration for _, frame_duration in frames[start:end])
        limited_frames.append((frames[start][0], duration))
    return limited_frames


async def _download_avatar_gif_frames(avatar_url: str) -> Optional[list[tuple[PILImage.Image, int]]]:
    from nonebot.internal.driver import Request
    from nonebot.log import logger
    from PIL import Image as PILImage, ImageSequence

    from .config import config_steam
    from .model import SafeResponse
    from .utils import http_client

    try:
        async with http_client() as client:
            res = SafeResponse(await client.request(Request("GET", avatar_url, timeout=30)))
    except Exception as e:
        logger.debug(f"Steam 动态头像 GIF 下载异常，跳过保留原时序模式：{e.args}")
        return None

    if res.status_code != 200 or not isinstance(res.content, bytes):
        logger.debug(f"Steam 动态头像 GIF 下载失败，状态码:{res.status_code}")
        return None

    try:
        avatar_gif = PILImage.open(io.BytesIO(res.content))
    except Exception as e:
        logger.debug(f"Steam 动态头像 GIF 读取失败，跳过保留原时序模式：{e.args}")
        return None

    if not getattr(avatar_gif, "is_animated", False):
        return None

    frames = []
    for frame in ImageSequence.Iterator(avatar_gif):
        duration = int(frame.info.get("duration") or 100)
        frames.append((frame.copy().convert("RGBA"), max(20, duration)))

    if not frames:
        return None
    return _limit_avatar_gif_frames(frames, config_steam.steam_dynamic_card_max_avatar_frames)


async def _render_dynamic_steam_card_with_avatar_frames(
    *,
    template_html: str,
    avatar_frames: list[tuple[PILImage.Image, int]],
    player_name: str,
    game_name: str,
    action_text: str,
    card_class: str,
    viewport_width: int,
    viewport_height: int,
    background_url: str,
) -> Optional[bytes]:
    from PIL import Image as PILImage

    from .config import config_steam
    from .utils import playwright_context

    html_content = render_steam_card_template(
        template_html=template_html,
        avatar_url=_image_to_png_data_url(avatar_frames[0][0]),
        player_name=player_name,
        action_text=action_text,
        game_name=game_name,
        card_class=card_class,
        background_url=background_url,
    )

    card_frames = []
    async with playwright_context() as pc:
        page = await pc.new_page()
        await page.set_viewport_size({"width": viewport_width, "height": viewport_height})
        await page.set_content(
            html_content,
            wait_until="networkidle",
            timeout=config_steam.steam_dynamic_card_timeout_ms,
        )
        await page.wait_for_selector(
            ".steam-card",
            state="visible",
            timeout=config_steam.steam_dynamic_card_timeout_ms,
        )
        card = await page.query_selector(".steam-card")
        if not card:
            return None

        for avatar_frame, _ in avatar_frames:
            await page.evaluate(
                "(src) => { document.querySelector('.avatar').src = src; }",
                _image_to_png_data_url(avatar_frame),
            )
            frame_bytes = await card.screenshot(type="png", omit_background=True)
            card_frames.append(PILImage.open(io.BytesIO(frame_bytes)).convert("RGBA"))

    if not card_frames:
        return None

    output = io.BytesIO()
    card_frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=card_frames[1:],
        duration=[duration for _, duration in avatar_frames],
        loop=0,
        disposal=2,
    )
    return output.getvalue()


async def render_dynamic_steam_card(
    avatar_url: str,
    player_name: str,
    game_name: str,
    action_text: str,
    background_url: str = "",
    avatar_grayscale: bool = False,
    background_grayscale: bool = False,
    stopped: bool = False,
) -> Optional[bytes]:
    from nonebot.log import logger

    from .config import config_steam
    from .source import dynamic_card_cache_dir
    from .utils import playwright_context

    if not config_steam.steam_dynamic_avatar_card or not is_animated_image_url(avatar_url):
        return None

    try:
        from PIL import Image as PILImage

        template_path = Path(__file__).parent / "templates"
        template_file = template_path / "steam_card.html"
        if not template_file.exists():
            logger.warning("Steam卡片模板文件 steam_card.html 不存在，跳过动态渲染")
            return None

        template_html = template_file.read_text("utf8")
        template_digest = hashlib.sha256(template_html.encode("utf8")).hexdigest()
        card_class, viewport_width, viewport_height = get_steam_card_layout(game_name, dynamic=True)
        if background_url:
            card_class = _append_card_class(card_class, "game-bg")
            if background_grayscale:
                card_class = _append_card_class(card_class, "grayscale-background")
        if stopped:
            card_class = _append_card_class(card_class, "stopped")
        if avatar_grayscale:
            card_class = _append_card_class(card_class, "grayscale-avatar")
        if config_steam.steam_dynamic_card_capture_duration_ms > 0:
            frame_count = max(
                config_steam.steam_dynamic_card_frame_count,
                (
                    config_steam.steam_dynamic_card_capture_duration_ms
                    + config_steam.steam_dynamic_card_capture_interval_ms
                    - 1
                )
                // config_steam.steam_dynamic_card_capture_interval_ms,
            )
            frame_duration_ms = max(
                20,
                round(config_steam.steam_dynamic_card_capture_duration_ms / frame_count),
            )
            capture_interval_ms = frame_duration_ms
        else:
            frame_count = config_steam.steam_dynamic_card_frame_count
            frame_duration_ms = config_steam.steam_dynamic_card_frame_duration_ms
            capture_interval_ms = config_steam.steam_dynamic_card_capture_interval_ms
        preserve_avatar_timing = config_steam.steam_dynamic_card_preserve_avatar_gif_timing
        cache_key = build_steam_card_cache_key(
            avatar_url=avatar_url,
            player_name=player_name,
            action_text=action_text,
            game_name=game_name,
            template_digest=template_digest,
            card_class=card_class,
            frame_count=frame_count,
            frame_duration_ms=frame_duration_ms,
            capture_interval_ms=capture_interval_ms,
            capture_duration_ms=config_steam.steam_dynamic_card_capture_duration_ms,
            preserve_avatar_timing=preserve_avatar_timing,
            max_avatar_frames=config_steam.steam_dynamic_card_max_avatar_frames,
            background_url=background_url,
        )
        cache_file = dynamic_card_cache_dir / f"{cache_key}.gif"
        if config_steam.steam_dynamic_card_cache and cache_file.exists():
            cache_file.touch()
            logger.debug(f"Steam 动态卡片缓存命中: {cache_key}")
            return cache_file.read_bytes()

        if preserve_avatar_timing:
            avatar_frames = await _download_avatar_gif_frames(avatar_url)
            if avatar_frames:
                gif_data = await _render_dynamic_steam_card_with_avatar_frames(
                    template_html=template_html,
                    avatar_frames=avatar_frames,
                    player_name=player_name,
                    game_name=game_name,
                    action_text=action_text,
                    card_class=card_class,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    background_url=background_url,
                )
                if gif_data:
                    if config_steam.steam_dynamic_card_cache:
                        cache_file.write_bytes(gif_data)
                        logger.debug(f"Steam 动态卡片原时序缓存写入: {cache_key}")
                    return gif_data
            logger.debug("Steam 动态卡片保留原头像 GIF 时序失败，回退截图采样模式")
            cache_file = None

        html_content = render_steam_card_template(
            template_html=template_html,
            avatar_url=avatar_url,
            player_name=player_name,
            action_text=action_text,
            game_name=game_name,
            card_class=card_class,
            background_url=background_url,
        )
        frames = []
        async with playwright_context() as pc:
            page = await pc.new_page()
            await page.set_viewport_size({"width": viewport_width, "height": viewport_height})
            await page.set_content(
                html_content,
                wait_until="networkidle",
                timeout=config_steam.steam_dynamic_card_timeout_ms,
            )
            await page.wait_for_selector(
                ".steam-card",
                state="visible",
                timeout=config_steam.steam_dynamic_card_timeout_ms,
            )
            await page.wait_for_timeout(200)
            card = await page.query_selector(".steam-card")
            if not card:
                return None

            for _ in range(frame_count):
                frame_bytes = await card.screenshot(type="png", omit_background=True)
                frames.append(PILImage.open(io.BytesIO(frame_bytes)).convert("RGBA"))
                await page.wait_for_timeout(capture_interval_ms)

        if not frames:
            return None

        output = io.BytesIO()
        frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=frame_duration_ms,
            loop=0,
            disposal=2,
        )
        gif_data = output.getvalue()
        if config_steam.steam_dynamic_card_cache and cache_file:
            cache_file.write_bytes(gif_data)
            logger.debug(f"Steam 动态卡片缓存写入: {cache_key}")
        return gif_data
    except Exception as e:
        logger.error(f"渲染 Steam 动态卡片失败: {e}")
        return None


async def render_bind_card(avatar_url: str, player_name: str, steam_id: str) -> Optional[bytes]:
    from nonebot.log import logger
    from nonebot_plugin_htmlrender import template_to_pic
    from .cache import get_cached_image_url

    try:
        avatar_url = await get_cached_image_url(avatar_url)
        template_path = str(Path(__file__).parent / "templates")

        if not (Path(__file__).parent / "templates" / "steam_bind_card.html").exists():
            logger.warning("Steam绑定模板文件 steam_bind_card.html 不存在，跳过渲染")
            return None

        pic_data = await template_to_pic(
            template_path=template_path,
            template_name="steam_bind_card.html",
            templates={
                "avatar_url": avatar_url,
                "player_name": player_name,
                "steam_id": steam_id,
            },
            pages={
                "viewport": {"width": 342, "height": 330},
                "base_url": f"file://{template_path}",
            },
            wait=1,
        )
        return pic_data
    except Exception as e:
        logger.error(f"渲染 Steam 绑定卡片失败: {e}")
        return None


async def render_help_card() -> Optional[bytes]:
    from nonebot.log import logger
    from nonebot_plugin_htmlrender import template_to_pic

    try:
        template_path = str(Path(__file__).parent / "templates")

        if not (Path(__file__).parent / "templates" / "steam_help_card.html").exists():
            logger.warning("Steam帮助模板文件 steam_help_card.html 不存在，跳过渲染")
            return None

        return await template_to_pic(
            template_path=template_path,
            template_name="steam_help_card.html",
            templates={},
            pages={
                "viewport": {"width": 760, "height": 780},
                "base_url": f"file://{template_path}",
            },
            wait=1,
        )
    except Exception as e:
        logger.error(f"渲染 Steam 帮助卡片失败: {e}")
        return None
