import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "nonebot_plugin_steam_game_status"
package = types.ModuleType("nonebot_plugin_steam_game_status")
package.__path__ = [str(PACKAGE_ROOT)]


class FakeUniMessage:
    sent_messages = []
    fail_sends = False

    def __init__(self, text):
        self.text = text

    async def send(self, *, target=None, bot=None):
        type(self).sent_messages.append((self.text, target, bot))
        if type(self).fail_sends:
            raise RuntimeError("send failed")


logger = types.SimpleNamespace(
    info=Mock(),
    debug=Mock(),
    warning=Mock(),
)
nonebot_module = types.ModuleType("nonebot")
nonebot_log_module = types.ModuleType("nonebot.log")
nonebot_log_module.logger = logger
alc_module = types.ModuleType("nonebot_plugin_alconna")
alc_module.__path__ = []
uniseg_module = types.ModuleType("nonebot_plugin_alconna.uniseg")
uniseg_module.UniMessage = FakeUniMessage

api_module = types.ModuleType("nonebot_plugin_steam_game_status.api")
api_module.gameid_to_name = AsyncMock(side_effect=lambda appid, name: name)
api_module.get_game_info = AsyncMock(return_value={"success": False})
api_module.get_group_target_bot = AsyncMock(return_value=("target", "bot"))
api_module.get_owned_games = AsyncMock(return_value={})
api_module.save_data = Mock()
api_module.test_group_active = AsyncMock()

config_module = types.ModuleType("nonebot_plugin_steam_game_status.config")
config_module.config_steam = types.SimpleNamespace(
    steam_web_key="test-key",
    steam_owned_game_baseline_concurrency=2,
    steam_owned_game_query_concurrency=2,
    steam_owned_game_include_demo=False,
)
source_module = types.ModuleType("nonebot_plugin_steam_game_status.source")
source_module.group_list = {}
source_module.owned_games = {}
source_module.steam_list = {}

fake_modules = {
    "nonebot": nonebot_module,
    "nonebot.log": nonebot_log_module,
    "nonebot_plugin_alconna": alc_module,
    "nonebot_plugin_alconna.uniseg": uniseg_module,
    "nonebot_plugin_steam_game_status": package,
    "nonebot_plugin_steam_game_status.api": api_module,
    "nonebot_plugin_steam_game_status.config": config_module,
    "nonebot_plugin_steam_game_status.source": source_module,
}

spec = importlib.util.spec_from_file_location(
    "nonebot_plugin_steam_game_status.owned_games", PACKAGE_ROOT / "owned_games.py"
)
assert spec and spec.loader
owned_games_module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {**fake_modules, spec.name: owned_games_module}):
    spec.loader.exec_module(owned_games_module)


class TestOwnedGamesDemoFiltering(unittest.IsolatedAsyncioTestCase):
    def set_api_mock(self, name, value):
        setattr(api_module, name, value)
        setattr(owned_games_module, name, value)

    def setUp(self):
        owned_games_module._DEMO_TYPE_CACHE.clear()
        source_module.group_list.clear()
        source_module.owned_games.clear()
        source_module.steam_list.clear()
        FakeUniMessage.sent_messages.clear()
        FakeUniMessage.fail_sends = False
        config_module.config_steam.steam_web_key = "test-key"
        config_module.config_steam.steam_owned_game_include_demo = False
        config_module.config_steam.steam_owned_game_query_concurrency = 2
        self.set_api_mock("gameid_to_name", AsyncMock(side_effect=lambda appid, name: name))
        self.set_api_mock("get_game_info", AsyncMock(return_value={"success": False}))
        self.set_api_mock("get_group_target_bot", AsyncMock(return_value=("target", "bot")))
        self.set_api_mock("get_owned_games", AsyncMock(return_value={}))
        self.set_api_mock("save_data", Mock())
        self.set_api_mock("test_group_active", AsyncMock())

    def configure_user(self, initial_games, current_games):
        source_module.group_list["group"] = {
            "status": True,
            "owned_game": True,
            "user_list": ["steam-user"],
        }
        source_module.steam_list["steam-user"] = {"nickname": "Player"}
        source_module.owned_games["steam-user"] = dict(initial_games)
        self.set_api_mock("get_owned_games", AsyncMock(return_value=dict(current_games)))

    async def test_only_demo_is_silent_and_absorbed_into_baseline(self):
        self.configure_user({"10": "Existing"}, {"10": "Existing", "20": "Demo App"})
        self.set_api_mock("get_game_info", AsyncMock(return_value={"success": True, "data": {"type": "Demo"}}))

        await owned_games_module.run_owned_games_check()
        self.assertEqual(FakeUniMessage.sent_messages, [])
        self.assertEqual(source_module.owned_games["steam-user"]["20"], "Demo App")
        self.assertEqual(api_module.get_game_info.await_count, 1)

        await owned_games_module.run_owned_games_check()
        self.assertEqual(api_module.get_game_info.await_count, 1)
        self.assertEqual(FakeUniMessage.sent_messages, [])

    async def test_mixed_list_sends_only_non_demo_and_names_do_not_decide(self):
        self.configure_user({}, {"20": "Demo App", "30": "Demon's Game"})
        self.set_api_mock("get_game_info", AsyncMock(side_effect=[
            {"success": True, "data": {"type": "demo"}},
            {"success": True, "data": {"type": "game"}},
        ]))

        await owned_games_module.run_owned_games_check()

        self.assertEqual(len(FakeUniMessage.sent_messages), 1)
        message = FakeUniMessage.sent_messages[0][0]
        self.assertIn("Demon's Game", message)
        self.assertNotIn("Demo App", message)
        self.assertEqual(set(source_module.owned_games["steam-user"]), {"20", "30"})

    async def test_include_demo_true_skips_type_lookup(self):
        self.configure_user({}, {"20": "Demo App"})
        config_module.config_steam.steam_owned_game_include_demo = True

        await owned_games_module.run_owned_games_check()

        self.assertEqual(api_module.get_game_info.await_count, 0)
        self.assertIn("Demo App", FakeUniMessage.sent_messages[0][0])

    async def test_failed_mixed_send_absorbs_only_demo_and_retries_real_game(self):
        current = {"20": "Demo App", "30": "Real Game"}
        self.configure_user({}, current)
        self.set_api_mock("get_game_info", AsyncMock(side_effect=[
            {"success": True, "data": {"type": "demo"}},
            {"success": True, "data": {"type": "game"}},
        ]))
        FakeUniMessage.fail_sends = True

        await owned_games_module.run_owned_games_check()
        self.assertEqual(source_module.owned_games["steam-user"], {"20": "Demo App"})
        self.assertIn("Real Game", FakeUniMessage.sent_messages[0][0])
        FakeUniMessage.fail_sends = False

        await owned_games_module.run_owned_games_check()
        self.assertEqual(len(FakeUniMessage.sent_messages), 2)
        self.assertIn("Real Game", FakeUniMessage.sent_messages[1][0])
        self.assertNotIn("Demo App", FakeUniMessage.sent_messages[1][0])
        self.assertEqual(source_module.owned_games["steam-user"], current)
        self.assertEqual(api_module.get_game_info.await_count, 2)

    async def test_first_baseline_is_silent_and_does_not_query_types(self):
        source_module.group_list["group"] = {
            "status": True,
            "owned_game": True,
            "user_list": ["steam-user"],
        }
        source_module.steam_list["steam-user"] = {"nickname": "Player"}
        self.set_api_mock("get_owned_games", AsyncMock(return_value={"20": "Demo App"}))

        await owned_games_module.run_owned_games_check()

        self.assertEqual(source_module.owned_games["steam-user"], {"20": "Demo App"})
        self.assertEqual(api_module.get_game_info.await_count, 0)
        self.assertEqual(FakeUniMessage.sent_messages, [])

    async def test_unknown_type_allows_real_game_and_retries_type_after_short_cache(self):
        fake_time = [100.0]
        self.configure_user({}, {"30": "Possibly Real"})
        self.set_api_mock("get_game_info", AsyncMock(side_effect=[
            {"success": False},
            {"success": True, "data": {"type": "demo"}},
        ]))
        FakeUniMessage.fail_sends = True
        with patch.object(owned_games_module, "time", types.SimpleNamespace(monotonic=lambda: fake_time[0])):
            await owned_games_module.run_owned_games_check()
            self.assertEqual(api_module.get_game_info.await_count, 1)
            self.assertIn("Possibly Real", FakeUniMessage.sent_messages[0][0])
            self.assertEqual(source_module.owned_games["steam-user"], {})

            await owned_games_module.run_owned_games_check()
            self.assertEqual(api_module.get_game_info.await_count, 1)
            self.assertEqual(source_module.owned_games["steam-user"], {})

            fake_time[0] += owned_games_module._DEMO_TYPE_UNKNOWN_TTL + 1
            await owned_games_module.run_owned_games_check()
            self.assertEqual(api_module.get_game_info.await_count, 2)
            self.assertEqual(source_module.owned_games["steam-user"], {"30": "Possibly Real"})

    async def test_confirmed_type_cache_expires_after_24_hours(self):
        fake_time = [100.0]
        self.set_api_mock(
            "get_game_info",
            AsyncMock(return_value={"success": True, "data": {"type": "demo"}}),
        )
        with patch.object(owned_games_module, "time", types.SimpleNamespace(monotonic=lambda: fake_time[0])):
            self.assertTrue(await owned_games_module._is_demo_app("55"))
            self.assertTrue(await owned_games_module._is_demo_app("55"))
            self.assertEqual(api_module.get_game_info.await_count, 1)
            fake_time[0] += owned_games_module._DEMO_TYPE_SUCCESS_TTL + 1
            self.assertTrue(await owned_games_module._is_demo_app("55"))
            self.assertEqual(api_module.get_game_info.await_count, 2)


if __name__ == "__main__":
    unittest.main()
