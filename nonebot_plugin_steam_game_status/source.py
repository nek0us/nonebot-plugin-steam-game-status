import asyncio
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional
from nonebot import require,logger
from .model import GroupData, GroupData2, GroupData3, GroupDataNew, UserData
from .status_state import build_conservative_reported_state, sanitize_reported_state
require("nonebot_plugin_localstore")
import nonebot_plugin_localstore as store  # noqa: E402

nb_project = os.path.basename(os.getcwd())

plugin_data_dir: Path = store.get_data_dir("nonebot_plugin_steam_game_status")
data_dir = plugin_data_dir / nb_project

# Ensure the new directories exist
data_dir.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, data: object) -> None:
    """以同目录临时文件替换 JSON，避免进程中断留下半截状态文件。"""
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temp_file:
            temp_name = temp_file.name
            json.dump(data, temp_file)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def load_json_or_recover(path: Path, default: object) -> object:
    """读取 JSON；损坏或顶层结构不符时保留副本并以安全默认值恢复。"""
    try:
        result = json.loads(path.read_text("utf8"))
        if type(result) is not type(default):
            raise ValueError(
                f"顶层类型应为 {type(default).__name__}，实际为 {type(result).__name__}"
            )
        return result
    except (OSError, json.JSONDecodeError, ValueError) as error:
        backup = path.with_name(f"{path.name}.corrupt.{int(time.time())}")
        try:
            if path.exists():
                os.replace(path, backup)
        except OSError:
            logger.warning(f"Steam 状态文件损坏且备份失败: {path}, {error}")
        else:
            logger.warning(f"Steam 状态文件损坏，已隔离并使用默认值恢复: {path}, {error}")
        atomic_write_json(path, default)
        return default


# 同一群的状态发送和群配置变更必须线性化，避免解绑后旧任务重新写回状态。
delivery_group_locks: Dict[str, asyncio.Lock] = {}
delivery_group_generations: Dict[str, int] = {}


def get_delivery_group_lock(group_id: str) -> asyncio.Lock:
    return delivery_group_locks.setdefault(str(group_id), asyncio.Lock())


def get_delivery_group_generation(group_id: str) -> int:
    return delivery_group_generations.get(str(group_id), 0)


def bump_delivery_group_generation(group_id: str) -> int:
    """使已在途的群状态任务失效；调用方需持有该群锁。"""
    group_id = str(group_id)
    next_generation = get_delivery_group_generation(group_id) + 1
    delivery_group_generations[group_id] = next_generation
    return next_generation

# 兼容性检测

# 需要移动的文件夹列表
dirs_to_move = ["steam_user_list.json", "steam_group_list.json"]
if os.name == 'nt':
    incorrect_dir = Path(os.getcwd())
    if incorrect_dir.exists() and incorrect_dir.is_dir():
        for dir_name in dirs_to_move:
            src_dir = incorrect_dir / dir_name
            dest_dir = data_dir / dir_name
            if src_dir.exists() and src_dir.is_dir():
                shutil.move(str(src_dir), str(dest_dir))

# 旧文件路径
old_dirpath = Path() / "data" / "steam_group" / "group_list.json"

# 新文件路径
new_file_steam = data_dir / "steam_user_list.json"
new_file_group = data_dir / "steam_group_list.json"
# 每个群已消费到的 Steam 状态。不能复用 steam_user_list：同一个 SteamID
# 在不同群可能有不同的屏蔽列表、不同的消息处理结果。
reported_steam_state_file = data_dir / "reported_steam_state.json"
reported_steam_state_schema_version = 1
game_cache_file = data_dir / "game_cache.json"
exclude_game_file = data_dir / "exclude_game"
game_free_cache_file = data_dir / "game_free_cache.json"
game_discounted_cache_file = data_dir / "game_discounted_cache.json"
game_discounted_subscribe_file = data_dir / "game_discounted_subscribe.json"
owned_games_file = data_dir / "owned_games.json"
dynamic_card_cache_dir = data_dir / "dynamic_card_cache"
image_resource_cache_dir = data_dir / "image_resource_cache"

dynamic_card_cache_dir.mkdir(parents=True, exist_ok=True)
image_resource_cache_dir.mkdir(parents=True, exist_ok=True)


def save_reported_steam_state(state: Dict[str, Dict[str, UserData]]) -> None:
    """持久化按群已处理状态，并为后续迁移保留显式版本。"""
    atomic_write_json(
        reported_steam_state_file,
        {"schema_version": reported_steam_state_schema_version, "groups": state},
    )


def _isolate_unusable_reported_steam_state(reason: str) -> None:
    backup = reported_steam_state_file.with_name(
        f"{reported_steam_state_file.name}.unsupported.{int(time.time())}"
    )
    try:
        os.replace(reported_steam_state_file, backup)
    except OSError as error:
        logger.warning(
            f"Steam 按群已处理状态{reason}，备份失败: "
            f"{reported_steam_state_file}, {error}"
        )
    else:
        logger.warning(
            f"Steam 按群已处理状态{reason}，已隔离: {backup}"
        )


def load_reported_steam_state() -> Optional[Dict[str, Dict[str, UserData]]]:
    """读取已处理状态；整文件不可用时返回 None，由调用方建立保守基线。"""
    try:
        document = json.loads(reported_steam_state_file.read_text("utf8"))
    except (OSError, json.JSONDecodeError) as error:
        _isolate_unusable_reported_steam_state(f"无法解析: {error}")
        return None
    if not isinstance(document, dict):
        _isolate_unusable_reported_steam_state("顶层不是对象")
        return None

    legacy = "schema_version" not in document
    if "schema_version" not in document:
        groups = document
    elif document.get("schema_version") == reported_steam_state_schema_version:
        groups = document.get("groups")
    else:
        _isolate_unusable_reported_steam_state("版本不支持")
        return None

    try:
        state, invalid_records = sanitize_reported_state(groups)
    except ValueError as error:
        _isolate_unusable_reported_steam_state(f"结构不支持: {error}")
        return None

    for group_id, steam_id in invalid_records:
        logger.warning(
            "Steam 按群已处理状态记录字段无效，已仅丢弃该记录: "
            f"群:{group_id} steam_id:{steam_id}"
        )
    if legacy:
        logger.info("Steam 按群已处理状态升级为带版本格式")
    if legacy or invalid_records:
        save_reported_steam_state(state)
    return state

exclude_game_default = ["Wallpaper Engine：壁纸引擎","虚拟桌宠模拟器","OVR Toolkit","OVR Advanced Settings","OBS Studio","VTube Studio","Live2DViewerEX","Blender","LIV"]

# 判断旧文件存不存在
if not old_dirpath.exists():
    # 不存在，看看新的在不在
    if not new_file_steam.exists():
        # 也不存在，新用户，直接创建
        logger.info("初次启动，创建 steam 缓存文件")
        atomic_write_json(new_file_steam, {})
        atomic_write_json(new_file_group, {})
        atomic_write_json(game_cache_file, {})
        atomic_write_json(exclude_game_file, {})
        atomic_write_json(game_free_cache_file, [])
        atomic_write_json(owned_games_file, {})
    else:
        # 存在，准备好的新用户
        # 看看exclude在不在
        group_tmp: Dict[str, GroupDataNew] = load_json_or_recover(new_file_group, {})  # type: ignore[assignment]
        if not exclude_game_file.exists():
            atomic_write_json(game_cache_file, {})
            if group_tmp == {}:
                atomic_write_json(exclude_game_file, {})
            else:
                exclude_game_tmp = {}
                for group_id in group_tmp:
                    exclude_game_tmp[group_id] = list(exclude_game_default)
                atomic_write_json(exclude_game_file, exclude_game_tmp)
        else:
            exclude_game_tmp: Dict[str, List[str]] = load_json_or_recover(exclude_game_file, {})  # type: ignore[assignment]
            for group_id in group_tmp:
                if group_id not in exclude_game_tmp:
                    exclude_game_tmp[group_id] = list(exclude_game_default)
            atomic_write_json(exclude_game_file, exclude_game_tmp)
        

else:
    # 存在旧文件，看看新的在不在
    if not new_file_steam.exists():
        # 不存在新文件，准备迁移
        
        old_json = load_json_or_recover(old_dirpath, {})
        
        new_json_steam = {}
        new_json_group = {}
        
        if old_json != {}:
            # 不为空，有内容迁移
            logger.info(f"版本更新，steam 数据迁移中：{data_dir}")
            for group_id, data in old_json.items():
                # Update group information in new_json_group
                new_json_group[group_id] = {
                    "status": data["status"] == "on",
                    "user_list": list(data.keys())[1:]  # Skip the "status" key
                }
                
                # Update user information in new_json_steam
                for steam_id, user_data in data.items():
                    if steam_id != "status":  # Skip the "status" entry
                        new_json_steam[steam_id] = user_data
            
            # 写入新文件
            atomic_write_json(new_file_steam, new_json_steam)
            atomic_write_json(new_file_group, new_json_group)
            # 太久远版本没做迁移，懒得适配排除目录和喜加一目录了，直接删除重新旧数据比较快
            logger.success("steam 数据迁移完成")

# 25.08.21 UserData迁移
steam_list_tmp: Dict[str, UserData] = load_json_or_recover(new_file_steam, {})  # type: ignore[assignment]
steam_list_tmp_first_val = next(iter(steam_list_tmp.values()), None)
if steam_list_tmp_first_val is not None and isinstance(steam_list_tmp_first_val, list):
    steam_list_dict = {}
    for steam_id in steam_list_tmp:
        steam_list_dict[steam_id] = UserData(
            time=steam_list_tmp[steam_id][0],
            game_name=steam_list_tmp[steam_id][1],
            nickname=steam_list_tmp[steam_id][2],
            game_id="",
            )
    atomic_write_json(new_file_steam, steam_list_dict)
    logger.success("steam 0.2.0 数据迁移成功")

# 25.09.02 adapter更新
steam_group_25_09_02: Dict[str, GroupData] = load_json_or_recover(new_file_group, {})  # type: ignore[assignment]
value_25_09_02 = next(iter(steam_group_25_09_02.values()), None)
if value_25_09_02:
    if "adapter" not in value_25_09_02:
        steam_group_dict_25_09_02 = {}
        for group_id in steam_group_25_09_02:
            steam_group_dict_25_09_02[group_id] = GroupData2(
                status=steam_group_25_09_02[group_id]["status"],
                user_list=steam_group_25_09_02[group_id]["user_list"],
                adapter="OneBot v11"
            )
        atomic_write_json(new_file_group, steam_group_dict_25_09_02)
        logger.success("steam 0.2.1 25.09.02 adapter更新数据成功")


# 25.09.08 xijiayi更新
steam_group_25_09_08: Dict[str, GroupData2] = load_json_or_recover(new_file_group, {})  # type: ignore[assignment]
value_25_09_08 = next(iter(steam_group_25_09_08.values()), None)
if value_25_09_08:
    if "xijiayi" not in value_25_09_08:
        steam_group_dict_25_09_08 = {}
        for group_id in steam_group_25_09_08:
            steam_group_dict_25_09_08[group_id] = GroupData3(
                status=steam_group_25_09_08[group_id]["status"],
                user_list=steam_group_25_09_08[group_id]["user_list"],
                adapter=steam_group_25_09_08[group_id]["adapter"],
                xijiayi=False
            )
        atomic_write_json(new_file_group, steam_group_dict_25_09_08)
        logger.success("steam 0.2.2 25.09.08 xijiayi更新数据成功")

# 0.2.2 25.09.08 版本喜加一适配
if not game_free_cache_file.exists():
    atomic_write_json(game_free_cache_file, [])

# 26.07.07 图片播报开关适配
steam_group_26_07_07: Dict[str, GroupData3] = load_json_or_recover(new_file_group, {})  # type: ignore[assignment]
value_26_07_07 = next(iter(steam_group_26_07_07.values()), None)
if value_26_07_07:
    if "image" not in value_26_07_07:
        steam_group_dict_26_07_07 = {}
        for group_id in steam_group_26_07_07:
            steam_group_dict_26_07_07[group_id] = GroupDataNew(
                status=steam_group_26_07_07[group_id]["status"],
                user_list=steam_group_26_07_07[group_id]["user_list"],
                adapter=steam_group_26_07_07[group_id]["adapter"],
                xijiayi=steam_group_26_07_07[group_id]["xijiayi"],
                image=True,
                stop_image=False
            )
        atomic_write_json(new_file_group, steam_group_dict_26_07_07)
        logger.success("steam 0.3.6 26.07.07 图片播报开关数据更新成功")

# 26.07.07 结束游戏图片播报开关适配
steam_group_stop_image_26_07_07: Dict[str, GroupDataNew] = load_json_or_recover(new_file_group, {})  # type: ignore[assignment]
value_stop_image_26_07_07 = next(iter(steam_group_stop_image_26_07_07.values()), None)
if value_stop_image_26_07_07:
    if "stop_image" not in value_stop_image_26_07_07:
        steam_group_stop_image_dict_26_07_07 = {}
        for group_id in steam_group_stop_image_26_07_07:
            steam_group_stop_image_dict_26_07_07[group_id] = GroupDataNew(
                status=steam_group_stop_image_26_07_07[group_id]["status"],
                user_list=steam_group_stop_image_26_07_07[group_id]["user_list"],
                adapter=steam_group_stop_image_26_07_07[group_id]["adapter"],
                xijiayi=steam_group_stop_image_26_07_07[group_id]["xijiayi"],
                image=steam_group_stop_image_26_07_07[group_id]["image"],
                stop_image=False
            )
        atomic_write_json(new_file_group, steam_group_stop_image_dict_26_07_07)
        logger.success("steam 0.3.6 26.07.07 结束游戏图片播报开关数据更新成功")

# 25.11.28 低价订阅适配
if not game_discounted_cache_file.exists():
    atomic_write_json(game_discounted_cache_file, [])
if not game_discounted_subscribe_file.exists():
    atomic_write_json(game_discounted_subscribe_file, {})

# 26.07.07 Steam 游戏库入库播报适配
steam_group_owned_game_26_07_07: Dict[str, GroupDataNew] = load_json_or_recover(new_file_group, {})  # type: ignore[assignment]
value_owned_game_26_07_07 = next(iter(steam_group_owned_game_26_07_07.values()), None)
if value_owned_game_26_07_07:
    if "owned_game" not in value_owned_game_26_07_07:
        steam_group_owned_game_dict_26_07_07 = {}
        for group_id in steam_group_owned_game_26_07_07:
            steam_group_owned_game_dict_26_07_07[group_id] = GroupDataNew(
                status=steam_group_owned_game_26_07_07[group_id]["status"],
                user_list=steam_group_owned_game_26_07_07[group_id]["user_list"],
                adapter=steam_group_owned_game_26_07_07[group_id]["adapter"],
                xijiayi=steam_group_owned_game_26_07_07[group_id]["xijiayi"],
                image=steam_group_owned_game_26_07_07[group_id]["image"],
                stop_image=steam_group_owned_game_26_07_07[group_id]["stop_image"],
                owned_game=False
            )
        atomic_write_json(new_file_group, steam_group_owned_game_dict_26_07_07)
        logger.success("steam 0.3.6 26.07.07 游戏库入库播报开关数据更新成功")
if not owned_games_file.exists():
    atomic_write_json(owned_games_file, {})

# 26.07.08 Steam 状态卡片背景群开关适配
steam_group_card_background_26_07_08: Dict[str, GroupDataNew] = load_json_or_recover(new_file_group, {})  # type: ignore[assignment]
value_card_background_26_07_08 = next(iter(steam_group_card_background_26_07_08.values()), None)
if value_card_background_26_07_08:
    if any(
        "image_background" not in group_data
        or "stop_image_background" not in group_data
        or "stop_image_background_grayscale" not in group_data
        for group_data in steam_group_card_background_26_07_08.values()
    ):
        steam_group_card_background_dict_26_07_08 = {}
        for group_id, group_data in steam_group_card_background_26_07_08.items():
            group_data.setdefault("image_background", True)
            group_data.setdefault("stop_image_background", False)
            group_data.setdefault("stop_image_background_grayscale", False)
            steam_group_card_background_dict_26_07_08[group_id] = group_data
        atomic_write_json(new_file_group, steam_group_card_background_dict_26_07_08)
        logger.success("steam 0.4.0 26.07.08 状态卡片背景群开关数据更新成功")

# 26.07.08 UserData 当前游戏 appid 适配
steam_list_game_id_26_07_08: Dict[str, UserData] = load_json_or_recover(new_file_steam, {})  # type: ignore[assignment]
value_game_id_26_07_08 = next(iter(steam_list_game_id_26_07_08.values()), None)
if value_game_id_26_07_08 and any("game_id" not in user_data for user_data in steam_list_game_id_26_07_08.values()):
    for user_data in steam_list_game_id_26_07_08.values():
        user_data.setdefault("game_id", "")
    atomic_write_json(new_file_steam, steam_list_game_id_26_07_08)
    logger.success("steam 0.4.0 26.07.08 用户当前游戏 appid 数据更新成功")

# 26.11.15 GroupDataNew 新增 auto_unbind_on_leave 和 steam_bind_initiators 字段适配
steam_group_auto_unbind_26_11_15: Dict[str, GroupDataNew] = load_json_or_recover(new_file_group, {})  # type: ignore[assignment]
value_auto_unbind_26_11_15 = next(iter(steam_group_auto_unbind_26_11_15.values()), None)
if value_auto_unbind_26_11_15 and any(
    "auto_unbind_on_leave" not in group_data 
    or "steam_bind_initiators" not in group_data
    for group_data in steam_group_auto_unbind_26_11_15.values()
):
    for group_data in steam_group_auto_unbind_26_11_15.values():
        group_data.setdefault("auto_unbind_on_leave", True)
        group_data.setdefault("steam_bind_initiators", {})
    atomic_write_json(new_file_group, steam_group_auto_unbind_26_11_15)
    logger.success("steam 0.4.0 26.11.15 群组自动解绑和Steam绑定发起者数据更新成功")

group_list: Dict[str, GroupDataNew] = load_json_or_recover(new_file_group, {})  # type: ignore[assignment]
steam_list: Dict[str, UserData] = load_json_or_recover(new_file_steam, {})  # type: ignore[assignment]
gameid2name: Dict[str, str] = load_json_or_recover(game_cache_file, {})  # type: ignore[assignment]
exclude_game: Dict[str, List[str]] = load_json_or_recover(exclude_game_file, {})  # type: ignore[assignment]
game_free_cache: List[str] = load_json_or_recover(game_free_cache_file, [])  # type: ignore[assignment]
game_discounted_cache: List[str] = load_json_or_recover(game_discounted_cache_file, [])  # type: ignore[assignment]
game_discounted_subscribe: Dict[str, List[str]] = load_json_or_recover(game_discounted_subscribe_file, {})  # type: ignore[assignment]
owned_games: Dict[str, Dict[str, str]] = load_json_or_recover(owned_games_file, {})  # type: ignore[assignment]

# 0.5.0：新增按群已处理状态。旧版本无法区分“已发送”和“曾被屏蔽后写入缓存”，
# 因此首次迁移或文件整体不可用时建立保守基线，避免给所有正在游戏的用户补报。
loaded_reported_state = load_reported_steam_state() if reported_steam_state_file.exists() else None
if loaded_reported_state is None:
    reported_steam_state: Dict[str, Dict[str, UserData]] = build_conservative_reported_state(
        steam_list, group_list, exclude_game, exclude_game_default
    )  # type: ignore[assignment]
    save_reported_steam_state(reported_steam_state)
    logger.info("Steam 按群已处理状态已建立保守基线，不补发已有游戏状态")
else:
    reported_steam_state = loaded_reported_state

# 与bot失联的group列表
inactive_groups: List[str] = []
inactive_groups_file: Path = data_dir / "inactive_groups.json"

# 初始化 inactive_groups
if inactive_groups_file.exists():
    inactive_groups = load_json_or_recover(inactive_groups_file, [])  # type: ignore[assignment]
else:
    atomic_write_json(inactive_groups_file, [])
    
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width={width}, initial-scale=1.0">
    <style>
        /* 全局重置 */
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        /* 页面容器 */
        html, body {{
            background-color: #fff;
            overflow-x: hidden;
            width: 100%;
            min-width: {width}px;
        }}
        
        /* 内容容器 - 关键修复 */
        .container {{
            width: {width}px;
            max-width: 100%;
            margin: 0 auto;
            padding: 20px;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            line-height: 1.6;
            color: #333;
        }}
        
        /* 标题样式 */
        h1, h2, h3 {{
            margin: 1.5em 0 0.8em;
            padding-bottom: 0.3em;
            border-bottom: 1px solid #eee;
        }}
        
        /* 段落样式 */
        p {{
            margin: 1em 0;
            text-align: justify;
            word-wrap: break-word;
            overflow-wrap: break-word; /* 确保长单词换行 */
        }}
        
        /* 媒体容器 - 关键修复 */
        .bb_img_ctn {{
            margin: 1.5em auto;
            text-align: center;
            width: 100%;
            max-width: {width}px;
            overflow: hidden; /* 防止内容溢出 */
        }}
        
        /* 图片和视频样式 - 关键修复 */
        img.bb_img, video.bb_img {{
            max-width: 100%;
            height: auto;
            display: block;
            margin: 0 auto;
        }}
        
        /* 列表样式 */
        ul.bb_ul {{
            padding-left: 2em;
            margin: 1em 0;
        }}
        
        li {{
            margin: 0.5em 0;
        }}
        
        /* 强调文本 */
        strong {{
            color: #e74c3c;
        }}
        
        /* 特殊元素处理 */
        br {{
            display: block;
            content: "";
            margin: 0.5em 0;
        }}
        
        /* 修复嵌套问题 */
        p > p {{
            margin: 0 !important;
            padding: 0 !important;
        }}
    </style>
</head>
<body>
    <div class="container">
        {content}
    </div>
</body>
</html>
"""
