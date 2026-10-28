# -*- coding: utf-8 -*-
"""语音开关：/语音 开|关|状态，用于随时静音/恢复语音消息。"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

VOICE_PLUGIN = "astrbot_plugin_mimo_tts_clone"


@register("voice_switch", "local", "语音开关：/语音 开|关|状态", "1.0.0")
class VoiceSwitch(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    def _manager(self):
        return getattr(self.context, "_star_manager", None)

    def _is_active(self) -> bool:
        try:
            plugin = self.context.get_registered_star(VOICE_PLUGIN)
            return bool(plugin and getattr(plugin, "activated", False))
        except Exception:
            return False

    async def _set(self, enabled: bool) -> str:
        manager = self._manager()
        if manager is None:
            return "插件管理器不可用，没法切换。"
        try:
            if enabled:
                await manager.turn_on_plugin(VOICE_PLUGIN)
                return "语音开了。她可以正常发语音了。"
            await manager.turn_off_plugin(VOICE_PLUGIN)
            return "语音关了。以后她只打字，不再发语音。"
        except Exception as exc:
            logger.warning(f"[voice_switch] toggle failed: {exc}")
            return f"切换失败：{exc}"

    @filter.command("语音")
    async def voice_command(self, event: AstrMessageEvent):
        text = (getattr(event, "message_str", "") or "").strip()
        arg = text.replace("/语音", "", 1).strip().lower()
        if arg in ("关", "关闭", "off", "静音", "别发了"):
            yield event.plain_result(await self._set(False))
        elif arg in ("开", "打开", "on", "恢复"):
            yield event.plain_result(await self._set(True))
        else:
            yield event.plain_result(
                f"语音现在{'开着' if self._is_active() else '关着'}。发「/语音 关」静音，发「/语音 开」恢复。"
            )

    @filter.command("语音关")
    async def voice_off(self, event: AstrMessageEvent):
        yield event.plain_result(await self._set(False))

    @filter.command("语音开")
    async def voice_on(self, event: AstrMessageEvent):
        yield event.plain_result(await self._set(True))
