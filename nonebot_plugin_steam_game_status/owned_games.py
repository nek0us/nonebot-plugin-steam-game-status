import asyncio
import time
from typing import Dict, List, Optional

from nonebot.log import logger
from nonebot_plugin_alconna.uniseg import UniMessage

from .api import (
    gameid_to_name,
    get_game_info,
    get_group_target_bot,
    get_owned_games,
    save_data,
    test_group_active,
)
from .config import config_steam
from .source import group_list, owned_games, steam_list


_DEMO_TYPE_CACHE: Dict[str, tuple[float, Optional[bool]]] = {}
_DEMO_TYPE_SUCCESS_TTL = 24 * 60 * 60
_DEMO_TYPE_UNKNOWN_TTL = 5 * 60


async def _is_demo_app(appid: str) -> bool:
    """Return whether Steam explicitly identifies this app as a demo."""
    now = time.monotonic()
    cached = _DEMO_TYPE_CACHE.get(str(appid))
    if cached and cached[0] > now:
        return cached[1] is True

    try:
        info = await get_game_info(str(appid), timeout=5)
    except Exception as error:
        logger.debug(f"Steam demo 类型查询失败，appid:{appid}，按普通游戏处理：{error}")
        info = None

    data = info.get("data") if isinstance(info, dict) else None
    app_type = data.get("type") if isinstance(data, dict) else None
    if isinstance(info, dict) and info.get("success") is True and isinstance(app_type, str) and app_type.strip():
        is_demo = app_type.strip().lower() == "demo"
        _DEMO_TYPE_CACHE[str(appid)] = (time.monotonic() + _DEMO_TYPE_SUCCESS_TTL, is_demo)
        return is_demo

    logger.debug(f"Steam demo 类型未知，appid:{appid}，按普通游戏处理")
    _DEMO_TYPE_CACHE[str(appid)] = (time.monotonic() + _DEMO_TYPE_UNKNOWN_TTL, None)
    return False


async def initialize_owned_games_baseline(steam_ids: List[str]) -> tuple[int, List[str]]:
    semaphore = asyncio.Semaphore(config_steam.steam_owned_game_baseline_concurrency)
    target_steam_ids = [steam_id for steam_id in set(steam_ids) if steam_id not in owned_games]

    async def fetch_baseline(steam_id: str) -> tuple[str, Optional[Dict[str, str]], bool]:
        async with semaphore:
            try:
                return steam_id, await get_owned_games(steam_id), False
            except Exception as e:
                logger.warning(f"steam游戏库基准建立异常，steam_id:{steam_id}：{e.args}")
                return steam_id, None, True

    created = 0
    failed_steam_ids = []
    for steam_id, current_games, has_error in await asyncio.gather(
        *(fetch_baseline(steam_id) for steam_id in target_steam_ids)
    ):
        if steam_id in owned_games:
            continue
        if has_error:
            failed_steam_ids.append(steam_id)
            continue
        if current_games is None:
            failed_steam_ids.append(steam_id)
            nickname = steam_list.get(steam_id, {}).get("nickname", "")
            logger.warning(
                f"steam游戏库基准建立失败，steam_id:{steam_id}，昵称:{nickname}，可能游戏库不可见或接口返回为空"
            )
            continue
        owned_games[steam_id] = current_games
        created += 1
        logger.info(f"steam游戏库入库播报建立基准，steam_id:{steam_id}，游戏数：{len(current_games)}")
    return created, failed_steam_ids


async def run_owned_games_check() -> None:
    if not config_steam.steam_web_key:
        return

    steam_id_to_groups: Dict[str, List[str]] = {}
    for group_id, group_data in group_list.items():
        if group_data.get("status") and group_data.get("owned_game", False):
            for steam_id in group_data["user_list"]:
                if steam_id not in steam_id_to_groups:
                    steam_id_to_groups[steam_id] = []
                steam_id_to_groups[steam_id].append(group_id)

    if not steam_id_to_groups:
        logger.debug("steam游戏库入库播报未开启，跳过本次检查")
        return

    async def fetch_owned_games(steam_id: str):
        try:
            return steam_id, await get_owned_games(steam_id)
        except Exception as e:
            logger.warning(f"steam游戏库获取异常，steam_id:{steam_id}，跳过本次更新：{e.args}")
            return steam_id, None

    logger.info(f"steam开始检查游戏库入库变更，用户数：{len(steam_id_to_groups)}")
    semaphore = asyncio.Semaphore(config_steam.steam_owned_game_query_concurrency)

    async def limited_fetch_owned_games(steam_id: str):
        async with semaphore:
            return await fetch_owned_games(steam_id)

    owned_game_results = await asyncio.gather(
        *(limited_fetch_owned_games(steam_id) for steam_id in steam_id_to_groups)
    )
    for steam_id, current_games in owned_game_results:
        group_ids = steam_id_to_groups[steam_id]
        if current_games is None:
            continue

        if steam_id not in owned_games:
            owned_games[steam_id] = current_games
            logger.info(f"steam游戏库入库播报建立基准，steam_id:{steam_id}，游戏数：{len(current_games)}")
            continue

        old_games = owned_games[steam_id]
        new_appids = sorted(set(current_games) - set(old_games))
        merged_games = {**old_games, **current_games}

        if not new_appids:
            owned_games[steam_id] = merged_games
            continue

        filtered_demo_appids = []
        announced_appids = new_appids
        if not config_steam.steam_owned_game_include_demo:
            filtered_demo_appids = [appid for appid in new_appids if await _is_demo_app(appid)]
            if filtered_demo_appids:
                filtered_demo_set = set(filtered_demo_appids)
                announced_appids = [appid for appid in new_appids if appid not in filtered_demo_set]
            if not announced_appids:
                owned_games[steam_id] = merged_games
                logger.debug(
                    f"Steam 游戏库新增内容均为 Demo，已更新基准但不发送消息，steam_id:{steam_id}，appid:{filtered_demo_appids}"
                )
                continue

        player_name = steam_list.get(steam_id, {}).get("nickname", steam_id)
        game_lines = []
        for appid in announced_appids:
            game_name = await gameid_to_name(appid, current_games[appid])
            game_lines.append(f"《{game_name or current_games[appid]}》")
        message_text = f"{player_name} 的 Steam 游戏库新增了：\n" + "\n".join(game_lines)
        sent = False

        for group_id in group_ids:
            target, bot = await get_group_target_bot(group_id)
            if target:
                try:
                    logger.info(f"群 {group_id} 发送 Steam 游戏库入库播报: {player_name} -> {announced_appids}")
                    await UniMessage(message_text).send(target=target, bot=bot)
                    sent = True
                except Exception as e:
                    logger.warning(f"群 {group_id} 发送 Steam 游戏库入库播报失败: {e}")
            else:
                await test_group_active(group_id)

        if sent:
            owned_games[steam_id] = merged_games
        else:
            if filtered_demo_appids:
                owned_games[steam_id] = {
                    **old_games,
                    **{appid: current_games[appid] for appid in filtered_demo_appids},
                }
            logger.warning(f"Steam 游戏库入库播报全部发送失败，保留旧基准等待下次重试，steam_id:{steam_id}")

    save_data()
    logger.info("steam游戏库入库检查任务完成")
