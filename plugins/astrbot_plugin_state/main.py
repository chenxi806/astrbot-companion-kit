"""状态驱动插件（AstrBot v4）。

两个 0-1 的慢变量：
- mood（心情）
- energy（精力）

每天漂移一次（向 0.6 回归 + 随机 ±0.1），对话里再微调（被夸 +，被冷落 −）。
状态不报数字，只变成一句定性提示注入；另带一个 whim 池——按状态挑一件小事
让她顺口分享，每天最多 2 次。

命令：/state 看当前状态（调试用）
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import date, datetime
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


DEFAULT_MEMORY_DIR = r"D:\deepseek图标\memory"
HEADER = "<State>"
FOOTER = "</State>"

POSITIVE_WORDS = ("喜欢你", "想你", "可爱", "好看", "厉害", "谢谢", "乖", "抱", "亲", "夸")
NEGATIVE_WORDS = ("算了", "不想理", "烦", "滚", "别烦", "生气", "失望", "讨厌")


def clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


@register(
    "astrbot_plugin_state",
    "local",
    "状态机：心情/精力慢漂移，影响语气和主动程度；whim 池提供随机由头",
    "1.0.0",
)
class StatePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._lock = asyncio.Lock()

    # ---------- 配置 ----------

    def _cfg(self, key: str, default):
        try:
            value = self.config.get(key, default)
        except AttributeError:
            return default
        return default if value is None else value

    def _memory_dir(self) -> Path:
        return Path(str(self._cfg("memory_dir", "")).strip() or DEFAULT_MEMORY_DIR)

    def _state_path(self) -> Path:
        try:
            base = Path(StarTools.get_data_dir("astrbot_plugin_state"))
        except Exception:
            base = self._memory_dir() / "state_data"
        base.mkdir(parents=True, exist_ok=True)
        return base / "state.json"

    def _whims_path(self) -> Path:
        raw = str(self._cfg("whims_path", "")).strip()
        return Path(raw) if raw else self._memory_dir() / "whims.json"

    def _load(self, path: Path, default: dict) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return dict(default)

    def _save(self, path: Path, data: dict) -> None:
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning(f"[state] 保存失败: {exc}")

    # ---------- 漂移 ----------

    def _tick(self, state: dict) -> dict:
        today = date.today().isoformat()
        if state.get("day") == today:
            return state
        try:
            last = date.fromisoformat(state.get("day", today))
            days = max(1, min(3, (date.today() - last).days))
        except Exception:
            days = 1
        hour = datetime.now().hour
        for key in ("mood", "energy"):
            value = float(state.get(key, 0.6))
            drift = random.uniform(-0.1, 0.1) * days
            value = value + drift + (0.6 - value) * 0.15 * days
            if key == "energy":
                if 0 <= hour < 6 or hour >= 23:
                    value -= 0.08 * days
                elif 6 <= hour < 10:
                    value += 0.05 * days
            state[key] = round(clamp(value), 3)
        state["day"] = today
        state["whims_today"] = 0
        state["micro_today"] = 0.0
        return state

    def _micro(self, state: dict, text: str) -> None:
        delta = 0.0
        if any(w in text for w in POSITIVE_WORDS):
            delta += 0.03
        if any(w in text for w in NEGATIVE_WORDS):
            delta -= 0.05
        if not delta:
            return
        used = float(state.get("micro_today", 0.0))
        if abs(used + delta) > 0.1:
            return
        state["mood"] = round(clamp(float(state.get("mood", 0.6)) + delta), 3)
        state["micro_today"] = round(used + delta, 3)

    # ---------- 描述 ----------

    @staticmethod
    def _mood_label(value: float) -> str:
        if value < 0.3:
            return "低落"
        if value < 0.5:
            return "有点闷"
        if value < 0.65:
            return "平常"
        if value < 0.8:
            return "松快"
        return "很好"

    @staticmethod
    def _energy_label(value: float) -> str:
        if value < 0.3:
            return "很累"
        if value < 0.5:
            return "没什么劲"
        if value < 0.7:
            return "还行"
        return "充沛"

    def _guidance(self, mood: float, energy: float) -> str:
        parts = []
        if energy < 0.4:
            parts.append("句子短一点，别拖长")
        elif energy > 0.75:
            parts.append("可以多闹两句")
        if mood < 0.4:
            parts.append("别硬撑着撒娇，语气收着点")
        elif mood > 0.75:
            parts.append("可以主动黏一下、说点软话")
        return "；".join(parts) or "正常说话就行"

    # ---------- whim ----------

    def _pick_whim(self, state: dict, now: datetime) -> dict | None:
        data = self._load(self._whims_path(), {"items": []})
        items = [i for i in data.get("items", []) if isinstance(i, dict)]
        if not items:
            return None
        if int(state.get("whims_today", 0)) >= int(self._cfg("max_per_day", 2)):
            return None
        last = float(state.get("last_whim_at", 0) or 0)
        if last and time.time() - last < float(self._cfg("min_gap_hours", 5)) * 3600:
            return None
        mood = float(state.get("mood", 0.6))
        energy = float(state.get("energy", 0.6))
        clock = now.strftime("%H:%M")
        ready = []
        for item in items:
            if mood < float(item.get("min_mood", 0)) or mood > float(item.get("max_mood", 1)):
                continue
            if energy < float(item.get("min_energy", 0)):
                continue
            window = item.get("window") or ["00:00", "24:00"]
            if not (window[0] <= clock <= window[1]):
                continue
            ready.append(item)
        if not ready:
            return None
        return random.choice(ready)

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
            state = self._load(self._state_path(), {"mood": 0.6, "energy": 0.6})
            state = self._tick(state)
            self._micro(state, text)
            now = datetime.now()
            whim = None
            if bool(self._cfg("whim_enabled", True)):
                whim = self._pick_whim(state, now)
            if whim:
                state["whims_today"] = int(state.get("whims_today", 0)) + 1
                state["last_whim_at"] = time.time()
            self._save(self._state_path(), state)

        mood = float(state.get("mood", 0.6))
        energy = float(state.get("energy", 0.6))
        lines = [
            f"【此刻状态】心情：{self._mood_label(mood)}；精力：{self._energy_label(energy)}。",
            self._guidance(mood, energy) + "。别报状态、别解释原因。",
        ]
        if whim:
            lines.append(f"【可以顺手提一句】你刚想起：{whim.get('text')}。自然带出来，别报幕。")

        self._strip_previous(req)
        req.extra_user_content_parts.append(
            TextPart(text=f"{HEADER}\n" + "\n".join(lines) + f"\n{FOOTER}").mark_as_temp()
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

    @filter.command("state")
    async def state_command(self, event: AstrMessageEvent):
        state = self._tick(self._load(self._state_path(), {"mood": 0.6, "energy": 0.6}))
        self._save(self._state_path(), state)
        yield event.plain_result(
            f"心情：{self._mood_label(float(state.get('mood', 0.6)))}（{state.get('mood')}）\n"
            f"精力：{self._energy_label(float(state.get('energy', 0.6)))}（{state.get('energy')}）\n"
            f"今天 whim：{state.get('whims_today', 0)} 次"
        )
