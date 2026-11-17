from collections.abc import Awaitable
import json
import random
import itertools
from typing import Any, Callable, List, Optional, TypedDict, Union
from nonebot import get_bots,get_adapter
from nonebot import get_bot as _get_bot, get_adapters
from nonebot.adapters import Adapter, Bot
from nonebot.internal.driver import Response, HeaderTypes, ContentTypes
from nonebot_plugin_alconna import SupportAdapter
from nonebot_plugin_alconna.uniseg import Target, SerializeFailed
from nonebot_plugin_alconna.uniseg.constraint import lang, log
from nonebot_plugin_alconna.uniseg.target import _cache_selector


class GroupData(TypedDict):
    status: bool
    user_list: List[str]

class GroupData2(GroupData):
    adapter: str
    
class GroupData3(GroupData2):
    xijiayi: bool
    
class GroupDataNew(GroupData3):
    image: bool
    image_background: bool
    stop_image: bool
    stop_image_background: bool
    stop_image_grayscale: bool
    stop_image_background_grayscale: bool
    owned_game: bool
    auto_unbind_on_leave: bool
    steam_bind_initiators: dict[str, str]

def create_group_data(
        status: bool = True,
        user_list: Optional[List[str]] = None,
        adapter: str = "OneBot V11",
        xijiayi: bool = False,
        image: bool = True,
        image_background: bool = True,
        stop_image: bool = False,
        stop_image_background: bool = False,
        stop_image_grayscale: bool = False,
        stop_image_background_grayscale: bool = False,
        owned_game: bool = False,
        auto_unbind_on_leave: bool = True,
        steam_bind_initiators: Optional[dict[str, str]] = None
        ) -> GroupDataNew:
    '''创建数据结构统一为函数调用'''
    return GroupDataNew(
        status=status,
        user_list=list(user_list) if user_list is not None else [],
        adapter=adapter,
        xijiayi=xijiayi,
        image=image,
        image_background=image_background,
        stop_image=stop_image,
        stop_image_background=stop_image_background,
        stop_image_grayscale=stop_image_grayscale,
        stop_image_background_grayscale=stop_image_background_grayscale,
        owned_game=owned_game,
        auto_unbind_on_leave=auto_unbind_on_leave,
        steam_bind_initiators=dict(steam_bind_initiators) if steam_bind_initiators is not None else {}
    )

class UserData(TypedDict):
    time: int
    game_name: str
    nickname: str
    game_id: str

class SafeResponse:
    def __init__(self, response: Response):
        self._response = response

    @property
    def status_code(self) -> int:
        return self._response.status_code
    
    @property
    def headers(self) -> "HeaderTypes":
        return self._response.headers
    
    @property
    def content(self) -> "ContentTypes":
        return self._response.content
    
    @property
    def request(self) -> Any:
        return self._response.request
    
    def __repr__(self) -> str:
        return f"SafeResponse({self._response})"
    
    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)
    
    @property
    def text(self) -> str:
        if self._response.content is None:
            return ""
        elif isinstance(self._response.content, str):
            return self._response.content
        elif isinstance(self._response.content, bytes):
            try:
                return self._response.content.decode('utf-8')
            except UnicodeDecodeError:
                try:
                    return self._response.content.decode('latin-1')
                except UnicodeDecodeError:
                    return self._response.content.decode('utf-8', errors='replace')
        else:
            return str(self._response.content)
    
    def json(self) -> dict:
        try:
            return json.loads(self.text)
        except json.JSONDecodeError:
            raise ValueError(f"Response is not valid JSON: {self.text[:100]}...")

def get_really_bots():
    bots_drivers = list(get_bots().values())
    log("TRACE", f"drivers all bots: {bots_drivers}")
    adapters = get_adapters()
    bots_adapters = list(itertools.chain.from_iterable(get_adapter(adapter_name).bots.values() for adapter_name in adapters))
    log("TRACE", f"adapters all bots: {bots_adapters}")
    if len(bots_drivers) > len(bots_adapters):
        bots = bots_drivers
        log("DEBUG", f"使用从驱动器获取bots: {bots}")
    else:
        bots = bots_adapters
        log("DEBUG", f"使用从适配器获取bots: {bots}")
    return bots
    
async def mod_get_bot(
    *,
    id: Optional[str] = None,
    adapter: Union[type[Adapter], str, None] = None,
    bot_id: Optional[str] = None,
    index: Optional[int] = None,
    rand: bool = False,
    predicate: Union[Callable[[Bot], Awaitable[bool]], None] = None,
) -> Union[list[Bot], Bot]:
    really_bots = get_really_bots()
    if not predicate and not adapter:
        if rand:
            return random.choice(really_bots)
        if index is not None:
            return really_bots[index]
        return _get_bot(bot_id)
    bots = []
    for bot in really_bots:
        if not predicate:

            async def _check_adapter(bot: Bot):
                _adapter = bot.adapter
                if isinstance(adapter, str):
                    return _adapter.get_name() == adapter
                return isinstance(_adapter, adapter)  # type: ignore

            predicate = _check_adapter
        if await predicate(bot):
            bots.append(bot)
    log("TRACE", f"get bots: {bots}")
    
    if not bot_id:
        if adapter == "OneBot V11" and id:
            for bot in bots:
                if await is_in_group(bot=bot, group_id=(int(id))):
                    log("DEBUG",f"检测到群对应bot，群:{id}，bot:{bot}")
                    return bot
            log("DEBUG",f"检测群对应bot失败，群:{id}，bots：{bots}")
            return []
        if rand:
            return random.choice(bots)
        if index is not None:
            return bots[index]
        return bots
    return next(bot for bot in bots if bot.self_id == bot_id)

class ModTarget(Target):
    def __init__(
            self, 
            id: str, 
            parent_id: str = "", 
            channel: bool = False, 
            private: bool = False, 
            source: str = "", 
            self_id: Union[str, None] = None, 
            selector: Union[Callable[["Target", Bot], Awaitable[bool]], None] = _cache_selector, 
            scope: Union[str, None] = None, 
            adapter: Union[str, type[Adapter], SupportAdapter, None] = None, 
            platform: Union[str, set[str], None] = None,
            extra: Union[dict[str, Any], None] = None,
            ):
        super().__init__(id, parent_id, channel, private, source, self_id, selector, scope, adapter, platform, extra)

    async def mod_select(self):
        if self.self_id:
            try:
                return await mod_get_bot(bot_id=self.self_id)
            except KeyError:
                self.self_id = None
        if self.selector:
            return await mod_get_bot(id=self.id, adapter=self.adapter, predicate=self.selector, rand=True)
        raise SerializeFailed(lang.require("nbp-uniseg", "bot_missing"))
    

async def is_in_group(bot: Bot, group_id: str) -> bool:
    groups = await bot.call_api("get_group_list")

    if not isinstance(groups, list):
        raise ValueError("群列表返回值不是列表")

    return any(
        str(group.get("group_id")) == str(group_id)
        for group in groups
        if isinstance(group, dict)
    )

async def get_group_member_ids(bot: Bot, group_id: str) -> set[str] | None:
    if bot.adapter.get_name() == "OneBot V11":
        try:
            group_list = await bot.call_api("get_group_list")
            group = next((group for group in group_list if str(group.get("group_id")) == str(group_id)), None)
            if not group:
                log("TRACE", f"Steam插件巡检获取群成员列表失败: 未找到群信息: bot {bot.self_id} 群 {group_id} Adapter {bot.adapter.get_name()}")
                return None
            
            member_count = group["member_count"]
        
            members = await bot.call_api("get_group_member_list", group_id=int(group_id))
            if not isinstance(members, list) or not members:
                log("WARNING", f"Steam插件巡检获取群成员列表失败: 返回值不是列表或为空: bot {bot.self_id} 群 {group_id}")
                return None
            if len(members) != member_count:
                log("WARNING", f"Steam插件巡检获取群成员列表失败: 返回值长度与群成员数不符: bot {bot.self_id} 群 {group_id} 返回值长度 {len(members)} 群成员数 {member_count}")
                return None
            
        except Exception as e:
            log("WARNING", f"Steam插件巡检获取群成员列表失败: {e}")
            return None
        
        if any(not isinstance(member, dict) or member.get("user_id") is None for member in members):
            log("WARNING", f"Steam插件巡检获取群成员列表失败: 返回值中包含非字典元素或缺少user_id: bot {bot.self_id} 群 {group_id}")
            return None
        
        member_ids = {str(member["user_id"]) for member in members}
        if str(bot.self_id) not in member_ids:
            log("WARNING", f"Steam插件巡检获取群成员列表失败: bot {bot.self_id} 不在群 {group_id} 成员列表中")
            return None
        
        return member_ids
        
    return None

async def get_group_member_ids_by_bots(group_id: str, adapter: str) -> set[str] | None:
    bots = get_really_bots()
    members_ids = set()
    for bot in bots:
        if bot.adapter.get_name().casefold() != adapter.casefold():
            continue
        member_ids = await get_group_member_ids(bot, group_id)
        if member_ids is not None:
            members_ids.update(member_ids)
    if not members_ids:
        log("WARNING", f"Steam插件巡检获取群成员列表失败: 所有bot都无法获取群成员列表，群号: {group_id}")
        return None
    
    return members_ids