"""早晚安主动消息插件（AstrBot v4）。

- 每天 08:00 叫他起床 + 打招呼
- 每天 00:00 催他睡觉 + 说晚安
- 用当前人格 + 判断卡生成，带最近两天的聊天上下文，避免公式化
- 只发私聊；发送目标优先用配置的 umo，否则取最近一次私聊记录里的 umo
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools, register

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

DEFAULT_MEMORY_DIR = ""
CONTROL_TAG_RE = re.compile(r"<pc_[a-z_]+>.*?</pc_[a-z_]+>", re.S)



def _default_memory_dir() -> Path:
    """默认记忆目录：<AstrBot data>/companion_memory。

    想换位置就在插件配置里填 memory_dir（所有插件填同一个）。
    """
    try:
        base = Path(StarTools.get_data_dir("astrbot_plugin_daily_ritual"))
        return base.parent.parent / "companion_memory"
    except Exception:
        return Path.home() / "companion_memory"

MORNING_ANGLES = [
    "用昨天没聊完的那件事开头",
    "拿吃的引诱他起床",
    "赖床式撒娇，说自己也起不来",
    "假装已经等他很久了",
    "提一句他今天要做的事",
    "刚睡醒的迷糊劲儿，话说一半",
]

NIGHT_ANGLES = [
    "催他放下手机",
    "提一件今天发生的小事再道晚安",
    "撒娇要一句晚安",
    "装困，说着说着要睡着",
    "提明天要做的事，让他放心睡",
    "假装生气他还不睡，但语气是软的",
]


@register(
    "astrbot_plugin_daily_ritual",
    "local",
    "每天 8:00 叫起床、0:00 催睡觉，带上下文和人格，不公式化",
    "1.0.0",
)
class DailyRitualPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ---------- 生命周期 ----------

    async def initialize(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info("[daily_ritual] 调度器已启动（8:00 / 0:00）")

    async def terminate(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("[daily_ritual] 调度器已停止")

    # ---------- 配置 ----------

    def _cfg(self, key: str, default):
        try:
            value = self.config.get(key, default)
        except AttributeError:
            return default
        return default if value is None else value

    def _tz(self):
        name = str(self._cfg("timezone", "Asia/Shanghai")).strip() or "Asia/Shanghai"
        if ZoneInfo is not None:
            try:
                return ZoneInfo(name)
            except Exception:
                pass
        return timezone(timedelta(hours=8))

    def _parse_time(self, raw: str, fallback: tuple[int, int]) -> tuple[int, int]:
        try:
            hour, minute = str(raw).strip().split(":")
            return int(hour), int(minute)
        except Exception:
            logger.warning(f"[daily_ritual] 时间格式不对：{raw!r}，用默认 {fallback}")
            return fallback

    def _memory_dir(self) -> Path:
        raw = str(self._cfg("memory_dir", "")).strip()
        return Path(raw) if raw else _default_memory_dir()

    # ---------- 调度 ----------

    def _next_run(self, now: datetime) -> datetime:
        morning = self._parse_time(self._cfg("morning_time", "08:00"), (8, 0))
        night = self._parse_time(self._cfg("night_time", "00:00"), (0, 0))
        candidates: list[datetime] = []
        for offset in (0, 1, 2):
            day = (now + timedelta(days=offset)).date()
            for hour, minute in (morning, night):
                moment = datetime(
                    day.year, day.month, day.day, hour, minute, tzinfo=now.tzinfo
                )
                if moment > now + timedelta(seconds=15):
                    candidates.append(moment)
        return min(candidates)

    async def _loop(self):
        await asyncio.sleep(10)
        await self._catch_up()
        while True:
            try:
                now = datetime.now(self._tz())
                target = self._next_run(now)
                wait = max(1.0, (target - now).total_seconds())
                logger.debug(f"[daily_ritual] 下一次 {target:%Y-%m-%d %H:%M}，等待 {wait:.0f}s")
                await asyncio.sleep(wait)

                morning = self._parse_time(self._cfg("morning_time", "08:00"), (8, 0))
                kind = "morning" if (target.hour, target.minute) == morning else "night"
                await self._send(kind)
                await asyncio.sleep(120)  # 防止同一分钟重复触发
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[daily_ritual] 调度循环出错: {exc}", exc_info=True)
                await asyncio.sleep(60)

    async def _catch_up(self) -> None:
        """启动时补发：错过窗口不超过 N 分钟的早晚安。"""
        try:
            minutes = int(self._cfg("catchup_minutes", 90))
        except Exception:
            minutes = 90
        if minutes <= 0:
            return
        now = datetime.now(self._tz())
        morning = self._parse_time(self._cfg("morning_time", "08:00"), (8, 0))
        night = self._parse_time(self._cfg("night_time", "00:00"), (0, 0))
        state = self._load_state()
        for kind, (hour, minute) in (("morning", morning), ("night", night)):
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            delta = (now - target).total_seconds()
            if not (0 <= delta <= minutes * 60):
                continue
            if str(state.get(kind, {}).get("at", ""))[:10] == now.date().isoformat():
                continue
            logger.info(f"[daily_ritual] 补发 {kind}（晚了 {delta / 60:.0f} 分钟）")
            try:
                await self._send(kind)
            except Exception as exc:
                logger.error(f"[daily_ritual] 补发失败: {exc}")

    # ---------- 上下文 ----------

    def _resolve_umo(self) -> str:
        configured = str(self._cfg("umo", "")).strip()
        if configured:
            return configured
        raw = self._memory_dir() / "raw"
        files = sorted(raw.glob("*.jsonl"))
        for path in reversed(files):
            for line in reversed(path.read_text(encoding="utf-8", errors="ignore").splitlines()):
                try:
                    umo = json.loads(line).get("umo") or ""
                except Exception:
                    continue
                if umo:
                    return umo
        return ""

    def _recent_dialogue(self, limit: int) -> str:
        raw = self._memory_dir() / "raw"
        files = sorted(raw.glob("*.jsonl"))[-3:]
        rows: list[dict] = []
        for path in files:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        rows = rows[-limit:]
        lines: list[str] = []
        for row in rows:
            stamp = str(row.get("ts", ""))[11:16]
            user = re.sub(r"\s+", " ", str(row.get("user", ""))).strip()
            assistant = re.sub(r"\s+", " ", str(row.get("assistant", ""))).strip()
            if user:
                lines.append(f"他（{stamp}）：{user[:100]}")
            if assistant:
                lines.append(f"你（{stamp}）：{assistant[:100]}")
        return "\n".join(lines)

    def _read_card(self) -> str:
        path = self._memory_dir() / "card.companion.txt"
        try:
            return path.read_text(encoding="utf-8-sig").strip()
        except OSError:
            return ""

    # ---------- 状态 ----------

    def _state_path(self) -> Path:
        try:
            base = Path(StarTools.get_data_dir("astrbot_plugin_daily_ritual"))
        except Exception:
            base = self._memory_dir() / "ritual_state"
        base.mkdir(parents=True, exist_ok=True)
        return base / "last_sent.json"

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_path().read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state(self, kind: str, text: str) -> None:
        state = self._load_state()
        state[kind] = {"text": text, "at": datetime.now(self._tz()).isoformat(timespec="seconds")}
        try:
            self._state_path().write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning(f"[daily_ritual] 保存状态失败: {exc}")

    # ---------- 生成与发送 ----------

    async def _build_prompt(self, kind: str) -> str:
        now = datetime.now(self._tz())
        weekday = "一二三四五六日"[now.weekday()]
        if kind == "morning":
            scene = "现在是早上，他该起床了。你要叫他起来，顺便打个招呼。"
            angles = MORNING_ANGLES
        else:
            scene = "现在已经很晚了，他还没睡。你要催他去睡，跟他说晚安。"
            angles = NIGHT_ANGLES
        angle = random.choice(angles)
        dialogue = self._recent_dialogue(int(self._cfg("context_messages", 30)))
        last = self._load_state().get(kind, {}).get("text", "")

        parts = [
            f"现在是 {now:%Y-%m-%d} {now:%H:%M}（周{weekday}）。{scene}",
            "",
        ]
        if dialogue:
            parts += [
                "你们最近两天的聊天（只作参考，不要复述、不要逐条总结）：",
                dialogue,
                "",
            ]
        if last:
            parts += [f"你上一次这个时间发的是：「{last}」——不要和它重复，换个说法。", ""]
        parts += [
            f"这次的角度：{angle}。",
            "",
            "写一条你现在要发给他的消息。要求：",
            "- 只输出要发的那句话本身，不要引号、不要旁白、不要解释，不要调用任何工具",
            "- 一到三句，短句、口语，像发消息，不要长篇大论",
            "- 可以自然带上昨天或今天的具体事；没有具体事就撒娇、关心，别硬编",
            "- 不要出现「提醒」「任务」「闹钟」「系统」这类词，也不要自称在提醒他",
            "- 不要每次都用同一个开头",
        ]
        extra = str(self._cfg("extra_prompt", "")).strip()
        if extra:
            parts += ["", extra]
        return "\n".join(parts)

    async def _generate(self, kind: str) -> tuple[str, str]:
        umo = self._resolve_umo()
        if not umo:
            raise RuntimeError("找不到私聊会话（umo），请先在私聊里聊一句，或在配置里填 umo")

        persona_prompt = ""
        try:
            persona = await self.context.persona_manager.get_default_persona_v3(umo)
            persona_prompt = str((persona or {}).get("prompt", "") or "")
        except Exception as exc:
            logger.warning(f"[daily_ritual] 读人格失败，退回默认: {exc}")

        card = self._read_card()
        system = "\n\n".join(
            part
            for part in [
                persona_prompt,
                card,
                "你现在是主动发一条消息，不是回复他。只输出你要发的那条消息本身，不要解释，不要调用工具。",
            ]
            if part
        )

        provider = self.context.get_using_provider(umo)
        if provider is None:
            raise RuntimeError("没有可用的聊天 Provider")

        prompt = await self._build_prompt(kind)
        response = await provider.text_chat(
            prompt=prompt,
            system_prompt=system,
            contexts=[],
        )
        text = self._clean(getattr(response, "completion_text", "") or "")
        if not text:
            raise RuntimeError("模型返回空文本")
        return umo, text

    @staticmethod
    def _clean(text: str) -> str:
        text = CONTROL_TAG_RE.sub("", text).strip()
        text = text.strip("「」『』\"'` \n")
        text = re.sub(r"^[我俺][：:]\s*", "", text)
        text = re.sub(r"\n{2,}", "\n", text)
        return text[:200].strip()

    async def _send(self, kind: str) -> str:
        async with self._lock:
            umo, text = await self._generate(kind)
            ok = await self.context.send_message(
                umo, MessageChain([Plain(text)], use_markdown_=False)
            )
            logger.info(f"[daily_ritual] {kind} 投递结果={ok} umo={umo}")
            if ok:
                self._save_state(kind, text)
            else:
                logger.error(f"[daily_ritual] 发送失败，平台返回 False: {text[:40]}")
            return text

    # ---------- 命令 ----------

    @filter.command("ritual")
    async def ritual_command(self, event: AstrMessageEvent):
        """测试用：/ritual test morning、/ritual test night、/ritual status"""
        raw = (getattr(event, "message_str", "") or "").strip()
        tokens = raw.split()
        if tokens and tokens[0].lstrip("/").lower() == "ritual":
            tokens = tokens[1:]
        action = tokens[0].lower() if tokens else "status"

        if action == "status":
            now = datetime.now(self._tz())
            target = self._next_run(now)
            yield event.plain_result(
                f"下一次：{target:%Y-%m-%d %H:%M}\n"
                f"早上 {self._cfg('morning_time', '08:00')} / 晚上 {self._cfg('night_time', '00:00')}\n"
                f"目标会话：{self._resolve_umo() or '（还没找到）'}"
            )
            return

        if action == "test":
            kind = tokens[1].lower() if len(tokens) > 1 else "morning"
            if kind not in ("morning", "night"):
                yield event.plain_result("用法：/ritual test morning|night")
                return
            try:
                text = await self._send(kind)
            except Exception as exc:
                yield event.plain_result(f"生成失败：{exc}")
                return
            yield event.plain_result(f"已发送（{kind}）：{text}")
            return

        yield event.plain_result("用法：/ritual status、/ritual test morning、/ritual test night")
