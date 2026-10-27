"""世界书插件（AstrBot v4）。

把人格里"不常驻"的部分拆成带关键词和概率的条目，命中才注入：
- 关键词命中 + 概率掷骰 + 冷却时间 + 最多注入 N 条
- 只在私聊生效（群聊另有条目，默认关闭）
- 和判断卡一样走 extra_user_content_parts 通道，不塞 system（会被人格覆盖）

命令：
- /lore            看条目数量和启用状态
- /lore list       列出全部条目
- /lore test 文本   试匹配，看这句话会注入哪几条（忽略概率）
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

try:
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover

    class TextPart:  # type: ignore[no-redef]
        def __init__(self, text: str):
            self.text = text

        def mark_as_temp(self):
            return self


DEFAULT_LORE_PATH = ""


def _default_memory_dir() -> Path:
    """默认记忆目录：<AstrBot data>/companion_memory。

    想换位置就在插件配置里填 memory_dir（所有插件填同一个）。
    """
    try:
        base = Path(StarTools.get_data_dir("astrbot_plugin_lorebook"))
        return base.parent.parent / "companion_memory"
    except Exception:
        return Path.home() / "companion_memory"

HEADER = "<Lorebook>"
FOOTER = "</Lorebook>"


@register(
    "astrbot_plugin_lorebook",
    "local",
    "世界书：按关键词和概率注入人格条目，让设定更厚、行为更随机",
    "1.0.0",
)
class LorebookPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._lock = asyncio.Lock()
        self._cache_key: tuple[int, int] | None = None
        self._cache_entries: list[dict] = []

    # ---------- 配置与文件 ----------

    def _cfg(self, key: str, default):
        try:
            value = self.config.get(key, default)
        except AttributeError:
            return default
        return default if value is None else value

    def _lore_path(self) -> Path:
        raw = str(self._cfg("lore_path", "")).strip()
        return Path(raw) if raw else _default_memory_dir() / "lorebook.json"

    def _state_path(self) -> Path:
        try:
            base = Path(StarTools.get_data_dir("astrbot_plugin_lorebook"))
        except Exception:
            base = self._lore_path().parent / "lore_state"
        base.mkdir(parents=True, exist_ok=True)
        return base / "state.json"

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_path().read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            self._state_path().write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning(f"[lorebook] 保存状态失败: {exc}")

    def _load_entries(self) -> list[dict]:
        path = self._lore_path()
        try:
            stat = path.stat()
        except OSError:
            return []
        key = (stat.st_mtime_ns, stat.st_size)
        if key == self._cache_key:
            return self._cache_entries
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = [e for e in data.get("entries", []) if isinstance(e, dict)]
        except Exception as exc:
            logger.warning(f"[lorebook] 读取 {path} 失败: {exc}")
            return self._cache_entries
        self._cache_key, self._cache_entries = key, entries
        logger.info(f"[lorebook] 已加载 {len(entries)} 条世界书条目")
        return entries

    # ---------- 选择 ----------

    def _select(
        self,
        text: str,
        is_group: bool,
        *,
        ignore_probability: bool = False,
        now: float | None = None,
    ) -> list[dict]:
        now = time.time() if now is None else now
        state = self._load_state()
        picked: list[tuple[int, int, dict]] = []

        for entry in self._load_entries():
            group_only = bool(entry.get("group_only", False))
            if is_group != group_only:
                continue
            keys = entry.get("keys") or []
            matched = [k for k in keys if k and k in text]
            if not matched and not entry.get("random_pool") and not group_only:
                continue
            if ignore_probability and entry.get("random_pool"):
                continue  # 试匹配只看关键词条目
            cooldown = int(entry.get("cooldown_minutes", 0) or 0)
            last = float(state.get(str(entry.get("id")), 0) or 0)
            if cooldown and now - last < cooldown * 60:
                continue
            if not ignore_probability:
                try:
                    probability = float(entry.get("probability", 1.0))
                except Exception:
                    probability = 1.0
                if random.random() > probability:
                    continue
            picked.append((int(entry.get("priority", 1)), len(matched), entry))

        picked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        limit = int(self._cfg("max_entries", 3))
        return [entry for _, _, entry in picked[: max(1, limit)]]

    # ---------- 注入 ----------

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest):
        if not bool(self._cfg("enabled", True)):
            return
        scope = str(self._cfg("scope", "private")).strip().lower()
        is_group = False
        try:
            is_group = event.get_message_type() == MessageType.GROUP_MESSAGE
        except Exception:
            pass
        if scope == "private" and is_group:
            return
        if not is_group and self._cfg("allowed_umos", []):
            allowed = self._cfg("allowed_umos", [])
            if isinstance(allowed, str):
                allowed = [x.strip() for x in allowed.splitlines() if x.strip()]
            if getattr(event, "unified_msg_origin", "") not in allowed:
                return

        text = (getattr(event, "message_str", "") or "").strip()
        if not text:
            return

        async with self._lock:
            chosen = self._select(text, is_group)
            if not chosen:
                return
            state = self._load_state()
            now = time.time()
            for entry in chosen:
                state[str(entry.get("id"))] = now
            self._save_state(state)

        body = "\n".join(f"- {entry.get('content', '').strip()}" for entry in chosen)
        preamble = "（你此刻想起的设定，自然用，不要复述，也不要提到「设定」「条目」）"
        self._strip_previous(req)
        req.extra_user_content_parts.append(
            TextPart(text=f"{HEADER}\n{preamble}\n{body}\n{FOOTER}").mark_as_temp()
        )
        logger.debug(f"[lorebook] 注入 {[e.get('id') for e in chosen]}")

    @staticmethod
    def _strip_previous(req: ProviderRequest) -> None:
        parts = getattr(req, "extra_user_content_parts", None)
        if not parts:
            return
        kept = [p for p in parts if HEADER not in (getattr(p, "text", "") or "")]
        if len(kept) != len(parts):
            req.extra_user_content_parts = kept

    # ---------- 命令 ----------

    @filter.command("lore")
    async def lore_command(self, event: AstrMessageEvent):
        raw = (getattr(event, "message_str", "") or "").strip()
        tokens = raw.split(maxsplit=2)
        if tokens and tokens[0].lstrip("/").lower() == "lore":
            tokens = tokens[1:]
        action = tokens[0].lower() if tokens else "status"
        entries = self._load_entries()

        if action == "status":
            yield event.plain_result(
                f"世界书：{len(entries)} 条，最多注入 {self._cfg('max_entries', 3)} 条\n"
                f"文件：{self._lore_path()}"
            )
            return

        if action == "list":
            lines = [f"- {e.get('id')}（{len(e.get('keys') or [])} 词，p={e.get('probability', 1.0)}）" for e in entries]
            yield event.plain_result("\n".join(lines[:30]) or "（空）")
            return

        if action == "test":
            text = tokens[1] if len(tokens) > 1 else ""
            if not text:
                yield event.plain_result("用法：/lore test 今天好累")
                return
            chosen = self._select(text, False, ignore_probability=True)
            if not chosen:
                yield event.plain_result("这句话没有命中任何条目。")
                return
            body = "\n".join(f"- {e.get('label')}（p={e.get('probability')}）" for e in chosen)
            yield event.plain_result(f"会注入 {len(chosen)} 条：\n{body}")
            return

        yield event.plain_result("用法：/lore、/lore list、/lore test 文本")
