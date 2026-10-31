from nonebot.log import logger
from typing import Union, Optional, List
from pydantic import BaseModel,validator
from nonebot import get_plugin_config, get_driver

from importlib.metadata import version

try:
    __version__ = version("nonebot_plugin_steam_game_status")
except Exception:
    __version__ = None

class Config(BaseModel):
    steam_web_key: Union[str, List[str]] = []
    steam_isthereanydeal_key: Union[str, List[str]] = []
    steam_command_priority: int = 5
    steam_interval: int = 1
    steam_proxy: Optional[str] = None
    steam_api_proxy: Optional[str] = None
    steam_store_proxy: Optional[str] = None
    steam_plugin_enabled: bool = True
    steam_link_enabled: bool = True
    steam_area_game: Union[bool, List[str]]= False
    steam_link_r18_game: Union[bool, List[str]] = False
    steam_tail_tone: str = ""
    steam_pretty_stop_duration: bool = True
    steam_subscribe_time: Union[str, List[str]] = ["08:00"]
    steam_status_query_concurrency: int = 6
    # 1 表示保持原有行为：一次成功的无游戏状态即播报停止。
    # 设置为 2 或更高时，需要连续多次成功查询无游戏才播报停止。
    steam_stop_confirmations: int = 1
    steam_owned_game_interval: int = 60
    steam_owned_game_include_demo: bool = False
    steam_owned_game_query_concurrency: int = 5
    steam_owned_game_baseline_concurrency: int = 5
    steam_playtime_timeout_ms: int = 20000
    steam_link_timeout_seconds: Optional[int] = 15
    steam_card_watermark: bool = False
    steam_dynamic_avatar_card: bool = False
    steam_dynamic_card_cache: bool = True
    steam_dynamic_card_preserve_avatar_gif_timing: bool = True
    steam_dynamic_card_max_avatar_frames: int = 120
    steam_dynamic_card_frame_count: int = 50
    steam_dynamic_card_frame_duration_ms: int = 80
    steam_dynamic_card_capture_interval_ms: int = 80
    steam_dynamic_card_capture_duration_ms: int = 4000
    steam_dynamic_card_timeout_ms: int = 15000
    steam_dynamic_avatar_cache_ttl_minutes: int = 60
    steam_image_cache_retention_days: int = 30
    
    @validator("steam_isthereanydeal_key")
    def check_isthereanydeal_key(cls,v: Union[str, List[str]]) -> Union[str, List[str]]:
        if isinstance(v,str):
            logger.success("steam_isthereanydeal_key 读取成功")
        elif isinstance(v, list) and all(isinstance(item, str) for item in v):
            logger.success("steam_isthereanydeal_key 列表读取成功")
        else:
            logger.error("steam_isthereanydeal_key 配置错误")
            # raise ValueError("steam_isthereanydeal_key 配置错误")
        return v
    
    @validator("steam_web_key")
    def check_api_key(cls,v: Union[str, List[str]]) -> Union[str, List[str]]:
        if isinstance(v,str):
            logger.success("steam_web_key 读取成功")
        elif isinstance(v, list) and all(isinstance(item, str) for item in v):
            logger.success("steam_web_key 列表读取成功")
        else:
            logger.error("steam_web_key 配置错误")
            # raise ValueError("steam_web_key 配置错误")
        return v
            
    @validator("steam_command_priority")
    def check_priority(cls,v: int) -> int:
        if v >= 1:
            return v
        raise ValueError("命令优先级必须为大于0的整数")
    
    @validator("steam_interval")
    def check_steam_interval(cls,v: int) -> int:
        if v >= 1:
            return v
        raise ValueError("steam查询间隔必须为大于0的整数")    

    @validator("steam_status_query_concurrency")
    def check_steam_status_query_concurrency(cls, v: int) -> int:
        if v >= 1:
            return v
        raise ValueError("steam_status_query_concurrency 必须为大于0的整数")

    @validator("steam_stop_confirmations")
    def check_steam_stop_confirmations(cls, v: int) -> int:
        if v >= 1:
            return v
        raise ValueError("steam_stop_confirmations 必须为大于等于1的整数")

    @validator("steam_owned_game_interval")
    def check_steam_owned_game_interval(cls, v: int) -> int:
        if v >= 1:
            return v
        raise ValueError("steam游戏库入库播报查询间隔必须为大于0的整数")

    @validator("steam_owned_game_query_concurrency")
    def check_steam_owned_game_query_concurrency(cls, v: int) -> int:
        if v >= 1:
            return v
        raise ValueError("steam_owned_game_query_concurrency 必须为大于0的整数")

    @validator("steam_owned_game_baseline_concurrency")
    def check_steam_owned_game_baseline_concurrency(cls, v: int) -> int:
        if v >= 1:
            return v
        raise ValueError("steam_owned_game_baseline_concurrency 必须为大于0的整数")

    @validator("steam_playtime_timeout_ms")
    def check_playtime_timeout_ms(cls, v: int) -> int:
        if v >= 1000:
            return v
        raise ValueError("steam_playtime_timeout_ms 必须为大于等于1000的整数")

    @validator("steam_link_timeout_seconds")
    def check_link_timeout_seconds(cls, v: int) -> int:
        if v >= 1:
            return v
        raise ValueError("steam_link_timeout_seconds 必须为大于等于1的整数")
    
    @validator("steam_proxy")
    def check_proxy(cls,v:Union[str, None]) -> Union[str, None]:
        if isinstance(v,str):
            logger.success(f"steam_proxy {v} 读取成功")
            return v

        
    @validator("steam_plugin_enabled")
    def check_steam_plugin_enabled(cls,v: bool) -> bool:
        return v
        
    @validator("steam_link_enabled")
    def check_steam_link_enabled(cls,v: bool) -> bool:
        if v:
            logger.success("steam_link_enabled 链接识别 已开启")
        else:
            logger.success("steam_link_enabled 链接识别 已关闭")
        return v
        
    @validator("steam_area_game")
    def check_steam_area_game(cls,v: Union[bool, List[str]]) -> Union[bool, List[str]]:
        if isinstance(v, bool):
            if v:
                logger.success("steam_area_game 其它区游戏识别 已开启")
            else:
                logger.success("steam_area_game 其它区游戏识别 已关闭")
            return v      
        elif isinstance(v, list) and all(isinstance(i, str) for i in v):
            logger.success(f"steam_area_game 其它区游戏识别 已为部分群开启：{' '.join(v)}")
            return v
        else:
            logger.error("steam_area_game 其它区游戏识别 配置错误")
            raise ValueError("steam_area_game 其它区游戏识别 配置错误")
        
    @validator("steam_link_r18_game")
    def check_steam_link_r18_game(cls,v: Union[bool, List[str]]) -> Union[bool, List[str]]:
        if isinstance(v, bool):
            if v:
                logger.success("steam_link_r18_game 识别 已开启")
            else:
                logger.success("steam_link_r18_game 识别 已关闭")
            return v
        elif isinstance(v, list) and all(isinstance(i, str) for i in v):
            logger.success(f"steam_link_r18_game 识别 已为部分群开启：{' '.join(v)}")
            return v
        else:
            logger.error("steam_link_r18_game 识别 配置错误")
            raise ValueError("steam_link_r18_game 识别 配置错误")
        
    @validator("steam_tail_tone")
    def check_tail_tone(cls,v: str) -> str:
        if v:
            logger.success("steam_tail_tone 读取成功")
        else:
            logger.success("steam_tail_tone未配置")
        return v

    @validator("steam_pretty_stop_duration")
    def check_pretty_stop_duration(cls, v: bool) -> bool:
        if v:
            logger.info("steam_pretty_stop_duration 已开启，停止播报将使用天/小时/分钟格式")
        return v
        
    @validator("steam_subscribe_time")
    def check_subscribe_time(cls,v: Union[str, List[str]]) -> List[str]:
        if v:
            if isinstance(v, str):
                if ":" in v:
                    logger.success(f"steam_subscribe_time 订阅时间 {v} 读取成功")
                    return [v]
                else:
                    logger.exception(f"steam_subscribe_time 订阅时间 {v} 设置格式错误，将使用默认时间 08:00 ")
                    return ["08:00"]
            elif isinstance(v, list) and all(isinstance(i, str) for i in v):
                if all(":" in i for i in v):
                    logger.success(f"steam_subscribe_time 订阅时间 {v} 读取成功")
                else:
                    logger.exception(f"steam_subscribe_time 订阅时间 {v} 配置错误，将使用默认时间 08:00 ")
                    return ["08:00"]
            else:
                logger.warning("steam_subscribe_time配置错误，将使用默认时间 08:00 ")
                return ["08:00"]
        else:
            logger.info("steam_subscribe_time未配置，将使用默认时间 08:00 ")
            return ["08:00"]
        return v

    @validator("steam_dynamic_avatar_card")
    def check_dynamic_avatar_card(cls, v: bool) -> bool:
        if v:
            logger.info("steam_dynamic_avatar_card 已开启，将尝试生成动态头像卡片")
        return v

    @validator("steam_dynamic_card_cache")
    def check_dynamic_card_cache(cls, v: bool) -> bool:
        if v:
            logger.info("steam_dynamic_card_cache 动态卡片缓存 已开启")
        else:
            logger.info("steam_dynamic_card_cache 动态卡片缓存 已关闭")
        return v

    @validator("steam_dynamic_card_preserve_avatar_gif_timing")
    def check_dynamic_card_preserve_avatar_gif_timing(cls, v: bool) -> bool:
        if v:
            logger.info("steam_dynamic_card_preserve_avatar_gif_timing 已开启，将尽量保留原头像 GIF 帧时序")
        return v

    @validator("steam_dynamic_card_max_avatar_frames")
    def check_dynamic_card_max_avatar_frames(cls, v: int) -> int:
        if v >= 0:
            return v
        raise ValueError("steam_dynamic_card_max_avatar_frames 必须为大于等于0的整数")

    @validator("steam_dynamic_card_frame_count")
    def check_dynamic_card_frame_count(cls, v: int) -> int:
        if v >= 2:
            return v
        raise ValueError("steam_dynamic_card_frame_count 必须为大于等于2的整数")

    @validator("steam_dynamic_card_frame_duration_ms")
    def check_dynamic_card_frame_duration_ms(cls, v: int) -> int:
        if v >= 20:
            return v
        raise ValueError("steam_dynamic_card_frame_duration_ms 必须为大于等于20的整数")

    @validator("steam_dynamic_card_capture_interval_ms")
    def check_dynamic_card_capture_interval_ms(cls, v: int) -> int:
        if v >= 20:
            return v
        raise ValueError("steam_dynamic_card_capture_interval_ms 必须为大于等于20的整数")

    @validator("steam_dynamic_card_capture_duration_ms")
    def check_dynamic_card_capture_duration_ms(cls, v: int) -> int:
        if v >= 0:
            return v
        raise ValueError("steam_dynamic_card_capture_duration_ms 必须为大于等于0的整数")

    @validator("steam_dynamic_card_timeout_ms")
    def check_dynamic_card_timeout_ms(cls, v: int) -> int:
        if v >= 1000:
            return v
        raise ValueError("steam_dynamic_card_timeout_ms 必须为大于等于1000的整数")

    @validator("steam_dynamic_avatar_cache_ttl_minutes")
    def check_dynamic_avatar_cache_ttl_minutes(cls, v: int) -> int:
        if v >= 0:
            return v
        raise ValueError("steam_dynamic_avatar_cache_ttl_minutes 必须为大于等于0的整数")

    @validator("steam_image_cache_retention_days")
    def check_image_cache_retention_days(cls, v: int) -> int:
        if v >= 0:
            return v
        raise ValueError("steam_image_cache_retention_days 必须为大于等于0的整数")
    
config_steam = get_plugin_config(Config)
bot_name = list(get_driver().config.nickname)

def get_steam_api_domain() -> str:
    """获取Steam API域名，如果配置了代理则使用代理域名"""
    return config_steam.steam_api_proxy if config_steam.steam_api_proxy else "api.steampowered.com"

def get_steam_store_domain() -> str:
    """获取Steam Store域名，如果配置了代理则使用代理域名"""
    return config_steam.steam_store_proxy if config_steam.steam_store_proxy else "store.steampowered.com"
