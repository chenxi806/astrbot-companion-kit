"""模式系统（AstrBot v4）：艾莉丝 / 女仆。

- /艾莉丝模式、/女仆模式：进入
- /退出模式：恢复
- 模式中每轮注入角色卡；超时自动退出
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
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


HEADER = "<ModeSwitch>"
FOOTER = "</ModeSwitch>"

MODE_NAMES = {"alice": "艾莉丝", "maid": "女仆"}
SCENE_KEYS = {
    "被夸": ("可爱", "好看", "漂亮", "厉害", "喜欢你", "夸"),
    "吃醋": ("谁", "她", "别人", "朋友", "同事"),
    "练剑": ("剑", "练", "强", "打", "战斗"),
    "害羞": ("抱", "亲", "近", "手", "喜欢"),
    "生气": ("生气", "烦", "吵", "讨厌"),
    "关心": ("累", "饿", "吃", "睡", "冷", "难受"),
    "晚安": ("晚安", "睡", "困", "失眠"),
}
ENTER_PATTERNS = {
    "alice": re.compile(
        r"(艾莉丝|爱丽丝|爱莉丝|艾丽丝|エリス)(模式|形态)"
        r"|(演一下|进入|开始)(艾莉丝|爱丽丝|爱莉丝|艾丽丝)"
    ),
    "maid": re.compile(r"(女仆|女仆装)(模式|形态)|(换|穿|进入)(女仆装|女仆模式)"),
}
EXIT_PATTERN = re.compile(r"^\s*(退出|回来|变回来|结束模式|恢复原样)[。！!…\s]*$")
ROLEPLAY_PATTERN = re.compile(r"(角色扮演|来玩角色扮演|陪我演|玩个扮演|演个角色)")
CANCEL_PATTERN = re.compile(r"^\s*(算了|不玩|不用了|取消|不要|下次吧)[。！!…\s]*$")
PENDING_SECONDS = 180

FALLBACK = {
    "alice": "艾莉丝·格雷拉特，红发红眼，剑术少女，傲娇直率、嘴硬心软。日语短句，口癖：べ、別に…／うるさい！／…知らない！",
    "maid": "大肥鱼换上女仆装，应声说「吾主」，举止规整，但本音会漏；不做低三下四的讨好。",
}


@register("astrbot_plugin_mode_switch", "local", "模式系统：艾莉丝 / 女仆，进入、退出、超时、注入", "1.0.0")
class ModeSwitch(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._cache = {}

    def _cfg(self, key: str, default=None):
        value = self.config.get(key, default) if hasattr(self.config, "get") else None
        return default if value is None else value

    def _memory(self) -> Path:
        raw = str(self._cfg("memory_dir", "")).strip()
        if not raw:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            raw = str(get_astrbot_data_path() / "companion_memory")
        return Path(raw)

    def _state_path(self) -> Path:
        path = self._memory() / "modes"
        path.mkdir(parents=True, exist_ok=True)
        return path / "state.json"

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_path().read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self, state: dict) -> None:
        path = self._state_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _card(self, mode: str) -> str:
        if mode in self._cache:
            return self._cache[mode]
        text = FALLBACK.get(mode, "")
        try:
            data = json.loads((self._memory() / "lorebook.json").read_text(encoding="utf-8"))
            for entry in data.get("entries", []):
                if entry.get("id") == mode and entry.get("content"):
                    text = str(entry["content"]).strip()
                    break
        except Exception:
            pass
        self._cache[mode] = text
        return text

    def _alice_samples(self, text: str, count: int = 2) -> list[str]:
        try:
            data = json.loads((self._memory() / "modes" / "alice_corpus.json").read_text(encoding="utf-8"))
            entries = data.get("entries", [])
        except Exception:
            return []
        if not entries:
            return []
        scenes = [name for name, keys in SCENE_KEYS.items() if any(k in text for k in keys)]
        pool = [e for e in entries if not scenes or e.get("scene") in scenes]
        if len(pool) < count:
            pool = entries
        import random

        return [str(e.get("zh") or e.get("jp", "")) for e in random.sample(pool, min(count, len(pool)))]

    async def _suspend_mimo(self, state: dict | None = None) -> None:
        state = self._load_state()
        try:
            plugin = self.context.get_registered_star("astrbot_plugin_mimo_tts_clone")
            if not plugin or not getattr(plugin, "activated", False):
                return
            manager = getattr(self.context, "_star_manager", None)
            if manager is None:
                return
            await manager.turn_off_plugin("astrbot_plugin_mimo_tts_clone")
            state["mimo_suspended"] = True
            self._save_state(state)
        except Exception as exc:
            logger.warning(f"[mode_switch] suspend mimo failed: {exc}")

    async def _resume_mimo(self, state: dict | None = None) -> None:
        state = self._load_state()
        if not state.get("mimo_suspended"):
            return
        try:
            manager = getattr(self.context, "_star_manager", None)
            if manager is not None:
                await manager.turn_on_plugin("astrbot_plugin_mimo_tts_clone")
        except Exception as exc:
            logger.warning(f"[mode_switch] resume mimo failed: {exc}")
        state["mimo_suspended"] = False
        self._save_state(state)

    def _active(self) -> str | None:
        state = self._load_state()
        mode = state.get("mode")
        if not mode:
            return None
        try:
            expires = datetime.fromisoformat(str(state.get("expires_at", "")))
        except Exception:
            expires = None
        if expires and datetime.now() >= expires:
            state["mode"] = None
            state["expired_at"] = datetime.now().isoformat(timespec="seconds")
            self._save_state(state)
            try:
                import asyncio

                loop = asyncio.get_running_loop()
                loop.create_task(self._resume_mimo(state))
            except Exception:
                pass
            return None
        return mode

    def _enter(self, mode: str) -> str:
        minutes = int(self._cfg(f"{mode}_minutes", 10) or 10)
        now = datetime.now()
        state = {
            "mode": mode,
            "started_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
            "minutes": minutes,
        }
        self._save_state(state)
        if mode == "alice":
            return f"……べ、別に。{minutes} 分钟，到点我自己走。"
        return f"知道了，主人。就 {minutes} 分钟。"

    def _exit(self) -> str:
        state = self._load_state()
        if not state.get("mode"):
            return "现在没在演啊。"
        mode = state.get("mode")
        state["mode"] = None
        state["exited_at"] = datetime.now().isoformat(timespec="seconds")
        self._save_state(state)
        return "……回来了。" if mode == "alice" else "换回来了，哥哥。"

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest):
        if not bool(self._cfg("enabled", True)):
            return
        if str(self._cfg("scope", "private")).lower() == "private":
            try:
                if event.get_message_type() == MessageType.GROUP_MESSAGE:
                    return
            except Exception:
                pass
        text = (getattr(event, "message_str", "") or "").strip()
        state = self._load_state()
        mode = state.get("mode")
        if ROLEPLAY_PATTERN.search(text):
            if mode:
                self._exit()
                await self._resume_mimo(state)
                state = self._load_state()
            state["roleplay_pending_at"] = datetime.now().isoformat(timespec="seconds")
            self._save_state(state)
            self._push(
                req,
                f"{HEADER}\n（他在邀请你玩角色扮演。先用一句话答应，再问他想演哪个：1 艾莉丝，2 女仆。"
                f"不要直接进入角色，不要提“模式”“设定”。）\n{FOOTER}",
            )
            return
        if mode:
            try:
                expires = datetime.fromisoformat(str(state.get("expires_at", "")))
            except Exception:
                expires = None
            if expires and datetime.now() >= expires:
                state["mode"] = None
                state["expired_at"] = datetime.now().isoformat(timespec="seconds")
                self._save_state(state)
                await self._resume_mimo(state)
                return
            if EXIT_PATTERN.match(text):
                self._exit()
                await self._resume_mimo(state)
                self._push(req, f"{HEADER}\n（模式刚结束，回到平常的语气，不要提“模式”。）\n{FOOTER}")
                return
        else:
            pending_at = str(state.get("roleplay_pending_at") or "")
            pending_ok = False
            if pending_at:
                try:
                    pending_ok = (datetime.now() - datetime.fromisoformat(pending_at)).total_seconds() <= PENDING_SECONDS
                except Exception:
                    pending_ok = False

            if pending_ok:
                clean = text.strip(" ，。！!？?、")
                alice_choice = bool(ENTER_PATTERNS["alice"].search(text)) or clean in ("1", "一", "①")
                maid_choice = bool(ENTER_PATTERNS["maid"].search(text)) or clean in ("2", "二", "②")
                if CANCEL_PATTERN.match(text):
                    state["roleplay_pending_at"] = None
                    self._save_state(state)
                    self._push(req, f"{HEADER}\n（他不想演了，正常回一句就行，不要提“模式”。）\n{FOOTER}")
                    return
                if alice_choice or maid_choice:
                    name = "alice" if alice_choice else "maid"
                    state["roleplay_pending_at"] = None
                    self._save_state(state)
                    self._enter(name)
                    mode = name
                    if name == "alice":
                        await self._suspend_mimo(self._load_state())
                else:
                    self._push(
                        req,
                        f"{HEADER}\n（他在选角色，但没选清楚。再问一次：1 艾莉丝，2 女仆。不要直接进入角色。）\n{FOOTER}",
                    )
                    return
            elif ROLEPLAY_PATTERN.search(text):
                state["roleplay_pending_at"] = datetime.now().isoformat(timespec="seconds")
                self._save_state(state)
                self._push(
                    req,
                    f"{HEADER}\n（他在邀请你玩角色扮演。先用一句话答应，再问他想演哪个：1 艾莉丝，2 女仆。"
                    f"不要直接进入角色，不要提“模式”“设定”。）\n{FOOTER}",
                )
                return
            else:
                for name, pattern in ENTER_PATTERNS.items():
                    if pattern.search(text):
                        self._enter(name)
                        mode = name
                        if name == "alice":
                            await self._suspend_mimo(self._load_state())
                        break
        if not mode:
            self._push(
                req,
                f"{HEADER}\n（称呼：回话时称他「你」，不要写「他」。例：（伸手把你拽进怀里）对；（把他拽进怀里）错。「」只包一句台词，动作放括号里，不要用「」包整段。）\n{FOOTER}",
            )
            return
        card = self._card(mode)
        if not card:
            return
        state = self._load_state()
        lang_rule = ""
        if mode == "alice":
            lang_rule = (
                "语言：回复用中文，不要日语。语气像喜欢上鲁迪之后的艾莉丝：护短、直球、黏人，"
                "会主动靠近、牵手、靠肩；吃醋会直接说；嘴上还凶，凶完补一句真心。"
                "只有语音才用日语；文字不要夹日语。\n"
                "要发语音时，只能调用 eris_tts_speak（艾莉丝声线），不要调用 mimo_tts_speak。"
                "默认 mode=short，只发 2~12 字情绪碎片，emotion 选 害羞/傲娇/生气/开心/吃醋/关心/撒娇/惊讶/嘴硬/软。\n"
                "晚安/哄睡用 mode=line。\n"
                "只有这六种情况用 mode=full，且 text 只放那一句（≤45字，不要带动作和旁白）："
                "安慰、告白或和好、挑逗、吃醋直说、认真生气或担心他受伤、想念撒娇。\n"
                "整句 3 分钟内最多一条，超了会被自动降级成碎片。\n"
                "调用后立即继续写中文正文，不要等语音。\n"
                "语音能力是正常的：需要发语音就直接调用 eris_tts_speak；不要对他说“语音功能没开”“你自己开一下”这种话。\n"
                "称呼：动作和台词里只能叫他「你」，绝对不要用「他」；你不是在写小说，是在对他说话。\n"
                "结构：长回复用空行分开，动作一段、台词一段、停顿一段；不要把动作和台词堆成一大段。\n"
            )
        block = (
            f"{HEADER}\n"
            f"【当前模式：{MODE_NAMES.get(mode, mode)}】\n"
            f"{card}\n"
            f"规则：不是换人，底层还是大肥鱼。会露馅：说着说着冒出原来的语气词或称呼，然后慌忙改口；耳朵红、手指绞衣角、动作僵硬。被看穿会羞耻、炸毛，但不承认。日常习惯和关心方式保留。\n"
            f"{lang_rule}"
            f"不要提“模式”“设定”“扮演”；他情绪不对时立刻破戏，用平时的声音说话。\n"
            f"到 {state.get('expires_at', '')} 自动退出。\n"
            f"语感样本（中文，学语气，不要照抄）：\n"
            + "\n".join(self._alice_samples(text) if mode == "alice" else [])
            + f"\n{FOOTER}"
        )
        parts = getattr(req, "extra_user_content_parts", None)
        if parts:
            kept = [p for p in parts if HEADER not in (getattr(p, "text", "") or "")]
            if len(kept) != len(parts):
                req.extra_user_content_parts = kept
        req.extra_user_content_parts.append(TextPart(text=block).mark_as_temp())

    def _push(self, req: ProviderRequest, block: str) -> None:
        parts = getattr(req, "extra_user_content_parts", None)
        if parts:
            kept = [p for p in parts if HEADER not in (getattr(p, "text", "") or "")]
            if len(kept) != len(parts):
                req.extra_user_content_parts = kept
        req.extra_user_content_parts.append(TextPart(text=block).mark_as_temp())

    @filter.command("艾莉丝模式", alias={"演一下艾莉丝", "エリス"})
    async def alice(self, event: AstrMessageEvent):
        yield event.plain_result(self._enter("alice"))

    @filter.command("女仆模式", alias={"女仆", "换女仆装"})
    async def maid(self, event: AstrMessageEvent):
        yield event.plain_result(self._enter("maid"))

    @filter.command("退出模式", alias={"退出", "回来", "变回来"})
    async def leave(self, event: AstrMessageEvent):
        yield event.plain_result(self._exit())

    @filter.command("模式状态")
    async def status(self, event: AstrMessageEvent):
        mode = self._active()
        if not mode:
            yield event.plain_result("当前没有模式。")
            return
        state = self._load_state()
        yield event.plain_result(f"当前：{MODE_NAMES.get(mode, mode)}，到 {state.get('expires_at', '')}。")
