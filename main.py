"""Model-directed humanlike behaviors with a persistent user blacklist."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.session_plugin_manager import SessionPluginManager

from .gate import EarlyMessageGate

PLUGIN_NAME = "astrbot_plugin_humanlike"
STORAGE_KEY = "blacklist"


def tool_result(ok: bool, message: str, **data) -> str:
    """Serialize a tool result as readable, structured JSON."""
    return json.dumps({"ok": ok, "message": message, **data}, ensure_ascii=False)


class HumanlikePlugin(Star):
    """拟人化行为：由模型管理黑名单，并在消息处理前忽略被拉黑用户。"""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._blacklist: dict[str, dict[str, dict[str, str]]] = {}
        self._lock = asyncio.Lock()
        self._gate = EarlyMessageGate(self.should_ignore)

    async def initialize(self) -> None:
        """Load persistent entries before enabling message interception."""
        if self._gate.active:
            return
        saved = await self.get_kv_data(STORAGE_KEY, {})
        if not isinstance(saved, dict):
            raise ValueError("The stored humanlike blacklist must be an object.")
        for platform_id, users in saved.items():
            if not isinstance(platform_id, str) or not platform_id:
                raise ValueError("The stored blacklist has an invalid platform ID.")
            if not isinstance(users, dict):
                raise ValueError("The stored blacklist users must be an object.")
            for user_id, entry in users.items():
                if (
                    not isinstance(user_id, str)
                    or not user_id
                    or not isinstance(entry, dict)
                    or not isinstance(entry.get("reason"), str)
                    or not isinstance(entry.get("created_at"), str)
                ):
                    raise ValueError("The stored blacklist has an invalid user entry.")
        self._blacklist = saved
        self._gate.install()

    def is_protected_user(self, event: AstrMessageEvent, user_id: str) -> bool:
        """Protect the bot and administrators from every loaded profile.

        Args:
            event: Native event identifying the bot and the authenticated caller.
            user_id: Exact platform user ID to check.

        Returns:
            Whether this user must never be ignored by the blacklist.
        """
        if user_id == str(event.get_self_id()):
            return True
        if user_id == event.get_sender_id() and event.is_admin():
            return True
        configs = self.context.astrbot_config_mgr.confs.values()
        return any(
            user_id == str(admin_id).strip()
            for config in configs
            for admin_id in config.get("admins_id", [])
        )

    async def should_ignore(self, event: AstrMessageEvent, config: dict) -> bool:
        """Check membership before any pipeline stages run.

        Args:
            event: Incoming message, before waking checks assign its role.
            config: The scheduler's selected AstrBot configuration profile.

        Returns:
            Whether this message should end without entering the pipeline.
        """
        platform_id = event.get_platform_id()
        sender_id = event.get_sender_id()
        if not sender_id or sender_id not in self._blacklist.get(platform_id, {}):
            return False
        if self.is_protected_user(event, sender_id):
            return False
        plugin_set = config.get("plugin_set", ["*"])
        if plugin_set != ["*"] and PLUGIN_NAME not in plugin_set:
            return False
        enabled = await SessionPluginManager.is_plugin_enabled_for_session(
            event.unified_msg_origin, PLUGIN_NAME
        )
        return (
            enabled
            and sender_id in self._blacklist.get(platform_id, {})
            and not self.is_protected_user(event, sender_id)
        )

    @filter.llm_tool(name="humanlike_blacklist")
    async def humanlike_blacklist(
        self,
        event: AstrMessageEvent,
        action: str,
        user_id: str = "",
        reason: str = "",
    ) -> str:
        """管理机器人自己的用户黑名单。可根据人设与实际互动决定拉黑或解除。

        拉黑后静默忽略该用户在当前机器人平台的所有群聊和私聊新消息。
        这不是 QQ 好友黑名单、群禁言或踢人，不会调用平台账号管理接口。
        管理员和机器人自身不能被拉黑。只使用事件或可信查询提供的真实用户 ID，
        不要猜测 ID，不要把昵称或 @ 文本作为 ID。被拉黑者无法通过新消息自行解除。

        Args:
            action(string): block 拉黑、unblock 解除拉黑、list 查询当前平台的黑名单。
            user_id(string): 拉黑或解除的真实用户 ID；留空表示当前消息发送者。list 时留空。
            reason(string): 可选的拉黑原因；仅在 block 时保存。

        Returns:
            JSON containing the outcome and current-platform blacklist information.
        """
        if not self._gate.active:
            return tool_result(False, "拟人化行为插件尚未就绪或已停用。")
        if action not in ("block", "unblock", "list"):
            return tool_result(False, "action 仅支持 block、unblock、list。")
        if not isinstance(user_id, str) or not isinstance(reason, str):
            return tool_result(False, "user_id 和 reason 必须是字符串。")
        platform_id = event.get_platform_id()
        if not isinstance(platform_id, str) or not platform_id:
            return tool_result(False, "无法确定当前机器人平台，未修改黑名单。")
        target_id = user_id.strip() or event.get_sender_id()
        if action != "list" and (
            not target_id
            or target_id == "all"
            or any(character.isspace() for character in target_id)
        ):
            return tool_result(False, "请提供一个真实且非空的用户 ID。")

        async with self._lock:
            if not self._gate.active:
                return tool_result(False, "拟人化行为插件已停用，未修改黑名单。")
            users = self._blacklist.get(platform_id, {})
            if action == "list":
                entries = [
                    {
                        "user_id": uid,
                        **entry,
                        "active": not self.is_protected_user(event, uid),
                    }
                    for uid, entry in sorted(users.items())
                ]
                return tool_result(
                    True,
                    "已查询当前平台黑名单；active=false 表示因管理员或机器人保护而不生效。",
                    platform_id=platform_id,
                    entries=entries,
                    total=len(entries),
                )
            if action == "block" and self.is_protected_user(event, target_id):
                return tool_result(False, "不能拉黑 AstrBot 管理员或机器人自身。")
            if (action == "block") == (target_id in users):
                return tool_result(
                    True,
                    "该用户已在黑名单中。"
                    if action == "block"
                    else "该用户不在黑名单中。",
                    platform_id=platform_id,
                    user_id=target_id,
                    changed=False,
                )

            # Publish a new snapshot only after durable storage succeeds.
            updated_users = users.copy()
            updated = self._blacklist.copy()
            if action == "block":
                updated_users[target_id] = {
                    "reason": reason.strip(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                updated[platform_id] = updated_users
            else:
                del updated_users[target_id]
                if updated_users:
                    updated[platform_id] = updated_users
                else:
                    updated.pop(platform_id, None)
            try:
                await self.put_kv_data(STORAGE_KEY, updated)
            except Exception:
                self.logger.exception("Failed to persist the humanlike blacklist")
                # AstrBot updates its KV cache before committing the database.
                try:
                    await self.put_kv_data(STORAGE_KEY, self._blacklist)
                except Exception:
                    self.logger.exception("Failed to restore the blacklist KV snapshot")
                return tool_result(
                    False, "保存黑名单失败，本次操作未生效，请稍后重试。"
                )
            self._blacklist = updated
            return tool_result(
                True,
                "已拉黑该用户，后续新消息会被静默忽略。"
                if action == "block"
                else "已解除拉黑，后续新消息可以正常处理。",
                platform_id=platform_id,
                user_id=target_id,
                changed=True,
            )

    async def terminate(self) -> None:
        """Restore message processing while retaining persistent blacklist data."""
        async with self._lock:
            self._gate.remove()
