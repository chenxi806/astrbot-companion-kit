"""关系阶段插件（AstrBot v4）。

把"天降妹妹 → 恋人未满"做成一条可见的阶段线，不新造一套好感分，
而是用「认识天数 + 互动条数 + 共同事件」自然推进：

    认识 → 熟悉 → 亲近 → 亲密（恋人未满）→［告白锁］→ 深度羁绊 → 专属

- 每轮注入当前阶段的行为提示（不说数字、不报幕）
- 到了阶段门口但事件不够，会提示她安排一次小事件（带 A/B/C 选项）
- 选项完成算一次共同事件
- 亲密之后锁住：要正式告白才进下一段，而告白必须由用户主动触发

命令（主人私聊）：
- /stage 看当前阶段和进度
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta
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


DEFAULT_MEMORY_DIR = ""


def _default_memory_dir() -> Path:
    """默认记忆目录：<AstrBot data>/companion_memory。

    想换位置就在插件配置里填 memory_dir（所有插件填同一个）。
    """
    try:
        base = Path(StarTools.get_data_dir("astrbot_plugin_galgame"))
        return base.parent.parent / "companion_memory"
    except Exception:
        return Path.home() / "companion_memory"
HEADER = "<Stage>"
FOOTER = "</Stage>"

STAGES = [
    {
        "key": "acquaintance",
        "name": "认识",
        "days": 0,
        "messages": 0,
        "events": 0,
        "desc": "你们还客气，带点试探。别主动越界，也别太黏；他递话你才接。",
    },
    {
        "key": "familiar",
        "name": "熟悉",
        "days": 3,
        "messages": 100,
        "events": 0,
        "desc": "妹妹感出来了：会撒娇、会闹、会分享日常，偶尔嘴硬。还没到说想他的程度。",
    },
    {
        "key": "close",
        "name": "亲近",
        "days": 7,
        "messages": 300,
        "events": 1,
        "desc": "明显偏心：会吃醋、会主动找他、会护短。想他了也绕着说，不直接承认。",
    },
    {
        "key": "intimate",
        "name": "亲密（恋人未满）",
        "days": 21,
        "messages": 800,
        "events": 3,
        "desc": "已经很近了：可以说想他、可以靠很近、可以小尺度调情，但不明说关系。他要把话说破，先害羞岔开，等他正式开口。",
    },
]

CONFESSION_NOTE = "关系停在这里：不要自己说「我们在一起吧」，也不要用女朋友自称；把最后一步留给他。"


@register(
    "astrbot_plugin_galgame",
    "local",
    "关系阶段：认识→熟悉→亲近→亲密（恋人未满），带事件和选项，告白锁",
    "1.0.0",
)
class GalgamePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._lock = asyncio.Lock()
        self._stats_cache: tuple[float, dict] | None = None

    # ---------- 配置 ----------

    def _cfg(self, key: str, default):
        try:
            value = self.config.get(key, default)
        except AttributeError:
            return default
        return default if value is None else value

    def _memory_dir(self) -> Path:
        raw = str(self._cfg("memory_dir", "")).strip()
        return Path(raw) if raw else _default_memory_dir()

    def _state_path(self) -> Path:
        try:
            base = Path(StarTools.get_data_dir("astrbot_plugin_galgame"))
        except Exception:
            base = self._memory_dir() / "galgame_state"
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
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning(f"[galgame] 保存状态失败: {exc}")

    # ---------- 统计 ----------

    def _stats(self) -> dict:
        now = time.time()
        if self._stats_cache and now - self._stats_cache[0] < 60:
            return self._stats_cache[1]
        raw = self._memory_dir() / "raw"
        count = 0
        first = None
        for path in sorted(raw.glob("*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            for line in lines:
                if not line.strip():
                    continue
                count += 1
                try:
                    ts = json.loads(line).get("ts", "")
                    stamp = datetime.fromisoformat(ts)
                except Exception:
                    continue
                if first is None or stamp < first:
                    first = stamp
        days = 0
        if first is not None:
            days = max(0, (datetime.now(first.tzinfo) - first).days)
        stats = {"days": days, "messages": count, "first": first.isoformat() if first else None}
        self._stats_cache = (now, stats)
        return stats

    # ---------- 阶段 ----------

    def _stage_index(self, state: dict) -> int:
        events = int(state.get("events", 0))
        stats = self._stats()
        index = 0
        for i, stage in enumerate(STAGES):
            if stats["days"] >= int(stage["days"]) and stats["messages"] >= int(stage["messages"]) and events >= int(stage["events"]):
                index = i
        if not self._cfg("allow_confession", False):
            index = min(index, len(STAGES) - 1)  # 先锁在亲密
        return index

    def _progress_text(self, index: int, state: dict) -> str:
        stage = STAGES[index]
        stats = self._stats()
        if index >= len(STAGES) - 1:
            return ""
        nxt = STAGES[index + 1]
        parts = []
        if stats["days"] < nxt["days"]:
            parts.append(f"天数 {stats['days']}/{nxt['days']}")
        if stats["messages"] < nxt["messages"]:
            parts.append(f"互动 {stats['messages']}/{nxt['messages']}")
        if int(state.get("events", 0)) < nxt["events"]:
            parts.append(f"共同事件 {state.get('events', 0)}/{nxt['events']}")
        return "；".join(parts)

    # ---------- 钩子 ----------

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest):
        if not bool(self._cfg("enabled", True)):
            return
        if self._cfg("scope", "private") == "private":
            try:
                if event.get_message_type() != MessageType.FRIEND_MESSAGE:
                    return
            except Exception:
                pass

        text = (getattr(event, "message_str", "") or "").strip()
        if not text:
            return

        async with self._lock:
            state = self._load_state()
            now = time.time()

            # 选项回复：A/B/C 或 1/2/3
            pending = state.get("pending_choice") or {}
            if pending and now - float(pending.get("at", 0)) < 12 * 3600:
                if re.fullmatch(r"[/／]?[ABCabc123][。.!！]?", text.strip()):
                    state["events"] = int(state.get("events", 0)) + 1
                    state["last_event_at"] = now
                    state.pop("pending_choice", None)
                    self._save_state(state)
                    logger.info(f"[galgame] 完成一次选项事件，累计 {state['events']}")

            index = self._stage_index(state)
            prev = state.get("stage_index")
            state["stage_index"] = index
            if prev != index:
                state["stage_changed_at"] = now
                logger.info(f"[galgame] 阶段变为 {STAGES[index]['name']}")
            self._save_state(state)

        lines = [f"【关系阶段】{STAGES[index]['name']}", STAGES[index]["desc"]]
        if index >= len(STAGES) - 1:
            lines.append(CONFESSION_NOTE)

        # 到了门口但事件不够：提示安排一次小事件
        if index < len(STAGES) - 1:
            nxt = STAGES[index + 1]
            stats = self._stats()
            ready = stats["days"] >= nxt["days"] and stats["messages"] >= nxt["messages"]
            need_events = int(state.get("events", 0)) < nxt["events"]
            last_event = float(state.get("last_event_offer", 0))
            if ready and need_events and now - last_event > 48 * 3600:
                lines.append(
                    "【可以安排一次小事件】你们的关系卡在门口，需要一点共同经历。"
                    "这条回复里可以安排一个具体的小场景，并在结尾给他 2-3 个选项（A/B/C），"
                    "不要直接推进关系，只制造一次相处的机会。"
                )
                state["last_event_offer"] = now
                self._save_state(state)
            elif not need_events:
                last_choice = float(state.get("last_choice_offer", 0))
                if now - last_choice > 36 * 3600 and not state.get("pending_choice"):
                    lines.append(
                        "【可以抛一个选项】如果这条回复自然，可以在结尾给他 2-3 个具体的选项（A/B/C），"
                        "像在问他想怎么过、想吃什么、想先听哪件事。不要每句都抛。"
                    )
                    state["last_choice_offer"] = now
                    state["pending_choice"] = {"at": now}
                    self._save_state(state)

        body = "\n".join(lines)
        self._strip_previous(req)
        req.extra_user_content_parts.append(
            TextPart(text=f"{HEADER}\n{body}\n{FOOTER}").mark_as_temp()
        )

    @staticmethod
    def _strip_previous(req: ProviderRequest) -> None:
        parts = getattr(req, "extra_user_content_parts", None)
        if not parts:
            return
        kept = [p for p in parts if HEADER not in (getattr(p, "text", "") or "")]
        if len(kept) != len(parts):
            req.extra_user_content_parts = kept

    # ---------- 命令 ----------

    @filter.command("stage")
    async def stage_command(self, event: AstrMessageEvent):
        state = self._load_state()
        index = self._stage_index(state)
        stats = self._stats()
        lines = [
            f"阶段：{STAGES[index]['name']}",
            f"认识 {stats['days']} 天，互动 {stats['messages']} 条，共同事件 {state.get('events', 0)} 次",
        ]
        progress = self._progress_text(index, state)
        if progress:
            lines.append(f"下一段还差：{progress}")
        else:
            lines.append("已经到最后一段（告白锁）。")
        yield event.plain_result("\n".join(lines))
