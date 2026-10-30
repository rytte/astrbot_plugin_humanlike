"""Verify interception precedes every native stage and restores cleanly."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_humanlike.main import PLUGIN_NAME


@pytest.mark.parametrize("group", ["", "group-a"])
async def test_blocked_messages_never_enter_waking_or_any_other_stage(env, group):
    from astrbot.api.message_components import At, Image
    from astrbot.core.pipeline.preprocess_stage.stage import PreProcessStage
    from astrbot.core.pipeline.process_stage.stage import ProcessStage
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage

    await env.plugin.humanlike_blacklist(env.event, "block")
    event = env.native_event(group=group)
    event.message_obj.message = [
        At(qq="bot"),
        Image(file="", url="https://invalid.test/image"),
    ]
    scheduler = PipelineScheduler(SimpleNamespace(astrbot_config=env.config))
    scheduler.stages = [WakingCheckStage(), PreProcessStage(), ProcessStage()]
    for stage in scheduler.stages:
        stage.process = AsyncMock()
    await scheduler.execute(event)
    assert event.is_stopped()
    event.send.assert_not_awaited()
    for stage in scheduler.stages:
        stage.process.assert_not_awaited()
    assert event._execution_active is False


async def test_unblocked_messages_keep_the_native_stage_sequence(env):
    sequence = []

    async def first(event):
        sequence.append("first-before")
        yield
        sequence.append("first-after")

    async def second(event):
        sequence.append("second")

    scheduler = PipelineScheduler(SimpleNamespace(astrbot_config=env.config))
    scheduler.stages = [
        SimpleNamespace(process=first),
        SimpleNamespace(process=second),
    ]
    await env.plugin._gate._original(scheduler, env.native_event())
    native_sequence = sequence.copy()
    sequence.clear()
    await scheduler.execute(env.event)
    assert sequence == native_sequence
    assert not env.event.is_stopped()


@pytest.mark.parametrize("plugin_set", [[], ["another-plugin"]])
async def test_profile_exclusion_bypasses_blacklist(env, plugin_set):
    await env.plugin.humanlike_blacklist(env.event, "block")
    env.config["plugin_set"] = plugin_set
    assert not await env.plugin.should_ignore(env.event, env.config)


async def test_explicit_profile_inclusion_enables_blacklist(env):
    await env.plugin.humanlike_blacklist(env.event, "block")
    env.config["plugin_set"] = [PLUGIN_NAME]
    assert await env.plugin.should_ignore(env.event, env.config)


async def test_native_session_disable_and_reenable_are_respected(env):
    await env.plugin.humanlike_blacklist(env.event, "block")
    umo = env.event.unified_msg_origin
    await env.preferences.session_put(
        umo,
        "session_plugin_config",
        {umo: {"disabled_plugins": [PLUGIN_NAME]}},
    )
    assert not await env.plugin.should_ignore(env.event, env.config)
    await env.preferences.session_put(
        umo, "session_plugin_config", {umo: {"disabled_plugins": []}}
    )
    assert await env.plugin.should_ignore(env.event, env.config)


async def test_admin_is_safe_before_waking_assigns_event_role(env):
    await env.plugin.humanlike_blacklist(env.event, "block")
    env.config["admins_id"].append("user")
    event = env.native_event()
    assert not event.is_admin()
    stage = SimpleNamespace(process=AsyncMock())
    scheduler = PipelineScheduler(SimpleNamespace(astrbot_config=env.config))
    scheduler.stages = [stage]
    await scheduler.execute(event)
    stage.process.assert_awaited_once_with(event)
    assert not event.is_stopped()


async def test_admin_promotion_during_session_lookup_is_respected(env, monkeypatch):
    from astrbot.core.star.session_plugin_manager import SessionPluginManager

    await env.plugin.humanlike_blacklist(env.event, "block")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_session_check(umo, name):
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(
        SessionPluginManager, "is_plugin_enabled_for_session", delayed_session_check
    )
    task = asyncio.create_task(env.plugin.should_ignore(env.event, env.config))
    await asyncio.wait_for(entered.wait(), 5)
    env.config["admins_id"].append("user")
    release.set()
    assert await asyncio.wait_for(task, 5) is False


async def test_blocked_webchat_still_closes_the_native_response_stream(env):
    from astrbot.core.platform.sources.webchat.webchat_event import WebChatMessageEvent

    await env.plugin.humanlike_blacklist(env.event, "block")
    source = env.native_event()
    event = WebChatMessageEvent(
        source.message_str,
        source.message_obj,
        source.platform_meta,
        source.session_id,
    )
    event.send = AsyncMock()
    stage = SimpleNamespace(process=AsyncMock())
    scheduler = PipelineScheduler(SimpleNamespace(astrbot_config=env.config))
    scheduler.stages = [stage]
    await scheduler.execute(event)
    stage.process.assert_not_awaited()
    event.send.assert_awaited_once_with(None)
    assert event._execution_active is False


async def test_plugin_disable_restores_original_dispatcher_and_retains_data(env):
    original = env.plugin._gate._original
    await env.plugin.humanlike_blacklist(env.event, "block")
    await env.plugin.terminate()
    assert PipelineScheduler._process_stages is original
    stage = SimpleNamespace(process=AsyncMock())
    scheduler = PipelineScheduler(SimpleNamespace(astrbot_config=env.config))
    scheduler.stages = [stage]
    await scheduler.execute(env.event)
    stage.process.assert_awaited_once()
    denied = json.loads(
        await env.plugin.humanlike_blacklist(env.admin, "unblock", "user")
    )
    assert not denied["ok"]
    await env.plugin.initialize()
    assert await env.plugin.should_ignore(env.event, env.config)


async def test_initialize_does_not_stack_multiple_wrappers(env):
    installed = PipelineScheduler._process_stages
    await env.plugin.initialize()
    assert PipelineScheduler._process_stages is installed


async def test_unload_preserves_another_plugins_later_wrapper(env, monkeypatch):
    await env.plugin.humanlike_blacklist(env.event, "block")
    our_wrapper = PipelineScheduler._process_stages

    async def other_wrapper(scheduler, event, from_stage=0):
        await our_wrapper(scheduler, event, from_stage)

    with monkeypatch.context() as patches:
        patches.setattr(PipelineScheduler, "_process_stages", other_wrapper)
        await env.plugin.terminate()
        assert PipelineScheduler._process_stages is other_wrapper
        stage = SimpleNamespace(process=AsyncMock())
        scheduler = PipelineScheduler(SimpleNamespace(astrbot_config=env.config))
        scheduler.stages = [stage]
        await scheduler.execute(env.event)
        stage.process.assert_awaited_once()
    env.plugin._gate.remove()


async def test_disable_during_an_await_does_not_stop_an_incoming_message(
    env, monkeypatch
):
    await env.plugin.humanlike_blacklist(env.event, "block")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_check(event, config):
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(env.plugin._gate, "should_ignore", delayed_check)
    stage = SimpleNamespace(process=AsyncMock())
    scheduler = PipelineScheduler(SimpleNamespace(astrbot_config=env.config))
    scheduler.stages = [stage]
    task = asyncio.create_task(scheduler.execute(env.event))
    await asyncio.wait_for(entered.wait(), 5)
    await env.plugin.terminate()
    release.set()
    await asyncio.wait_for(task, 5)
    stage.process.assert_awaited_once()
    assert not env.event.is_stopped()


def test_tool_is_registered_with_the_native_function_calling_schema():
    tool = llm_tools.get_func("humanlike_blacklist")
    assert tool is not None
    assert set(tool.parameters["properties"]) == {"action", "user_id", "reason"}
    assert all(
        item["type"] == "string" for item in tool.parameters["properties"].values()
    )
    assert "管理员" in tool.description
