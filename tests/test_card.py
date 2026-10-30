import importlib.util
import importlib.machinery
import asyncio
import json
from pathlib import Path
import sys
import types
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, patch
import unittest


card_module_path = Path(__file__).resolve().parents[1] / "nonebot_plugin_steam_game_status" / "card.py"
card_spec = importlib.util.spec_from_file_location("card", card_module_path)
assert card_spec and card_spec.loader
card = importlib.util.module_from_spec(card_spec)
card_spec.loader.exec_module(card)


class TestSteamCardCache(unittest.TestCase):
    def test_cache_key_includes_player_name(self):
        base = {
            "avatar_url": "https://example.com/avatar.gif",
            "action_text": "开始玩",
            "game_name": "Game",
            "template_digest": "template",
        }

        key_a = card.build_steam_card_cache_key(player_name="Alice", **base)
        key_b = card.build_steam_card_cache_key(player_name="Bob", **base)

        self.assertNotEqual(key_a, key_b)

    def test_cache_key_includes_action_text(self):
        base = {
            "avatar_url": "https://example.com/avatar.gif",
            "player_name": "Alice",
            "game_name": "Game",
            "template_digest": "template",
        }

        start_key = card.build_steam_card_cache_key(action_text="开始玩", **base)
        stop_key = card.build_steam_card_cache_key(action_text="停止", **base)

        self.assertNotEqual(start_key, stop_key)

    def test_cache_key_includes_frame_duration(self):
        base = {
            "avatar_url": "https://example.com/avatar.gif",
            "player_name": "Alice",
            "action_text": "开始玩",
            "game_name": "Game",
            "template_digest": "template",
        }

        fast_key = card.build_steam_card_cache_key(frame_duration_ms=80, **base)
        slow_key = card.build_steam_card_cache_key(frame_duration_ms=120, **base)

        self.assertNotEqual(fast_key, slow_key)

    def test_cache_key_includes_capture_interval(self):
        base = {
            "avatar_url": "https://example.com/avatar.gif",
            "player_name": "Alice",
            "action_text": "开始玩",
            "game_name": "Game",
            "template_digest": "template",
        }

        dense_key = card.build_steam_card_cache_key(capture_interval_ms=80, **base)
        sparse_key = card.build_steam_card_cache_key(capture_interval_ms=150, **base)

        self.assertNotEqual(dense_key, sparse_key)

    def test_cache_key_includes_capture_duration(self):
        base = {
            "avatar_url": "https://example.com/avatar.gif",
            "player_name": "Alice",
            "action_text": "开始玩",
            "game_name": "Game",
            "template_digest": "template",
        }

        short_key = card.build_steam_card_cache_key(capture_duration_ms=1800, **base)
        full_key = card.build_steam_card_cache_key(capture_duration_ms=4000, **base)

        self.assertNotEqual(short_key, full_key)

    def test_cache_key_includes_card_class(self):
        base = {
            "avatar_url": "https://example.com/avatar.gif",
            "player_name": "Alice",
            "action_text": "开始玩",
            "game_name": "Game",
            "template_digest": "template",
        }

        normal_key = card.build_steam_card_cache_key(card_class="", **base)
        compact_key = card.build_steam_card_cache_key(card_class="compact", **base)

        self.assertNotEqual(normal_key, compact_key)

    def test_cache_key_includes_preserve_avatar_timing(self):
        base = {
            "avatar_url": "https://example.com/avatar.gif",
            "player_name": "Alice",
            "action_text": "开始玩",
            "game_name": "Game",
            "template_digest": "template",
        }

        sampled_key = card.build_steam_card_cache_key(preserve_avatar_timing=False, **base)
        preserved_key = card.build_steam_card_cache_key(preserve_avatar_timing=True, **base)

        self.assertNotEqual(sampled_key, preserved_key)

    def test_cache_key_includes_max_avatar_frames(self):
        base = {
            "avatar_url": "https://example.com/avatar.gif",
            "player_name": "Alice",
            "action_text": "开始玩",
            "game_name": "Game",
            "template_digest": "template",
            "preserve_avatar_timing": True,
        }

        small_key = card.build_steam_card_cache_key(max_avatar_frames=60, **base)
        large_key = card.build_steam_card_cache_key(max_avatar_frames=120, **base)

        self.assertNotEqual(small_key, large_key)

    def test_cache_key_includes_background_url(self):
        base = {
            "avatar_url": "https://example.com/avatar.gif",
            "player_name": "Alice",
            "action_text": "start",
            "game_name": "Game",
            "template_digest": "template",
        }

        plain_key = card.build_steam_card_cache_key(background_url="", **base)
        background_key = card.build_steam_card_cache_key(background_url="https://example.com/bg.jpg", **base)

        self.assertNotEqual(plain_key, background_key)

    def test_detects_gif_avatar_url(self):
        self.assertTrue(card.is_animated_image_url("https://example.com/avatar.GIF?size=small"))
        self.assertFalse(card.is_animated_image_url("https://example.com/avatar.jpg"))

    def test_builds_game_background_url(self):
        self.assertIn("/123/library_hero.jpg", card.build_steam_game_background_url("123"))
        self.assertEqual(card.build_steam_game_background_url(""), "")

    def test_dynamic_card_uses_compact_layout(self):
        card_class, width, height = card.get_steam_card_layout("Game", dynamic=True)

        self.assertEqual(card_class, "compact")
        self.assertEqual(width, card.STEAM_CARD_COMPACT_VIEWPORT_WIDTH)
        self.assertEqual(height, card.STEAM_CARD_COMPACT_VIEWPORT_HEIGHT)

    def test_long_static_game_uses_wide_layout(self):
        card_class, width, height = card.get_steam_card_layout("Very Very Long Game Name")

        self.assertEqual(card_class, "wide")
        self.assertEqual(width, card.STEAM_CARD_WIDE_VIEWPORT_WIDTH)
        self.assertEqual(height, card.STEAM_CARD_WIDE_VIEWPORT_HEIGHT)


class TestSteamBackgroundResolution(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        card._BACKGROUND_CACHE.clear()
        card._BACKGROUND_LAST_VALID.clear()
        card._BACKGROUND_LOCKS.clear()

    def test_parses_hashed_and_legacy_asset_formats(self):
        hero = "bd7b837949f6eac2f8c8f6825047563fe706fdc4/library_hero.jpg"
        hashed = {
            "response": {"store_items": [{"appid": 4731980, "success": 1, "assets": {
                "library_hero": hero,
                "asset_url_format": "steam/apps/4731980/${FILENAME}?t=1789492417",
            }}]}
        }
        self.assertEqual(
            card._parse_store_item_background(hashed, "4731980"),
            "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/4731980/"
            + hero + "?t=1789492417",
        )
        legacy = {"response": {"store_items": [{"appid": 123, "success": 1, "assets": {
            "library_hero_2x": "library_hero_2x.jpg",
            "asset_url_format": "steam/apps/123/${FILENAME}",
        }}]}}
        self.assertEqual(
            card._parse_store_item_background(legacy, "123"),
            "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/123/library_hero_2x.jpg",
        )

    def test_rejects_other_app_and_missing_fields(self):
        other_app = {"response": {"store_items": [{"appid": 456, "success": 1, "assets": {
            "library_hero": "hero.jpg", "asset_url_format": "${FILENAME}",
        }}]}}
        missing = {"response": {"store_items": [{"appid": 123, "success": 1, "assets": {
            "library_hero": "hero.jpg",
        }}]}}
        no_placeholder = {"response": {"store_items": [{"appid": 123, "success": 1, "assets": {
            "library_hero": "hero.jpg", "asset_url_format": "steam/apps/123/",
        }}]}}
        self.assertEqual(card._parse_store_item_background(other_app, "123"), "")
        self.assertEqual(card._parse_store_item_background(missing, "123"), "")
        self.assertEqual(card._parse_store_item_background(no_placeholder, "123"), "")
        self.assertEqual(card._parse_store_item_background([], "123"), "")
        self.assertEqual(card._parse_store_item_background({"response": []}, "123"), "")

    async def _run_fetch_with_response(self, status, content):
        request_calls = []

        class FakeRequest:
            def __init__(self, method, url, **kwargs):
                request_calls.append((method, url, kwargs))

        class FakeSafeResponse:
            def __init__(self, response):
                self.status_code = response.status_code
                self.content = response.content

        class FakeClient:
            async def request(self, request):
                return types.SimpleNamespace(status_code=status, content=content)

        class FakeClientContext:
            async def __aenter__(self):
                return FakeClient()

            async def __aexit__(self, *args):
                return None

        fake_package = types.ModuleType("nonebot_plugin_steam_game_status")
        fake_package.__path__ = []
        fake_nonebot = types.ModuleType("nonebot")
        fake_nonebot_internal = types.ModuleType("nonebot.internal")
        fake_driver = types.ModuleType("nonebot.internal.driver")
        fake_driver.Request = FakeRequest
        config = types.ModuleType("nonebot_plugin_steam_game_status.config")
        config.get_steam_api_domain = lambda: "api.example.test"
        model = types.ModuleType("nonebot_plugin_steam_game_status.model")
        model.SafeResponse = FakeSafeResponse
        utils = types.ModuleType("nonebot_plugin_steam_game_status.utils")
        utils.http_client = FakeClientContext
        modules = {
            "nonebot": fake_nonebot,
            "nonebot.internal": fake_nonebot_internal,
            "nonebot.internal.driver": fake_driver,
            "nonebot_plugin_steam_game_status": fake_package,
            "nonebot_plugin_steam_game_status.config": config,
            "nonebot_plugin_steam_game_status.model": model,
            "nonebot_plugin_steam_game_status.utils": utils,
        }
        with (
            patch.object(card, "__package__", "nonebot_plugin_steam_game_status"),
            patch.object(
                card,
                "__spec__",
                importlib.machinery.ModuleSpec("nonebot_plugin_steam_game_status.card", loader=None),
            ),
            patch.dict(sys.modules, modules),
        ):
                result = await card._fetch_steam_game_background_url("123")
        return result, request_calls

    async def test_fetch_accepts_json_string_and_bytes_and_builds_request(self):
        payload = {"response": {"store_items": [{"appid": 123, "success": 1, "assets": {
            "library_hero": "hash/library_hero.jpg", "asset_url_format": "steam/apps/123/${FILENAME}?t=1",
        }}]}}
        expected = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/123/hash/library_hero.jpg?t=1"
        for content in (json.dumps(payload), json.dumps(payload).encode()):
            result, calls = await self._run_fetch_with_response(200, content)
            self.assertEqual(result, expected)
            self.assertEqual(calls[0][0], "GET")
            self.assertEqual(calls[0][2], {"timeout": 5})
            parsed_url = urlparse(calls[0][1])
            self.assertEqual(parsed_url.netloc, "api.example.test")
            request_data = json.loads(parse_qs(parsed_url.query)["input_json"][0])
            self.assertEqual(request_data["ids"], [{"appid": 123}])
            self.assertEqual(request_data["context"], {"language": "english", "country_code": "US"})
            self.assertEqual(request_data["data_request"], {"include_assets": True})

    async def test_fetch_handles_http_error_and_invalid_json(self):
        for status, content in ((503, b"{}"), (200, b"not json")):
            result, _ = await self._run_fetch_with_response(status, content)
            self.assertEqual(result, "")

    async def test_invalid_appid_does_not_fetch(self):
        with patch.object(card, "_fetch_steam_game_background_url", new_callable=AsyncMock) as fetch:
            self.assertEqual(await card.resolve_steam_game_background_url("0"), "")
            self.assertEqual(await card.resolve_steam_game_background_url("abc"), "")
            fetch.assert_not_awaited()

    async def test_appid_is_normalized_before_lookup_and_cache(self):
        resolved = "https://example.com/hero.jpg"
        with patch.object(card, "_fetch_steam_game_background_url", new_callable=AsyncMock, return_value=resolved) as fetch:
            self.assertEqual(await card.resolve_steam_game_background_url(123), resolved)
            self.assertEqual(await card.resolve_steam_game_background_url("00123"), resolved)
            fetch.assert_awaited_once_with("123")
        self.assertIn("123", card._BACKGROUND_CACHE)
        self.assertNotIn("00123", card._BACKGROUND_CACHE)

    async def test_fallback_on_missing_and_http_exception(self):
        with patch.object(card, "_fetch_steam_game_background_url", new_callable=AsyncMock, return_value=""):
            self.assertEqual(
                await card.resolve_steam_game_background_url("123"),
                card.build_steam_game_background_url("123"),
            )
        card._BACKGROUND_CACHE.clear()
        with patch.object(card, "_fetch_steam_game_background_url", new_callable=AsyncMock, side_effect=OSError("offline")):
            self.assertEqual(
                await card.resolve_steam_game_background_url("123"),
                card.build_steam_game_background_url("123"),
            )

    async def test_success_cache_and_expiry_retry(self):
        fake_time = [100.0]
        url = "https://example.com/hash/library_hero.jpg"
        with patch.object(card.time, "monotonic", side_effect=lambda: fake_time[0]), patch.object(
            card, "_fetch_steam_game_background_url", new_callable=AsyncMock, return_value=url
        ) as fetch:
            self.assertEqual(await card.resolve_steam_game_background_url("123"), url)
            self.assertEqual(await card.resolve_steam_game_background_url("123"), url)
            fetch.assert_awaited_once()
            fake_time[0] += card._BACKGROUND_SUCCESS_TTL + 1
            self.assertEqual(await card.resolve_steam_game_background_url("123"), url)
            self.assertEqual(fetch.await_count, 2)

    async def test_failure_cache_and_retry_after_expiry(self):
        fake_time = [100.0]
        with patch.object(card.time, "monotonic", side_effect=lambda: fake_time[0]), patch.object(
            card, "_fetch_steam_game_background_url", new_callable=AsyncMock, return_value=""
        ) as fetch:
            await card.resolve_steam_game_background_url("123")
            await card.resolve_steam_game_background_url("123")
            fetch.assert_awaited_once()
            fake_time[0] += card._BACKGROUND_FAILURE_TTL + 1
            await card.resolve_steam_game_background_url("123")
            self.assertEqual(fetch.await_count, 2)

    async def test_concurrent_requests_are_deduplicated(self):
        url = "https://example.com/hero.jpg"
        async def delayed_fetch(appid):
            await asyncio.sleep(0.01)
            return url

        with patch.object(card, "_fetch_steam_game_background_url", new_callable=AsyncMock, side_effect=delayed_fetch) as fetch:
            results = await asyncio.gather(*(card.resolve_steam_game_background_url("123") for _ in range(8)))
        self.assertEqual(results, [url] * 8)
        fetch.assert_awaited_once()

    async def test_refresh_failure_keeps_previous_valid_url(self):
        fake_time = [100.0]
        url = "https://example.com/hash/library_hero.jpg"
        with patch.object(card.time, "monotonic", side_effect=lambda: fake_time[0]), patch.object(
            card, "_fetch_steam_game_background_url", new_callable=AsyncMock, side_effect=[url, OSError("offline")]
        ) as fetch:
            self.assertEqual(await card.resolve_steam_game_background_url("123"), url)
            fake_time[0] += card._BACKGROUND_SUCCESS_TTL + 1
            self.assertEqual(await card.resolve_steam_game_background_url("123"), url)
            self.assertEqual(await card.resolve_steam_game_background_url("123"), url)
            self.assertEqual(fetch.await_count, 2)

if __name__ == "__main__":
    unittest.main()
