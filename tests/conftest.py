"""Test the plugin against native AstrBot events and persistent storage."""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("ASTRBOT_SOURCE", PLUGIN.parent / "AstrBot"))
sys.path.insert(0, str(PLUGIN.parent))
sys.path.insert(0, str(SOURCE))
TEST_ROOT = tempfile.TemporaryDirectory(prefix="humanlike-tests-")
os.environ["ASTRBOT_ROOT"] = TEST_ROOT.name


def native_event(user="user", *, platform="qq-main", group="", admin=False):
    """Create a native event with a real trace, identity, and result lifecycle."""
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.message_components import Plain
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata

    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    message.sender = MessageMember(user_id=user, nickname=user)
    message.self_id = "bot"
    message.group_id = group
    message.session_id = group or user
    message.message_id = "native-message"
    message.message_str = "你好"
    message.message = [Plain("你好")]
    message.raw_message = {"post_type": "message"}
    event = AstrMessageEvent(
        message.message_str,
        message,
        PlatformMetadata("aiocqhttp", "QQ", platform),
        message.session_id,
    )
    event.role = "admin" if admin else "member"
    event.send = AsyncMock()
    return event


@pytest.fixture
async def env(tmp_path, monkeypatch):
    from astrbot.core.db.sqlite import SQLiteDatabase
    from astrbot.core.star import session_plugin_manager
    from astrbot.core.star.context import Context
    from astrbot.core.utils import plugin_kv_store
    from astrbot.core.utils.shared_preferences import SharedPreferences
    from astrbot_plugin_humanlike.main import HumanlikePlugin

    database = SQLiteDatabase(str(tmp_path / "core.sqlite3"))
    await database.initialize()
    preferences = SharedPreferences(database, str(tmp_path / "preferences.json"))
    await preferences.initialize()
    monkeypatch.setattr(plugin_kv_store, "sp", preferences)
    monkeypatch.setattr(session_plugin_manager, "sp", preferences)
    config = {"admins_id": ["admin"], "plugin_set": ["*"]}
    context = Context.__new__(Context)
    context._config = config
    context.astrbot_config_mgr = SimpleNamespace(
        confs={"default": config, "secondary": {"admins_id": ["other-admin", 42]}}
    )
    plugin = HumanlikePlugin(context)
    plugin.plugin_id = "unknown/astrbot_plugin_humanlike"
    await plugin.initialize()
    yield SimpleNamespace(
        plugin=plugin,
        config=config,
        context=context,
        db=database,
        preferences=preferences,
        native_event=native_event,
        event=native_event(),
        admin=native_event("admin", admin=True),
    )
    await plugin.terminate()
    await preferences.close()
    await database.engine.dispose()
