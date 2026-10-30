import sys
import json
import time
import random
import asyncio

from typing import Dict, List, Optional, Literal
from nonebot import require
from nonebot.permission import SUPERUSER
from nonebot.log import logger
from nonebot.matcher import Matcher
from nonebot.plugin import PluginMetadata
from nonebot.internal.driver import Request
from nonebot.exception import MatcherException
from nonebot.exception import ActionFailed, NetworkError
from nonebot.plugin import inherit_supported_adapters

from arclet.alconna import Alconna, Option, Args, CommandMeta, AllParam

from .duration import format_playtime_duration
from .utils import http_client, driver, HTTPClientSession, to_enum
from .card import (
    resolve_steam_game_background_url,
    render_bind_card,
    render_dynamic_steam_card,
    render_help_card,
    render_steam_card,
)
from .avatar import normalize_steam_avatar_url, resolve_avatar_url
from .model import UserData, SafeResponse, create_group_data
from .status_state import (
    HandledStatePersistenceError,
    build_steam_id_to_groups,
    decide_status_transition,
    delivery_snapshot_is_current,
    persist_handled_event,
    persist_then_send,
)
from .config import Config, __version__, config_steam, bot_name, get_steam_api_domain
from .api import (
    clear_inactive_groups_list,
    gameid_to_name,
    get_inactive_groups_list,
    steam_link_rule,
    get_game_info,
    get_steam_key,
    save_data,
    no_private_rule,
    get_game_data_msg,
    make_game_data_node_msg,
    send_node_msg,
    get_free_games_info,
    game_discounted_subscribe,
    get_group_target_bot,
    test_group_active,
    get_steam_playtime,
    SteamPlaytimeUnavailableError,
    get_discounted_games_info,
)
from .owned_games import initialize_owned_games_baseline, run_owned_games_check
from .source import (
    new_file_group,
    new_file_steam,
    save_reported_steam_state,
    exclude_game_file,
    exclude_game_default,
    steam_list,
    reported_steam_state,
    group_list,
    exclude_game,
    inactive_groups,
    atomic_write_json,
    get_delivery_group_lock,
    get_delivery_group_generation,
    bump_delivery_group_generation,
    game_discounted_cache,
    dynamic_card_cache_dir,
    image_resource_cache_dir,
)
from .cache import cleanup_expired_cache_files

require("nonebot_plugin_alconna")
from nonebot_plugin_alconna import Arparma, on_alconna, Match  # noqa: E402
from nonebot_plugin_alconna.uniseg import UniMessage, CustomNode, Reference, MsgTarget  # noqa: E402

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler  # noqa: E402

require("nonebot_plugin_htmlrender")

# 仅用于“无游戏”连续确认；重启后清空会更保守地延迟一次停止播报。
pending_stop_counts: Dict[str, Dict[str, int]] = {}


def _delivery_is_current(
    group_id: str,
    steam_id: str,
    expected_reported: Optional[UserData],
    expected_generation: int,
) -> bool:
    """在群锁内确认任务快照尚未被解绑、关闭播报或另一任务替换。"""
    return delivery_snapshot_is_current(
        group_list.get(group_id),
        steam_id,
        expected_reported,
        reported_steam_state.get(group_id, {}).get(steam_id),
        expected_generation,
        get_delivery_group_generation(group_id),
    )


def _persist_group_event(
    group_id: str,
    steam_id: str,
    action_type: str,
    current_game: UserData,
) -> bool:
    """调用方持有群锁时消费并落盘；失败会恢复内存状态。"""
    try:
        persist_handled_event(
            reported_steam_state,
            group_id,
            steam_id,
            action_type,
            current_game,
            lambda: save_reported_steam_state(reported_steam_state),
        )
    except Exception as error:
        logger.error(
            "Steam 状态事件落盘失败，未发送且允许后续重新处理 "
            f"steam_id:{steam_id} 群:{group_id} action:{action_type}: {error}"
        )
        return False
    return True


__plugin_meta__ = PluginMetadata(
    name="Steam游戏状态",
    description="播报群友的Steam游戏状态",
    usage="""首先获取自己的Steam ID，
        获取方法：
            获取Steam ID 64
                Steam 桌面网站或桌面客户端：点开右上角昵称下拉菜单，点击账户明细，即可看到 Steam ID
                Steam 应用：点击右上角头像，点击账户明细，即可看到 Steam ID
            获取Steam好友代码
                Steam 桌面网站或桌面客户端：点开导航栏 好友 选项卡，点击添加好友，即可看到 Steam 好友代码
                Steam 应用：点击右上角头像，点击好友，点击添加好友，即可看到 Steam 好友代码
        (如果有命令前缀，需要加上，一般为 / )    

        绑定方法：
            steam绑定/steam添加/steam.add [个人ID数值] 

        删除方法：
            steam解绑/steam删除/steam.del [个人ID数值] 

        命令：
            steam列表/steam绑定列表 	   
            steam屏蔽 [游戏名]
            steam恢复 [游戏名]
            steam排除列表
            steam播报开启/steam播报打开  
            steam播报关闭/steam播报停止 
            steam图片播报开启/steam图片播报关闭
            steam图片背景开启/steam图片背景关闭
            steam结束图片播报开启/steam结束图片播报关闭
            steam结束图片背景开启/steam结束图片背景关闭
            steam结束头像黑白开启/steam结束头像黑白关闭
            steam结束背景黑白开启/steam结束背景黑白关闭
                默认开始/切换游戏使用图片播报，结束游戏使用文字播报。
            steam入库播报开启/steam入库播报关闭
                默认关闭。按 steam_owned_game_interval 检查公开游戏库，只播报新增游戏。
            steam喜加一
            steam喜加一订阅
            steam喜加一退订
            steam帮助

        链接识别：
            从商店复制链接
    """,
    type="application",
    config=Config,
    homepage="https://github.com/nek0us/nonebot_plugin_steam_game_status",
    supported_adapters=inherit_supported_adapters("nonebot_plugin_alconna"),
    extra={
        "author": "nek0us",
        "version": __version__,
        "priority": config_steam.steam_command_priority
    }
)


steam_link_re_alc = Alconna(
    "steam_open",
    Args["appid", int],
    meta=CommandMeta(compact=True, strict=False)
)
steam_link_re_alc.shortcut(
    r".*https?://store\.steampowered\.com/app/(\d+).*",
    {"args": ["{0}"], "prefix": False}
)
steam_link_re = on_alconna(
    steam_link_re_alc,
    rule=steam_link_rule,
    block=False,
    priority=config_steam.steam_command_priority
)


@steam_link_re.handle()
async def steam_link_handle(target: MsgTarget, matcher: Matcher, appid: Match[str]):
    app_id = str(appid.result)
    try:
        res_json = await get_game_info(app_id, timeout=config_steam.steam_link_timeout_seconds)
        if 'error' in res_json:
            logger.warning(f"steam链接识别失败，异常为：{res_json['error']}")
            await matcher.finish("steam链接失败，请检查日志输出")
        if not res_json['success']:
            logger.info(f"steam链接游戏信息获取失败，疑似appid错误：{app_id}")
            await matcher.finish("没有找到这个游戏", reply_message=True)
        id = str(target.id)
        if res_json["from"] != "cn":
            if isinstance(config_steam.steam_area_game, bool):
                if not config_steam.steam_area_game:
                    await matcher.finish("没有找到这个游戏", reply_message=True)
            else:
                if id not in config_steam.steam_area_game:
                    await matcher.finish("没有找到这个游戏", reply_message=True)
        game_data = res_json['data']
        if "ratings" in game_data:
            if "steam_germany" in game_data["ratings"]:
                if game_data["ratings"]["steam_germany"]['rating'] == "BANNED":
                    if isinstance(config_steam.steam_link_r18_game, bool):
                        if not config_steam.steam_link_r18_game:
                            logger.info(f"steam appid:{app_id} 根据r18设置被过滤")
                            await matcher.finish("这个禁止！", reply_message=True)
                    else:
                        if id not in config_steam.steam_link_r18_game:
                            logger.info(f"steam appid:{app_id} 根据r18设置被过滤，{id} 不在白名单内")
                            await matcher.finish("这个禁止！", reply_message=True)
        forward_name, msgs = await get_game_data_msg(res_json)
        messages = await make_game_data_node_msg(target, forward_name, msgs)
        await send_node_msg(messages, app_id)
        logger.debug(f"steam app_id: {app_id} 解析完成")
    except Exception as e:
        logger.warning(f"steam app_id: {app_id} 解析失败：{e.args}")
        await matcher.send(f"steam app_id：{app_id} 解析失败 {config_steam.steam_tail_tone}")


@driver.on_startup
async def _():
    removed_cache_files = cleanup_expired_cache_files(
        dynamic_card_cache_dir, config_steam.steam_image_cache_retention_days
    )
    removed_cache_files += cleanup_expired_cache_files(
        image_resource_cache_dir, config_steam.steam_image_cache_retention_days
    )
    if removed_cache_files:
        logger.info(f"Cleaned {removed_cache_files} expired Steam image cache files")

    # 重启后不再沿用原始观测的开始时间，避免停止时误算跨重启时长。
    for steam_id in steam_list:
        if steam_list[steam_id] and steam_list[steam_id]["time"] != 0:
            steam_list[steam_id]["time"] = -1  # -1 为特殊时间用来判断是否重启

        # 修复用户id异常
        if isinstance(steam_list[steam_id], list) and steam_list[steam_id] == [-1]:
            steam_name: str = ""
            try:
                async with http_client() as client:
                    url = f"https://{get_steam_api_domain()}/ISteamUser/GetPlayerSummaries/v0002/?key=" + get_steam_key() + "&steamids=" + steam_id
                    res = SafeResponse(await client.request(Request("GET", url, timeout=30)))
                    if res.status_code != 200:
                        logger.warning(
                            f"Steam id: {steam_id} 修复失败，下次重启时重试。失败原因 http状态码不为200: {res.status_code}")
                        continue
                    if json.loads(res.text)["response"]["players"] == []:
                        logger.warning(f"Steam id: {steam_id} 修复失败，下次重启时重试。失败原因 获取到的用户信息为空")
                        continue
                    steam_name = json.loads(res.text)["response"]["players"][0]['personaname']
            except Exception as e:
                logger.warning(f"Steam id: {steam_id} 修复失败，下次重启时重试。失败原因 : {e.args}")
                continue
            steam_list[steam_id] = UserData(time=0, game_name="", nickname=steam_name, game_id="")
            logger.debug(f"Steam id: {steam_id},name: {steam_name} 异常，修复成功")

    # 保持旧版重启后的时长语义：重启前已处理的游戏在停止时不猜测时长。
    for group_reported in reported_steam_state.values():
        for user_data in group_reported.values():
            if user_data.get("game_name") and user_data.get("time") != 0:
                user_data["time"] = -1


async def get_status(client: HTTPClientSession, steam_id_to_groups: Dict[str, List[str]],
                     steam_list: Dict[str, UserData], steam_id: str):
    global exclude_game
    res = None
    try:
        url = f"https://{get_steam_api_domain()}/ISteamUser/GetPlayerSummaries/v0002/?key=" + get_steam_key() + "&steamids=" + steam_id

        res = SafeResponse(await client.request(Request("GET", url, timeout=30)))
        if res.status_code != 200:
            logger.trace(f"steam id:{steam_id} 查询状态不是200，{res.status_code} {res.text}")
            return

        players = json.loads(res.text)["response"]["players"]
        if not players:
            logger.trace(f"steam id:{steam_id} 查询成功但用户信息为空")
            return
        res_info = players[0]
        nickname = res_info["personaname"]
        previous_raw = steam_list[steam_id]
        is_playing = "gameextrainfo" in res_info
        timestamp = int(time.time() / 60)

        if is_playing:
            game_name = await gameid_to_name(res_info["gameid"], res_info["gameextrainfo"])
            game_name = game_name or res_info["gameextrainfo"]
            game_id = str(res_info.get("gameid", ""))
            same_raw_game = game_id == str(previous_raw.get("game_id", "")) and bool(previous_raw.get("game_name"))
            raw_time = previous_raw["time"] if same_raw_game else timestamp
            steam_list[steam_id] = UserData(time=raw_time, game_name=game_name, nickname=nickname, game_id=game_id)
        else:
            game_name = ""
            game_id = ""
            steam_list[steam_id] = UserData(time=timestamp, game_name="", nickname=nickname, game_id="")

        # steam_list 是最近一次 Steam 原始观测；真正的按群消息处理进度以下面状态为准。
        for group_id in steam_id_to_groups[steam_id]:
            group_id = str(group_id)
            # steam_id_to_groups 是本轮开始时的快照。绑定/解绑命令可能在网络
            # 请求或卡片渲染期间完成，发送前必须重新核验，不能向已解绑的群发送。
            current_group = group_list.get(group_id)
            if (
                not current_group
                or not current_group.get("status", False)
                or steam_id not in current_group.get("user_list", [])
            ):
                logger.trace(f"Steam 状态任务已过期，跳过发送 steam_id:{steam_id} 群:{group_id}")
                continue
            async with get_delivery_group_lock(group_id):
                current_reported = reported_steam_state.get(group_id, {}).get(steam_id)
                delivery_generation = get_delivery_group_generation(group_id)
                if not _delivery_is_current(
                    group_id, steam_id, current_reported, delivery_generation
                ):
                    continue
                reported = current_reported
            previous_stop_count = pending_stop_counts.get(group_id, {}).get(steam_id, 0)
            transition = decide_status_transition(
                is_playing=is_playing,
                game_name=game_name,
                game_id=game_id,
                reported=reported,
                excluded_games=exclude_game.get(group_id, []),
                pending_stop_count=previous_stop_count,
                stop_confirmations=config_steam.steam_stop_confirmations,
            )
            if transition.pending_stop_count:
                pending_stop_counts.setdefault(group_id, {})[steam_id] = transition.pending_stop_count
            else:
                stop_group = pending_stop_counts.get(group_id, {})
                stop_group.pop(steam_id, None)
                if not stop_group:
                    pending_stop_counts.pop(group_id, None)

            handled_state = UserData(time=timestamp, game_name=game_name, nickname=nickname, game_id=game_id)
            if transition.clear_handled:
                async with get_delivery_group_lock(group_id):
                    if not _delivery_is_current(group_id, steam_id, reported, delivery_generation):
                        continue
                    if not _persist_group_event(group_id, steam_id, "stop", handled_state):
                        continue
                logger.debug(
                    f"Steam 屏蔽游戏停止，已清理已处理状态 steam_id:{steam_id} "
                    f"群:{group_id} 游戏:{reported['game_name']}"
                )
                continue
            if not transition.action_type:
                if transition.reason == "await_stop_confirmation":
                    logger.trace(
                        f"Steam 无游戏等待确认 steam_id:{steam_id} 群:{group_id} "
                        f"{transition.pending_stop_count}/{config_steam.steam_stop_confirmations}"
                    )
                else:
                    logger.trace(
                        f"Steam 状态无需处理 steam_id:{steam_id} 群:{group_id} "
                        f"reason:{transition.reason}"
                    )
                continue

            action_type = transition.action_type
            display_game_name = reported["game_name"] if action_type == "stop" else game_name
            display_game_id = reported.get("game_id", "") if action_type == "stop" else game_id
            reported_time = reported.get("time", -1) if reported else -1
            if action_type == "stop":
                if reported_time == -1:
                    action_text = "结束了游戏"
                else:
                    duration = timestamp - reported_time
                    duration_text = format_playtime_duration(duration) if config_steam.steam_pretty_stop_duration else f"{duration} 分钟"
                    action_text = f"玩了 {duration_text} 后停止"
            else:
                action_text = "开始玩"
            reported_snapshot = dict(reported) if reported else None
            try:
                target, bot = await get_group_target_bot(group_id)
            except Exception as target_error:
                async with get_delivery_group_lock(group_id):
                    if not _delivery_is_current(group_id, steam_id, reported_snapshot, delivery_generation):
                        logger.trace(f"Steam 群状态已变化，取消异常事件 steam_id:{steam_id} 群:{group_id}")
                        continue
                    if not _persist_group_event(group_id, steam_id, action_type, handled_state):
                        continue
                logger.warning(
                    f"Steam 状态选择群 bot 失败，事件已消费且不重试 steam_id:{steam_id} "
                    f"群:{group_id} action:{action_type}: {target_error}"
                )
                continue
            if not target:
                async with get_delivery_group_lock(group_id):
                    if not _delivery_is_current(group_id, steam_id, reported_snapshot, delivery_generation):
                        logger.trace(f"Steam 群状态已变化，取消失联事件 steam_id:{steam_id} 群:{group_id}")
                        continue
                    if not _persist_group_event(group_id, steam_id, action_type, handled_state):
                        continue
                logger.debug(f"Steam 群不可达，状态事件已消费 steam_id:{steam_id} 群:{group_id} action:{action_type}")
                try:
                    await test_group_active(group_id)
                except Exception as active_error:
                    logger.warning(f"Steam 失联群状态更新失败 群:{group_id}: {active_error}")
                continue

            group_data = group_list.get(group_id, {})
            use_image = group_data.get("stop_image", False) if action_type == "stop" else group_data.get("image", True)
            msg_to_send = None
            if use_image:
                try:
                    avatar_url = await resolve_avatar_url(steam_id, res_info.get("avatarfull", ""))
                    use_background = group_data.get("stop_image_background", False) if action_type == "stop" else group_data.get("image_background", True)
                    avatar_grayscale = action_type == "stop" and group_data.get("stop_image_grayscale", False)
                    background_grayscale = action_type == "stop" and group_data.get("stop_image_background_grayscale", False)
                    background_url = await resolve_steam_game_background_url(display_game_id) if use_background and display_game_id else ""
                    logger.trace(f"Steam 状态卡片渲染 steam_id:{steam_id} 群:{group_id} action:{action_type} appid:{display_game_id}")
                    card = await render_dynamic_steam_card(
                        avatar_url=avatar_url, player_name=nickname, game_name=display_game_name,
                        action_text=action_text, background_url=background_url,
                        avatar_grayscale=avatar_grayscale, background_grayscale=background_grayscale,
                        stopped=action_type == "stop",
                    )
                    if not card:
                        card = await render_steam_card(
                            avatar_url=avatar_url, player_name=nickname, game_name=display_game_name,
                            action_text=action_text, background_url=background_url,
                            avatar_grayscale=avatar_grayscale, background_grayscale=background_grayscale,
                            stopped=action_type == "stop",
                        )
                    if card:
                        msg_to_send = UniMessage.image(raw=card)
                except Exception as card_e:
                    logger.warning(f"Steam 状态卡片渲染失败 steam_id:{steam_id} 群:{group_id}: {card_e}")

            if not msg_to_send:
                tone = config_steam.steam_tail_tone
                if action_type == "start":
                    msg_to_send = UniMessage(f"{nickname} 开始玩 {display_game_name}{tone} 。")
                elif action_type == "switch":
                    msg_to_send = UniMessage(f"{nickname} 又开始玩 {display_game_name}{tone} 。")
                elif reported_time == -1:
                    msg_to_send = UniMessage(f"{nickname} 不再玩 {display_game_name} 。但{random.choice(bot_name)}忘了，不记得玩了多久了{tone}。")
                else:
                    duration = timestamp - reported_time
                    duration_text = format_playtime_duration(duration) if config_steam.steam_pretty_stop_duration else f"{duration} 分钟"
                    msg_to_send = UniMessage(f"{nickname} 玩了 {duration_text} {display_game_name} 后不玩了{tone}。")

            # 同一状态事件只尝试一次：先原子落盘为“已处理”，再调用适配器发送。
            # 适配器明确拒绝、网络未知和取消均不回滚，避免禁言、风控时反复触发风险。
            async with get_delivery_group_lock(group_id):
                if not _delivery_is_current(group_id, steam_id, reported_snapshot, delivery_generation):
                    logger.trace(f"Steam 群状态已变化，取消发送 steam_id:{steam_id} 群:{group_id}")
                    continue
                if is_playing and game_name in exclude_game.get(group_id, []):
                    logger.trace(f"Steam 发送前命中屏蔽游戏，取消发送 steam_id:{steam_id} 群:{group_id}")
                    continue
                logger.debug(
                    f"Steam 状态事件准备落盘并发送 steam_id:{steam_id} 群:{group_id} "
                    f"action:{action_type} appid:{display_game_id}"
                )
                try:
                    await persist_then_send(
                        reported_steam_state,
                        group_id,
                        steam_id,
                        action_type,
                        handled_state,
                        lambda: save_reported_steam_state(reported_steam_state),
                        lambda: msg_to_send.send(target=target, bot=bot),
                    )
                except HandledStatePersistenceError as persist_error:
                    logger.error(
                        "Steam 状态事件落盘失败，未发送且允许后续重新处理 "
                        f"steam_id:{steam_id} 群:{group_id} action:{action_type}: "
                        f"{persist_error.__cause__}"
                    )
                    continue
                except asyncio.CancelledError:
                    logger.warning(
                        f"Steam 状态发送等待回包时被取消，事件不重试 steam_id:{steam_id} "
                        f"群:{group_id} action:{action_type}"
                    )
                    raise
                except ActionFailed as send_e:
                    logger.warning(
                        f"Steam 状态被适配器明确拒绝，事件不重试 steam_id:{steam_id} "
                        f"群:{group_id} action:{action_type}: {send_e}"
                    )
                    continue
                except NetworkError as send_e:
                    logger.warning(
                        f"Steam 状态发送结果不确定，事件不重试 steam_id:{steam_id} "
                        f"群:{group_id} action:{action_type}: {send_e}"
                    )
                    continue
                except Exception as send_e:
                    logger.warning(
                        f"Steam 状态发送失败，事件不重试 steam_id:{steam_id} "
                        f"群:{group_id} action:{action_type}: {send_e}"
                    )
                    continue
                logger.debug(f"Steam 状态发送完成 steam_id:{steam_id} 群:{group_id} action:{action_type} appid:{display_game_id}")
    except Exception as e:
        a, b, exc_traceback = sys.exc_info()
        logger.trace(
            f"steam id:{steam_id} 查询状态失败,line: {exc_traceback.tb_lineno if exc_traceback else ''}，{e.args} \n{res.text if res else None}\n{a}\n{b}")


@scheduler.scheduled_job("interval", minutes=config_steam.steam_interval, id="steam",
                         misfire_grace_time=(config_steam.steam_interval * 60 - 1))
async def now_steam():
    if config_steam.steam_web_key:
        global steam_list
        logger.debug("steam准备开始生成查询字典")
        task_list = []
        steam_id_to_groups = build_steam_id_to_groups(group_list)
        logger.debug("steam生成查询字典完成，准备添加任务")
        async with http_client() as client:
            semaphore = asyncio.Semaphore(config_steam.steam_status_query_concurrency)

            async def query_status(steam_id: str):
                async with semaphore:
                    await get_status(client, steam_id_to_groups, steam_list, steam_id)

            for steam_id in steam_id_to_groups:
                task_list.append(query_status(steam_id))
            try:
                logger.debug("steam添加任务完成，准备运行并等待任务")
                # 状态卡片（首次背景图/动态帧）可能需要数十秒。旧的 ``-20``
                # 在一分钟轮询时只给整批任务 40 秒，会在 send_msg 已经发出、
                # 仍等待适配器回包时取消协程，从而造成一次真实的漏播报。
                # 仍预留 5 秒给下一轮调度，避免任务长期重叠。
                query_timeout = max(1, config_steam.steam_interval * 60 - 5)
                await asyncio.wait_for(asyncio.gather(*task_list), timeout=query_timeout)
                logger.debug("steam自动查询任务完成")
            except asyncio.TimeoutError:
                logger.warning(
                    f"Steam 自动查询批次超时（{query_timeout} 秒），未完成任务已取消；"
                    "请结合‘卡片渲染’和‘发送等待回包’日志排查"
                )
            except Exception as e:
                logger.debug(f"steam新异常:{e.args}")
            finally:
                atomic_write_json(new_file_steam, steam_list)
                save_reported_steam_state(reported_steam_state)
                logger.debug("steam finally保存完成")


@scheduler.scheduled_job(
    "interval",
    minutes=config_steam.steam_owned_game_interval,
    id="steam_owned_games",
    misfire_grace_time=(config_steam.steam_owned_game_interval * 60 - 1),
)
async def now_steam_owned_games():
    await run_owned_games_check()


steam_command_alc = Alconna(
    "steam",
    Option("add", Args["id", str], alias=["绑定", "添加", ".add"], separators="", compact=True),
    Option("del", Args["id", str], alias=["解绑", "删除", ".del"], separators="", compact=True),
    Option("屏蔽", Args["game", AllParam(str)], separators="", compact=True),
    Option("恢复", Args["game", AllParam(str)], separators="", compact=True),
    Option("打折订阅", Args["game", AllParam(str)], alias=["折扣订阅", "促销订阅", "低价订阅"], separators="",
           compact=True),
    Option("打折退订", Args["game", AllParam(str)], alias=["折扣退订", "促销退订", "低价退订"], separators="",
           compact=True),
    Option("排除列表", separators="", compact=True),
    Option("list", alias=["列表", "绑定列表", "播报列表"], separators="", compact=True),
    Option("播报", Args["status", str], separators="", compact=True),
    Option("图片播报", Args["status", str], separators="", compact=True),
    Option("图片背景", Args["status", str], alias=["启动图片背景", "开始图片背景", "卡片背景"], separators="", compact=True),
    Option("结束图片播报", Args["status", str], separators="", compact=True),
    Option("结束图片背景", Args["status", str], separators="", compact=True),
    Option("结束头像黑白", Args["status", str], alias=["结束图片黑白"], separators="", compact=True),
    Option("结束背景黑白", Args["status", str], alias=["结束图片背景黑白"], separators="", compact=True),
    Option("入库播报", Args["status", str], alias=["游戏库播报"], separators="", compact=True),
    Option("墙", Args["user", str], separators="", compact=True),
    Option("喜加一", Args["action", Optional[Literal["订阅", "退订"]]], separators="", compact=True),
    Option("帮助", alias=["help", "菜单", ".help"], separators="", compact=True),
    Option("失联群列表", separators="", compact=True),
    Option("失联群清理", separators="", compact=True),
    separators="",
    meta=CommandMeta(compact=True)
)
# 兼容用户习惯输入的首字母大写命令，例如 Steam帮助、Steam绑定。
steam_command_alc.shortcut("Steam", {"command": "steam {args}", "fuzzy": True})
steam_cmd = on_alconna(steam_command_alc, priority=config_steam.steam_command_priority, rule=no_private_rule)


async def ensure_group_data(target: MsgTarget) -> str:
    """补齐群配置；命令触达失联群时在持锁状态下恢复并立即持久化。"""
    group_id = str(target.id)
    adapter = to_enum(target.adapter).value if target.adapter else ""
    changed = False
    recovered = False
    async with get_delivery_group_lock(group_id):
        if group_id not in group_list:
            group_list[group_id] = create_group_data(adapter=adapter)
            changed = True
        else:
            group_data = group_list[group_id]
            defaults = create_group_data(adapter=group_data.get("adapter") or adapter)
            for key, value in defaults.items():
                if key not in group_data:
                    group_data[key] = value
                    changed = True
        if group_id not in exclude_game:
            exclude_game[group_id] = list(exclude_game_default)
            changed = True
        if group_id in inactive_groups:
            inactive_groups.remove(group_id)
            pending_stop_counts.pop(group_id, None)
            bump_delivery_group_generation(group_id)
            changed = True
            recovered = True
        if changed:
            save_data()
    if recovered:
        logger.info(f"群组id： {group_id} 收到 Steam 命令，已从失联群组列表恢复")
    return group_id


@steam_cmd.assign("add")
async def steam_bind_handle(target: MsgTarget, matcher: Matcher, id: Match[str]):
    steam_id = str(id.result)
    if len(steam_id) != 17:
        try:
            steam_id = int(steam_id)
            steam_id += 76561197960265728
            steam_id = str(steam_id)
        except Exception as e:
            logger.debug(f"Steam 绑定出错，输入值：{steam_id}，错误：{e.args}")
            await matcher.finish(f"Steam ID格式错误{config_steam.steam_tail_tone}")
    global steam_list, group_list, exclude_game, reported_steam_state
    group_id = await ensure_group_data(target)

    if steam_id in group_list[group_id]["user_list"]:
        await matcher.finish(f"已经绑定过了{config_steam.steam_tail_tone}")

    steam_name: str = ""
    avatar_url: str = ""

    try:
        async with http_client() as client:
            url = f"https://{get_steam_api_domain()}/ISteamUser/GetPlayerSummaries/v0002/?key=" + get_steam_key() + "&steamids=" + steam_id
            res = SafeResponse(await client.request(Request("GET", url, timeout=30)))

            if res.status_code != 200:
                logger.debug(f"{steam_id} 绑定失败，{res.status_code} {res.text}")
                await matcher.finish(f"{steam_id} 绑定失败，{res.status_code} {res.text}")

            players = json.loads(res.text)["response"]["players"]
            if players == []:
                logger.debug(f"{steam_id} 绑定失败，查无此人，请检查输入的id")
                await matcher.finish(f"{steam_id} 绑定失败，查无此人，请检查输入的id{config_steam.steam_tail_tone}")

            steam_name = players[0]["personaname"]
            avatar_url = normalize_steam_avatar_url(players[0].get("avatarfull", ""))

    except MatcherException:
        raise
    except Exception as e:
        logger.debug(f"{steam_id} 绑定失败，{e.args}")
        await matcher.finish(f"{steam_id} 绑定失败{config_steam.steam_tail_tone}，{e}")

    # 新群绑定不应重置其他群正在使用的原始状态；本群已处理状态则必须清空。
    async with get_delivery_group_lock(group_id):
        if steam_id not in steam_list:
            steam_list[steam_id] = UserData(time=0, game_name="", nickname=steam_name, game_id="")
        else:
            steam_list[steam_id]["nickname"] = steam_name
        reported_steam_state.setdefault(group_id, {}).pop(steam_id, None)
        pending_stop_counts.get(group_id, {}).pop(steam_id, None)
        group_list[group_id]["user_list"].append(steam_id)
        bump_delivery_group_generation(group_id)
    if group_list[group_id].get("owned_game", False):
        await initialize_owned_games_baseline([steam_id])
    save_data()

    # 渲染发送绑定成功
    bind_img = None
    if avatar_url:
        bind_img = await render_bind_card(avatar_url, steam_name, steam_id)

    if bind_img:
        await matcher.finish(UniMessage.image(raw=bind_img))
    else:
        # 降级：有问题则发送纯文本
        await matcher.finish(
            f"Steam ID：{steam_id}\nSteam ID64：{steam_id}\nSteam Name：{steam_name}\n 绑定成功了{config_steam.steam_tail_tone}")


@steam_cmd.assign("del")
async def steam_del_handle(target: MsgTarget, matcher: Matcher, id: Match[str]):
    steam_id = str(id.result)
    if len(steam_id) != 17:
        try:
            steam_id = int(steam_id)
            steam_id += 76561197960265728
            steam_id = str(steam_id)
        except Exception as e:
            logger.debug(f"Steam 绑定出错，输入值：{steam_id}，错误：{e.args}")
            await matcher.finish(f"Steam ID格式错误{config_steam.steam_tail_tone}")
    steam_name: str = ""
    global group_list, reported_steam_state

    group_id = str(target.id)
    if group_id not in group_list:
        await matcher.finish(f"本群不存在 Steam 绑定记录{config_steam.steam_tail_tone}")
    await ensure_group_data(target)

    if steam_id not in group_list[group_id]["user_list"]:
        await matcher.finish(f"本群尚未绑定该 Steam ID{config_steam.steam_tail_tone}")
    steam_name = steam_list[steam_id]["nickname"]

    async with get_delivery_group_lock(group_id):
        try:
            group_list[group_id]["user_list"].remove(steam_id)
        except Exception as e:
            logger.debug(f"删除steam id 失败，输入值：{steam_id}，错误：{e.args}")
            await matcher.finish(f"没有找到 Steam ID：{steam_id}{config_steam.steam_tail_tone}")
        group_reported = reported_steam_state.get(group_id)
        if group_reported:
            group_reported.pop(steam_id, None)
            if not group_reported:
                reported_steam_state.pop(group_id, None)
        pending_stop_counts.get(group_id, {}).pop(steam_id, None)
        bump_delivery_group_generation(group_id)
    save_data()
    await matcher.finish(f"Steam ID：{steam_id}\nSteam Name：{steam_name}\n 删除成功了{config_steam.steam_tail_tone}")


steam_cmd_scr = steam_cmd.dispatch("屏蔽")
steam_cmd_rec = steam_cmd.dispatch("恢复")


@steam_cmd_scr.handle()
@steam_cmd_rec.handle()
async def steam_clude_handle(target: MsgTarget, arp: Arparma, matcher: Matcher, game: Match[str]):
    global group_list, exclude_game
    handle = next(iter(arp.components))
    game_name = str(game.result)
    group_id = await ensure_group_data(target)
    if game_name == "":
        await matcher.finish(f"请输入要{handle}的完整游戏名称{config_steam.steam_tail_tone}")
    elif handle == "屏蔽":
        if game_name in exclude_game[group_id]:
            await matcher.finish(f"{game_name} 已经被屏蔽过了{config_steam.steam_tail_tone}")
        exclude_game[group_id].append(game_name)
    elif handle == "恢复":
        if game_name not in exclude_game[group_id]:
            await matcher.finish(f"{game_name} 没有被屏蔽过{config_steam.steam_tail_tone}")
        exclude_game[group_id].remove(game_name)
    atomic_write_json(exclude_game_file, exclude_game)
    await matcher.finish(f"{handle}游戏 {game_name} 完成{config_steam.steam_tail_tone}")


@steam_cmd.assign("排除列表")
async def steam_exclude_list_handle(target: MsgTarget):
    global group_list, exclude_game
    group_id = await ensure_group_data(target)

    nodes = [
        CustomNode(
            uid=str(target.self_id),
            name=str(index + 1),
            content=UniMessage.text(game_name)
        )
        for index, game_name in enumerate(exclude_game[group_id])
    ]
    await UniMessage(Reference(nodes=nodes)).send()


@steam_cmd.assign("list")
async def steam_bind_list_handle(target: MsgTarget):
    group_id = await ensure_group_data(target)
    try:
        nodes = [
            CustomNode(
                uid=str(target.self_id),
                name=str(index + 1),
                content=UniMessage.text(
                    f"Steam ID：{steam_id}\n昵称：{steam_list[steam_id]['nickname']}\n{'正在玩：' + steam_list[steam_id]['game_name'] if steam_list[steam_id]['game_name'] != '' else ''}")
            )
            for index, steam_id in enumerate(group_list[group_id]["user_list"])
        ]
        await UniMessage(Reference(nodes=nodes)).send()
    except Exception as e:
        logger.debug(f"Steam 列表合并消息发送出错，错误：{e.args}")
        await UniMessage(f"本群尚未绑定任何 Steam ID，请先绑定{config_steam.steam_tail_tone}。").send()


@steam_cmd.assign("播报")
async def steam_on_handle(target: MsgTarget, status: Match[str]):
    if str(status.result) not in ("开启", "关闭"):
        await UniMessage(f"仅允许设置播报开启或关闭{config_steam.steam_tail_tone}").send(reply_to=True)
    else:
        global group_list
        group_id = await ensure_group_data(target)
        async with get_delivery_group_lock(group_id):
            group_list[group_id]["status"] = True if str(status.result) == "开启" else False
            if str(status.result) == "关闭":
                pending_stop_counts.pop(group_id, None)
            bump_delivery_group_generation(group_id)
        save_data()
        await UniMessage(f"Steam 播报已{str(status.result)}{config_steam.steam_tail_tone}").send(reply_to=True)


@steam_cmd.assign("图片播报")
async def steam_image_handle(target: MsgTarget, status: Match[str]):
    if str(status.result) not in ("开启", "关闭"):
        await UniMessage(f"仅允许设置图片播报开启或关闭{config_steam.steam_tail_tone}").send(reply_to=True)
    else:
        global group_list
        group_id = await ensure_group_data(target)
        group_list[group_id]["image"] = True if str(status.result) == "开启" else False
        save_data()
        await UniMessage(f"Steam 图片播报已{str(status.result)}{config_steam.steam_tail_tone}").send(reply_to=True)


@steam_cmd.assign("图片背景")
async def steam_image_background_handle(target: MsgTarget, status: Match[str]):
    if str(status.result) not in ("开启", "关闭"):
        await UniMessage(f"仅允许设置图片背景开启或关闭{config_steam.steam_tail_tone}").send(reply_to=True)
    else:
        global group_list
        group_id = await ensure_group_data(target)
        group_list[group_id]["image_background"] = True if str(status.result) == "开启" else False
        save_data()
        await UniMessage(f"Steam 图片背景已{str(status.result)}{config_steam.steam_tail_tone}").send(reply_to=True)


@steam_cmd.assign("结束图片播报")
async def steam_stop_image_handle(target: MsgTarget, status: Match[str]):
    if str(status.result) not in ("开启", "关闭"):
        await UniMessage(f"仅允许设置结束图片播报开启或关闭{config_steam.steam_tail_tone}").send(reply_to=True)
    else:
        global group_list
        group_id = await ensure_group_data(target)
        group_list[group_id]["stop_image"] = True if str(status.result) == "开启" else False
        save_data()
        await UniMessage(f"Steam 结束图片播报已{str(status.result)}{config_steam.steam_tail_tone}").send(reply_to=True)


@steam_cmd.assign("结束图片背景")
async def steam_stop_image_background_handle(target: MsgTarget, status: Match[str]):
    if str(status.result) not in ("开启", "关闭"):
        await UniMessage(f"仅允许设置结束图片背景开启或关闭{config_steam.steam_tail_tone}").send(reply_to=True)
    else:
        global group_list
        group_id = await ensure_group_data(target)
        group_list[group_id]["stop_image_background"] = True if str(status.result) == "开启" else False
        save_data()
        await UniMessage(f"Steam 结束图片背景已{str(status.result)}{config_steam.steam_tail_tone}").send(reply_to=True)


@steam_cmd.assign("结束头像黑白")
async def steam_stop_image_grayscale_handle(target: MsgTarget, status: Match[str]):
    if str(status.result) not in ("开启", "关闭"):
        await UniMessage(f"仅允许设置结束头像黑白开启或关闭{config_steam.steam_tail_tone}").send(reply_to=True)
    else:
        global group_list
        group_id = await ensure_group_data(target)
        group_list[group_id]["stop_image_grayscale"] = True if str(status.result) == "开启" else False
        save_data()
        await UniMessage(f"Steam 结束头像黑白已{str(status.result)}{config_steam.steam_tail_tone}").send(reply_to=True)


@steam_cmd.assign("结束背景黑白")
async def steam_stop_background_grayscale_handle(target: MsgTarget, status: Match[str]):
    if str(status.result) not in ("开启", "关闭"):
        await UniMessage(f"仅允许设置结束背景黑白开启或关闭{config_steam.steam_tail_tone}").send(reply_to=True)
    else:
        global group_list
        group_id = await ensure_group_data(target)
        group_list[group_id]["stop_image_background_grayscale"] = True if str(status.result) == "开启" else False
        save_data()
        await UniMessage(f"Steam 结束背景黑白已{str(status.result)}{config_steam.steam_tail_tone}").send(reply_to=True)


@steam_cmd.assign("入库播报")
async def steam_owned_game_handle(target: MsgTarget, status: Match[str]):
    if str(status.result) not in ("开启", "关闭"):
        await UniMessage(f"仅允许设置入库播报开启或关闭{config_steam.steam_tail_tone}").send(reply_to=True)
    else:
        global group_list
        group_id = await ensure_group_data(target)
        owned_game_enabled = str(status.result) == "开启"
        group_list[group_id]["owned_game"] = owned_game_enabled
        created = 0
        failed_steam_ids = []
        if owned_game_enabled:
            created, failed_steam_ids = await initialize_owned_games_baseline(group_list[group_id]["user_list"])
        save_data()
        baseline_text = f"，已建立 {created} 个游戏库基准"
        if failed_steam_ids:
            failed_names = [
                steam_list.get(steam_id, {}).get("nickname") or steam_id
                for steam_id in failed_steam_ids[:3]
            ]
            baseline_text += f"，{len(failed_steam_ids)} 个失败：{'、'.join(failed_names)}"
            if len(failed_steam_ids) > 3:
                baseline_text += " 等"
        await UniMessage(f"Steam 入库播报已{str(status.result)}{baseline_text if owned_game_enabled else ''}{config_steam.steam_tail_tone}").send(reply_to=True)


@steam_cmd.assign("喜加一")
async def steam_free_handle(target: MsgTarget, matcher: Matcher, action: Match[str]):
    group_id = await ensure_group_data(target)
    if action.result:
        group_list[group_id]["xijiayi"] = True if str(action.result) == "订阅" else False
        save_data()
        await matcher.finish(f"steam 喜加一 已{str(action.result)}{config_steam.steam_tail_tone}")
    res = await get_free_games_info(target)
    if res:
        await matcher.finish(res)


@steam_cmd.assign("帮助")
async def steam_help_handle(matcher: Matcher):
    help_img = await render_help_card()
    if help_img:
        await matcher.finish(UniMessage.image(raw=help_img))

    await matcher.finish(
        "Steam 游戏状态帮助\n"
        "常用：steam绑定、steam解绑、steam列表\n"
        "播报：steam播报 开启/关闭、steam图片播报 开启/关闭、steam结束图片播报 开启/关闭\n"
        "屏蔽：steam屏蔽 游戏名、steam恢复 游戏名、steam排除列表\n"
        "入库：steam入库播报 开启/关闭\n"
        "订阅：steam喜加一、steam喜加一 订阅/退订、steam打折订阅 appid\n"
        "工具：steam墙 7656..."
    )


@steam_cmd.assign("墙")
async def steam_wall(matcher: Matcher, user: Match[str]):
    try:
        screenshot = await get_steam_playtime(str(user.result))
        await UniMessage.image(raw=screenshot).send()
    except SteamPlaytimeUnavailableError as e:
        logger.info(f"获取 Steam 游戏时长拼图不可用：{e}")
        await UniMessage.text(f"获取 Steam 游戏时长拼图失败{config_steam.steam_tail_tone}：{e}").send()
    except Exception as e:
        logger.warning(f"获取 Steam 游戏时长拼图出错：{e.args}")
        await UniMessage.text(f"获取 Steam 游戏时长拼图出错{config_steam.steam_tail_tone}，请稍后再试或检查日志").send()
    await matcher.finish()


@steam_cmd.assign("打折订阅")
async def steam_discounted_games_bind(target: MsgTarget, matcher: Matcher, game: Match[str]):
    global game_discounted_subscribe
    group_id = await ensure_group_data(target)
    game_id = str(game.result)
    if group_id in game_discounted_subscribe.get(game_id, []):
        await matcher.finish(f"已订阅过 {config_steam.steam_tail_tone}", reply_message=True)
    try:
        info = await get_discounted_games_info(target, game_id)
    except Exception as e:
        await matcher.finish(f"订阅出错了{config_steam.steam_tail_tone}, {e.args}")

    if not info:
        await matcher.finish(f"免费或未推出游戏不能订阅{config_steam.steam_tail_tone}", reply_message=True)
    else:
        game_discounted_subscribe.setdefault(game_id, []).append(group_id)
        save_data()
        if isinstance(info, str):
            await matcher.finish(f"已订阅折扣提醒{config_steam.steam_tail_tone},\n{info}", reply_message=True)
        else:
            await matcher.finish(f"该游戏正在打折{config_steam.steam_tail_tone}", reply_message=True)


@steam_cmd.assign("打折退订")
async def steam_discounted_games_del(target: MsgTarget, matcher: Matcher, game: Match[str]):
    global game_discounted_subscribe
    group_id = await ensure_group_data(target)
    game_id = str(game.result)
    if game_id not in game_discounted_subscribe or group_id not in game_discounted_subscribe[game_id]:
        await matcher.finish(f"未订阅过 {config_steam.steam_tail_tone}", reply_message=True)
    game_discounted_subscribe[game_id].remove(group_id)
    if not game_discounted_subscribe[game_id]:
        del game_discounted_subscribe[game_id]
        if game_id in game_discounted_cache:
            game_discounted_cache.remove(game_id)
    save_data()
    await matcher.finish(f"已退订折扣提醒{config_steam.steam_tail_tone}", reply_message=True)


steam_admin_cmd = on_alconna(
    steam_command_alc,
    priority=config_steam.steam_command_priority,
    permission=SUPERUSER
)


@steam_admin_cmd.assign("失联群列表")
async def steam_inactive_groups_handle(target: MsgTarget):
    unimsg = await get_inactive_groups_list(target)
    await unimsg.send()


@steam_admin_cmd.assign("失联群清理")
async def steam_clear_inactive_groups_handle(target: MsgTarget):
    unimsg = await clear_inactive_groups_list(target)
    await unimsg.send()


@driver.on_startup
async def _init_steam_subscribe_jobs():
    times = config_steam.steam_subscribe_time

    for idx, t in enumerate(times):
        hour_str, minute_str = t.split(":")
        hour = int(hour_str)
        minute = int(minute_str)

        job_xijiayi_id = f"steam_xijiayi_subscribe_{idx}"
        logger.info(f"注册 steam 订阅定时任务: {job_xijiayi_id} -> {hour:02d}:{minute:02d}")
        job_discounted_id = f"steam_discounted_subscribe_{idx}"
        logger.info(f"注册 steam 订阅定时任务: {job_discounted_id} -> {hour:02d}:{minute:02d}")

        scheduler.add_job(
            steam_subscribe,
            "cron",
            hour=hour,
            minute=minute,
            id=job_xijiayi_id,
            replace_existing=True,
        )
        scheduler.add_job(
            sbeam_subscribe,
            "cron",
            hour=hour,
            minute=minute,
            id=job_discounted_id,
            replace_existing=True,
        )


async def steam_subscribe():
    logger.info("steam定时尝试获取推送喜加一")
    await get_free_games_info()
    logger.info("steam定时尝试获取推送喜加一结束")


async def sbeam_subscribe():
    logger.info("steam定时尝试获取推送打折")
    await get_discounted_games_info()
    logger.info("steam定时尝试获取推送打折结束")
