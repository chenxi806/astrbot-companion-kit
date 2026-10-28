"""分段发送：把长回复拆成几条，动作和台词分开，隔一会儿发一条。"""

from __future__ import annotations

import asyncio
import re

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_reply_splitter", "local", "分段发送：长回复拆成几条，动作和台词分开", "1.0.0")
class ReplySplitter(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}

    def _cfg(self, key: str, default=None):
        value = self.config.get(key, default) if hasattr(self.config, "get") else None
        return default if value is None else value

    @filter.on_decorating_result()
    async def split(self, event: AstrMessageEvent):
        if not bool(self._cfg("enabled", True)):
            return
        if str(self._cfg("scope", "private")).lower() == "private":
            try:
                if event.get_message_type() == MessageType.GROUP_MESSAGE:
                    return
            except Exception:
                pass
        result = event.get_result()
        if result is None or not result.chain:
            return
        if any(not isinstance(comp, Plain) for comp in result.chain):
            return

        text = "".join(comp.text for comp in result.chain).strip()
        if len(text) < int(self._cfg("min_chars", 80) or 80):
            return
        if "```" in text or "http://" in text or "https://" in text:
            return
        if "<pc_" in text or "&lt;pc_" in text or "</pc_" in text:
            return

        segments = self._split(text)
        if len(segments) < 2:
            return

        delay = float(self._cfg("delay_seconds", 1.0) or 1.0)
        for segment in segments[:-1]:
            await event.send(event.chain_result([Plain(segment)]))
            await asyncio.sleep(delay)
        event.set_result(event.chain_result([Plain(segments[-1])]))

    @staticmethod
    def _split(text: str) -> list[str]:
        text = text.replace("\r", "")
        if text.startswith("「") and text.endswith("」") and text.count("「") == 1:
            text = text[1:-1].strip()
        # 动作后面紧跟台词时拆开
        text = re.sub(r"）\s*(?=「)", "）\n「", text)
        text = re.sub(r"(?<=[。！？!?…])\s*(?=「)", "\n「", text)

        blocks = [b.strip() for b in text.split("\n") if b.strip()]
        if len(blocks) == 1 and len(blocks[0]) > 80:
            blocks = [b.strip() for b in re.split(r"(?<=[。！？!?…])", blocks[0]) if b.strip()]

        cleaned: list[str] = []
        for block in blocks:
            block = block.strip()
            if block.startswith("」"):
                block = block[1:].lstrip()
            if block.startswith("「") and "」" not in block:
                block = block[1:].lstrip()
            if block.endswith("」") and "「" not in block:
                block = block[:-1].rstrip()
            if not block:
                continue
            if re.fullmatch(r"[「」『』\s]+", block):
                continue
            cleaned.append(block)

        merged: list[str] = []
        for block in cleaned:
            if merged and len(block) < 12 and not block.startswith(("「", "『", "“")):
                merged[-1] = merged[-1] + block
            else:
                merged.append(block)

        # 括号/引号没闭合的段不能单独发，合并到下一段
        balanced: list[str] = []
        buf = ""
        for block in merged:
            candidate = (buf + "\n" + block) if buf else block
            if candidate.count("（") == candidate.count("）") and candidate.count("「") == candidate.count("」"):
                balanced.append(candidate)
                buf = ""
            else:
                buf = candidate
        if buf:
            balanced.append(buf)

        # 超过 5 条时合并相邻最短的两段，绝不丢弃内容
        while len(balanced) > 5:
            idx = min(range(len(balanced) - 1), key=lambda i: len(balanced[i]) + len(balanced[i + 1]))
            balanced[idx] = balanced[idx] + "\n" + balanced[idx + 1]
            del balanced[idx + 1]
        return balanced
