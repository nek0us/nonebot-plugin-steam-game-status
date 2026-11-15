from dataclasses import dataclass
from nonebot.adapters import Bot, Event

@dataclass(frozen=True)
class MemberLeave:
    adapter: str
    group_id: str
    user_id: str
    
def parse_member_leave(bot: Bot, event: Event) -> MemberLeave | None:
    adapter_name = bot.adapter.get_name()
    
    if adapter_name == "OneBot V11":
        if event.get_event_name() not in (
            "notice.group_decrease.leave",
            "notice.group_decrease.kick",
        ):
            return None
        group_id = getattr(event, "group_id", None)
        user_id = getattr(event, "user_id", None)
        
        if group_id is None or user_id is None:
            return None
        
        if user_id == str(bot.self_id):
            return None
        
        
        return MemberLeave(
            adapter=bot.adapter.get_name(),
            group_id=str(group_id),
            user_id=str(user_id)
        )
        
    return None