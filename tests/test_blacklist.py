"""Exercise model operations, administrator protection, and durable state."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_humanlike.main import HumanlikePlugin


async def test_model_can_block_self_and_unblock_from_another_conversation(env):
    blocked = json.loads(
        await env.plugin.humanlike_blacklist(env.event, "block", reason="持续骚扰")
    )
    assert blocked["ok"] and blocked["changed"]
    assert blocked["user_id"] == "user"
    assert await env.plugin.should_ignore(env.native_event(), env.config)
    listed = json.loads(await env.plugin.humanlike_blacklist(env.admin, "list"))
    assert listed["total"] == 1
    entry = listed["entries"][0]
    assert entry["user_id"] == "user"
    assert entry["reason"] == "持续骚扰" and entry["active"]
    assert entry["created_at"].endswith("+00:00")
    result = json.loads(
        await env.plugin.humanlike_blacklist(env.admin, "unblock", "user")
    )
    assert result["ok"] and result["changed"]
    assert not await env.plugin.should_ignore(env.native_event(), env.config)
    assert env.plugin._blacklist == {}


@pytest.mark.parametrize("user_id", ["admin", "other-admin", "42", "bot"])
async def test_cannot_blacklist_admins_from_any_profile_or_the_bot(env, user_id):
    result = json.loads(
        await env.plugin.humanlike_blacklist(env.event, "block", user_id)
    )
    assert not result["ok"]
    assert env.plugin._blacklist == {}


async def test_current_administrator_role_is_also_protected(env):
    event = env.native_event("role-admin", admin=True)
    result = json.loads(await env.plugin.humanlike_blacklist(event, "block"))
    assert not result["ok"]


async def test_live_administrator_promotion_bypasses_existing_record(env):
    await env.plugin.humanlike_blacklist(env.event, "block")
    assert await env.plugin.should_ignore(env.event, env.config)
    env.config["admins_id"].append("user")
    assert not await env.plugin.should_ignore(env.native_event(), env.config)
    listed = json.loads(await env.plugin.humanlike_blacklist(env.admin, "list"))
    assert listed["entries"][0]["active"] is False
    await env.plugin.humanlike_blacklist(env.admin, "unblock", "user")
    assert env.plugin._blacklist == {}


async def test_duplicate_operations_do_not_rewrite_or_replace_reasons(env, monkeypatch):
    await env.plugin.humanlike_blacklist(env.event, "block", reason="original")
    write = AsyncMock(wraps=env.plugin.put_kv_data)
    monkeypatch.setattr(env.plugin, "put_kv_data", write)
    result = json.loads(
        await env.plugin.humanlike_blacklist(env.event, "block", reason="replacement")
    )
    assert result["ok"] and result["changed"] is False
    assert env.plugin._blacklist["qq-main"]["user"]["reason"] == "original"
    absent = json.loads(
        await env.plugin.humanlike_blacklist(env.admin, "unblock", "absent")
    )
    assert absent["ok"] and absent["changed"] is False
    write.assert_not_awaited()


async def test_blacklist_covers_groups_and_private_chat_but_isolates_platforms(env):
    await env.plugin.humanlike_blacklist(env.event, "block")
    for group in ("", "group-a", "group-b"):
        assert await env.plugin.should_ignore(env.native_event(group=group), env.config)
    assert not await env.plugin.should_ignore(
        env.native_event(platform="qq-secondary"), env.config
    )
    listed = json.loads(
        await env.plugin.humanlike_blacklist(
            env.native_event(platform="qq-secondary"), "list"
        )
    )
    assert listed["total"] == 0
    await env.plugin.humanlike_blacklist(
        env.native_event(platform="qq-secondary"), "unblock", "user"
    )
    assert await env.plugin.should_ignore(env.event, env.config)


async def test_actual_database_survives_a_fresh_preferences_instance(env, monkeypatch):
    from astrbot.core.star import session_plugin_manager
    from astrbot.core.utils import plugin_kv_store
    from astrbot.core.utils.shared_preferences import SharedPreferences

    await env.plugin.humanlike_blacklist(env.event, "block", reason="persistent")
    await env.plugin.terminate()
    await env.preferences.close()
    fresh_preferences = SharedPreferences(env.db, env.preferences.path)
    await fresh_preferences.initialize()
    monkeypatch.setattr(plugin_kv_store, "sp", fresh_preferences)
    monkeypatch.setattr(session_plugin_manager, "sp", fresh_preferences)
    reloaded = HumanlikePlugin(env.context)
    reloaded.plugin_id = env.plugin.plugin_id
    try:
        await reloaded.initialize()
        assert await reloaded.should_ignore(env.event, env.config)
        assert reloaded._blacklist["qq-main"]["user"]["reason"] == "persistent"
    finally:
        await reloaded.terminate()
        await fresh_preferences.close()


async def test_storage_failure_does_not_activate_a_block_even_after_reload(
    env, monkeypatch
):
    original = env.db.insert_preference_or_update
    monkeypatch.setattr(
        env.db,
        "insert_preference_or_update",
        AsyncMock(side_effect=OSError("disk full")),
    )
    result = json.loads(await env.plugin.humanlike_blacklist(env.event, "block"))
    assert not result["ok"]
    assert not await env.plugin.should_ignore(env.event, env.config)
    assert await env.plugin.get_kv_data("blacklist", {}) == {}
    monkeypatch.setattr(env.db, "insert_preference_or_update", original)
    await env.plugin.terminate()
    await env.plugin.initialize()
    assert not await env.plugin.should_ignore(env.event, env.config)


async def test_failed_unblock_preserves_the_existing_entry(env, monkeypatch):
    await env.plugin.humanlike_blacklist(env.event, "block")
    monkeypatch.setattr(
        env.db,
        "insert_preference_or_update",
        AsyncMock(side_effect=OSError("disk full")),
    )
    result = json.loads(
        await env.plugin.humanlike_blacklist(env.admin, "unblock", "user")
    )
    assert not result["ok"]
    assert await env.plugin.should_ignore(env.event, env.config)
    stored = await env.plugin.get_kv_data("blacklist", {})
    assert "user" in stored["qq-main"]


async def test_concurrent_tool_calls_do_not_lose_entries(env):
    results = await asyncio.gather(
        *(
            env.plugin.humanlike_blacklist(env.event, "block", f"target-{index}")
            for index in range(12)
        )
    )
    assert all(json.loads(result)["ok"] for result in results)
    stored = await env.plugin.get_kv_data("blacklist", {})
    assert len(stored["qq-main"]) == 12
    assert stored == env.plugin._blacklist


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "clear"},
        {"action": "block", "user_id": 123},
        {"action": "block", "user_id": "two users"},
        {"action": "block", "user_id": "all"},
        {"action": "block", "reason": None},
    ],
)
async def test_invalid_tool_arguments_leave_storage_unchanged(env, kwargs):
    result = json.loads(await env.plugin.humanlike_blacklist(env.event, **kwargs))
    assert not result["ok"]
    assert env.plugin._blacklist == {}


async def test_missing_sender_and_missing_platform_are_rejected(env):
    for event in (env.native_event(""), env.native_event(platform="")):
        result = json.loads(await env.plugin.humanlike_blacklist(event, "block"))
        assert not result["ok"]
    assert env.plugin._blacklist == {}


@pytest.mark.parametrize(
    "saved", [[], {"qq-main": []}, {"qq-main": {"user": {"reason": 123}}}]
)
async def test_corrupt_persistence_never_installs_a_gate_or_erases_data(env, saved):
    from astrbot.core.pipeline.scheduler import PipelineScheduler

    await env.plugin.terminate()
    original = PipelineScheduler._process_stages
    await env.plugin.put_kv_data("blacklist", saved)
    with pytest.raises(ValueError, match="stored"):
        await env.plugin.initialize()
    assert PipelineScheduler._process_stages is original
    assert await env.plugin.get_kv_data("blacklist", {}) == saved
