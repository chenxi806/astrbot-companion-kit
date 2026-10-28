"""梗库插件（AstrBot v4）。

按消息里出现的梗，把对应的短释义注进 prompt：不占世界书名额，也不会一次性塞一大堆。
"""

from __future__ import annotations

import json
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover

    class TextPart:  # type: ignore[no-redef]
        def __init__(self, text: str):
            self.text = text

        def mark_as_temp(self):
            return self


HEADER = "<MemeNote>"
FOOTER = "</MemeNote>"


@register(
    "astrbot_plugin_meme_glossary",
    "local",
    "梗库：按消息里出现的梗注入短释义，让她听得懂也接得住",
    "1.0.0",
)
class MemeGlossary(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._items: list[dict] = []
        self._mtime = 0.0

    def _cfg(self, key: str, default=None):
        value = self.config.get(key, default) if hasattr(self.config, "get") else None
        return default if value is None else value

    def _path(self) -> Path:
        raw = str(self._cfg("memes_path", "")).strip()
        if raw:
            return Path(raw)
        memory = str(self._cfg("memory_dir", "")).strip()
        if not memory:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            memory = str(get_astrbot_data_path() / "companion_memory")
        return Path(memory) / "memes.json"

    def _load(self) -> list[dict]:
        path = self._path()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return []
        if mtime != self._mtime:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._items = data.get("items", [])
                self._mtime = mtime
            except Exception as exc:
                logger.warning(f"[meme_glossary] 读梗库失败: {exc}")
                return []
        return self._items

    def _in_scope(self, event: AstrMessageEvent) -> bool:
        if str(self._cfg("scope", "private")).lower() != "private":
            return True
        try:
            return event.get_message_type() != MessageType.GROUP_MESSAGE
        except Exception:
            return True

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest):
        if not bool(self._cfg("enabled", True)):
            return
        if not self._in_scope(event):
            return
        text = (getattr(event, "message_str", "") or "").strip()
        if not text:
            return

        items = self._load()
        limit = int(self._cfg("max_entries", 2) or 2)
        scored = []
        for item in items:
            matched = [k for k in item.get("keys", []) if k and k in text]
            if matched:
                scored.append((sum(len(k) for k in matched), item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        picked = [item for _, item in scored[:limit]]

        self._strip(req)
        if not picked:
            if any(w in text for w in ("什么梗", "啥梗", "这梗", "听不懂")):
                note = (
                    f"{HEADER}\n他问的这个梗你可能没听过。直接说「这个我还真没听过」，"
                    f"别装懂，也别硬接。\n{FOOTER}"
                )
                req.extra_user_content_parts.append(TextPart(text=note).mark_as_temp())
            return

        lines = [HEADER, "他说到的梗（懂了就自然接，不要解释梗、不要科普来源）："]
        for item in picked:
            lines.append(
                f"- {item.get('term')}：{item.get('meaning')}；用法：{item.get('usage')}"
            )
        lines.append(FOOTER)
        req.extra_user_content_parts.append(TextPart(text="\n".join(lines)).mark_as_temp())

    @staticmethod
    def _strip(req: ProviderRequest) -> None:
        parts = getattr(req, "extra_user_content_parts", None)
        if not parts:
            return
        kept = [p for p in parts if HEADER not in (getattr(p, "text", "") or "")]
        if len(kept) != len(parts):
            req.extra_user_content_parts = kept
