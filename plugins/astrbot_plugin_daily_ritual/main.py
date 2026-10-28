"""早晚安 + 主动消息（已合并）—— AstrBot v4 插件。

一条链路全包：
- 每天 08:00 叫他起床 + 打招呼
- 每天 23:00 催他睡觉 + 说晚安
- 白天（默认 09:00-22:30）他安静下来之后，每隔 60~180 分钟主动找一次，每天最多 4 条
- 早晚安有 90 分钟补发窗口（进程重启也能补）
- 内容由当前人格 + 判断卡 + 最近两天上下文生成，避免公式化
- 只发私聊；目标优先用配置的 umo，否则取最近一次私聊记录里的 umo
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

DEFAULT_MEMORY_DIR = r"D:\deepseek图标\memory"
CONTROL_TAG_RE = re.compile(r"<pc_[a-z_]+>.*?</pc_[a-z_]+>", re.S)

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

DAYTIME_ANGLES = [
    "想起一件小事，随口告诉他",
    "问他吃饭了没有，但别像查岗",
    "说自己刚才在做什么，带一点撒娇",
    "吐槽一句今天遇到的小麻烦",
    "问他手头的事顺不顺利",
    "没来由地冒一句想他，然后马上嘴硬",
    "分享一个刚看到的、觉得他会喜欢的东西",
]


@register(
    "astrbot_plugin_daily_ritual",
    "local",
    "早晚安 + 主动消息合并版：8:00 叫起床、23:00 催睡，白天随机主动找他",
    "1.1.0",
)
class DailyRitualPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._task: asyncio.Task | None = None
        self._random_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._started_at = datetime.now(self._tz())

    # ---------- 生命周期 ----------

    async def initialize(self):
        if not bool(self._cfg("enabled", True)):
            logger.info("[daily_ritual] 已禁用，不定时发送（/ritual test 仍然可用）")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info(
                "[daily_ritual] 调度器已启动（%s / %s）",
                self._cfg("morning_time", "08:00"),
                self._cfg("night_time", "23:00"),
            )
        if self._random_task is None or self._random_task.done():
            self._random_task = asyncio.create_task(self._random_loop())
            logger.info(
                "[daily_ritual] 主动消息已启动（窗口 %s，间隔 %s~%s 分钟，每天最多 %s 条）",
                self._cfg("random_window", "09:00-22:30"),
                self._cfg("random_min_gap_minutes", 60),
                self._cfg("random_max_gap_minutes", 180),
                self._cfg("random_daily_max", 4),
            )

    async def terminate(self):
        for name in ("_task", "_random_task"):
            task = getattr(self, name)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, name, None)
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

    def _parse_span(self, raw: str, fallback: tuple[int, int, int, int]):
        """把 "09:00-22:30" 解析成 (开始时分, 结束时分)。"""
        try:
            left, right = str(raw).split("-", 1)
            sh, sm = self._parse_time(left, (fallback[0], fallback[1]))
            eh, em = self._parse_time(right, (fallback[2], fallback[3]))
            return sh, sm, eh, em
        except Exception:
            return fallback

    def _memory_dir(self) -> Path:
        raw = str(self._cfg("memory_dir", "")).strip() or DEFAULT_MEMORY_DIR
        return Path(raw)

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default))
        except Exception:
            return default

    # ---------- 固定时间（早晚安） ----------

    def _next_run(self, now: datetime) -> datetime:
        morning = self._parse_time(self._cfg("morning_time", "08:00"), (8, 0))
        night = self._parse_time(self._cfg("night_time", "23:00"), (23, 0))
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
        night = self._parse_time(self._cfg("night_time", "23:00"), (23, 0))
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

    # ---------- 随机主动消息 ----------

    async def _random_loop(self):
        if not bool(self._cfg("random_enable", True)):
            logger.info("[daily_ritual] 随机主动消息已关闭")
            return
        await asyncio.sleep(20)
        while True:
            try:
                await self._maybe_random()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[daily_ritual] 主动消息检查出错: {exc}", exc_info=True)
            await asyncio.sleep(60)

    def _in_window(self, now: datetime) -> bool:
        sh, sm, eh, em = self._parse_span(self._cfg("random_window", "09:00-22:30"), (9, 0, 22, 30))
        start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
        return start <= now <= end

    def _last_user_at(self) -> datetime | None:
        """从 raw 聊天记录里取他最后一次说话的时间。"""
        raw = self._memory_dir() / "raw"
        files = sorted(raw.glob("*.jsonl"))
        for path in reversed(files):
            for line in reversed(path.read_text(encoding="utf-8", errors="ignore").splitlines()):
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not str(row.get("user", "")).strip():
                    continue
                ts = str(row.get("ts", ""))
                try:
                    return datetime.fromisoformat(ts).replace(tzinfo=self._tz())
                except Exception:
                    continue
        return None

    def _random_state(self, state: dict, now: datetime) -> dict:
        rnd = state.get("random")
        if not isinstance(rnd, dict):
            rnd = {}
        if rnd.get("date") != now.date().isoformat():
            rnd = {"date": now.date().isoformat(), "count": 0}
        return rnd

    def _random_ready(self, now: datetime, state: dict) -> tuple[bool, str]:
        if not bool(self._cfg("random_enable", True)):
            return False, "off"
        if not self._in_window(now):
            return False, "outside_window"
        rnd = self._random_state(state, now)
        if int(rnd.get("count", 0)) >= self._int("random_daily_max", 4):
            return False, "daily_max"
        if int(state.get("unanswered", 0)) >= self._int("random_max_unanswered", 3):
            return False, "unanswered_max"

        last_user = self._last_user_at()
        idle_minutes = self._int("random_idle_after_user_minutes", 45)
        if last_user and (now - last_user).total_seconds() < idle_minutes * 60:
            return False, "user_active"

        gap = int(rnd.get("gap_minutes") or 0)
        if gap <= 0:
            gap = random.randint(
                self._int("random_min_gap_minutes", 60),
                max(self._int("random_min_gap_minutes", 60), self._int("random_max_gap_minutes", 180)),
            )
        last_at = rnd.get("last_at")
        if last_at:
            try:
                last_dt = datetime.fromisoformat(str(last_at))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=self._tz())
                if now < last_dt + timedelta(minutes=gap):
                    return False, "waiting"
            except Exception:
                pass
        else:
            # 刚启动时先暖一会儿，别一上线就冒出来
            if now < self._started_at + timedelta(minutes=min(gap, 30)):
                return False, "warming_up"
        return True, "due"

    async def _maybe_random(self) -> None:
        now = datetime.now(self._tz())
        async with self._lock:
            state = self._load_state()
            self._refresh_unanswered(state, now)
            ready, reason = self._random_ready(now, state)
            if not ready:
                logger.debug(f"[daily_ritual] 主动消息暂不发：{reason}")
                return
            text = await self._generate("random")
            ok = await self.context.send_message(
                self._resolve_umo(), MessageChain([Plain(text)], use_markdown_=False)
            )
            logger.info(f"[daily_ritual] random 投递结果={ok} reason={reason}")
            if not ok:
                return
            rnd = self._random_state(state, now)
            rnd["count"] = int(rnd.get("count", 0)) + 1
            rnd["last_at"] = now.isoformat(timespec="seconds")
            rnd["gap_minutes"] = random.randint(
                self._int("random_min_gap_minutes", 60),
                max(self._int("random_min_gap_minutes", 60), self._int("random_max_gap_minutes", 180)),
            )
            rnd["text"] = text
            state["random"] = rnd
            state["unanswered"] = int(state.get("unanswered", 0)) + 1
            self._write_state(state)

    def _refresh_unanswered(self, state: dict, now: datetime) -> None:
        """他回过消息就清零；顺便补记最近一次他说话的时间。"""
        last_user = self._last_user_at()
        if not last_user:
            return
        stamp = last_user.isoformat(timespec="seconds")
        if state.get("last_user_ts") != stamp:
            state["last_user_ts"] = stamp
            state["unanswered"] = 0
            self._write_state(state)

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
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_state(self, state: dict) -> None:
        try:
            self._state_path().write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning(f"[daily_ritual] 保存状态失败: {exc}")

    def _save_state(self, kind: str, text: str) -> None:
        state = self._load_state()
        state[kind] = {"text": text, "at": datetime.now(self._tz()).isoformat(timespec="seconds")}
        self._write_state(state)

    # ---------- 生成与发送 ----------

    async def _build_prompt(self, kind: str) -> str:
        now = datetime.now(self._tz())
        weekday = "一二三四五六日"[now.weekday()]
        if kind == "morning":
            scene = "现在是早上，他该起床了。你要叫他起来，顺便打个招呼。"
            angles = MORNING_ANGLES
        elif kind == "night":
            scene = "现在已经很晚了，他还没睡。你要催他去睡，跟他说晚安。"
            angles = NIGHT_ANGLES
        else:
            scene = "现在是白天，你们已经有一阵没说话。你想起来他，主动找他说一句。"
            angles = DAYTIME_ANGLES
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

    async def _generate(self, kind: str) -> str:
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
        return text

    @staticmethod
    def _clean(text: str) -> str:
        text = CONTROL_TAG_RE.sub("", text).strip()
        text = text.strip("「」『』\"'` \n")
        text = re.sub(r"^(大肥鱼|小鲸鱼|我)[：:]\s*", "", text)
        text = re.sub(r"\n{2,}", "\n", text)
        return text[:200].strip()

    async def _send(self, kind: str) -> str:
        async with self._lock:
            umo = self._resolve_umo()
            text = await self._generate(kind)
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
        """测试用：/ritual test morning|night|random、/ritual status"""
        raw = (getattr(event, "message_str", "") or "").strip()
        tokens = raw.split()
        if tokens and tokens[0].lstrip("/").lower() == "ritual":
            tokens = tokens[1:]
        action = tokens[0].lower() if tokens else "status"

        if action == "status":
            now = datetime.now(self._tz())
            target = self._next_run(now)
            state = self._load_state()
            rnd = self._random_state(state, now)
            ready, reason = self._random_ready(now, state)
            yield event.plain_result(
                f"下一次早晚安：{target:%Y-%m-%d %H:%M}\n"
                f"早上 {self._cfg('morning_time', '08:00')} / 晚上 {self._cfg('night_time', '23:00')}\n"
                f"主动消息：{'开' if self._cfg('random_enable', True) else '关'}"
                f"（窗口 {self._cfg('random_window', '09:00-22:30')}，"
                f"{self._cfg('random_min_gap_minutes', 60)}~{self._cfg('random_max_gap_minutes', 180)} 分钟，"
                f"今天已发 {int(rnd.get('count', 0))}/{self._cfg('random_daily_max', 4)} 条，"
                f"未回复 {int(state.get('unanswered', 0))} 次，当前：{reason}）\n"
                f"目标会话：{self._resolve_umo() or '（还没找到）'}"
            )
            return

        if action == "test":
            kind = tokens[1].lower() if len(tokens) > 1 else "morning"
            if kind not in ("morning", "night", "random"):
                yield event.plain_result("用法：/ritual test morning|night|random")
                return
            try:
                if kind == "random":
                    umo = self._resolve_umo()
                    text = await self._generate("random")
                    ok = await self.context.send_message(
                        umo, MessageChain([Plain(text)], use_markdown_=False)
                    )
                    logger.info(f"[daily_ritual] random(手动) 投递结果={ok}")
                else:
                    text = await self._send(kind)
            except Exception as exc:
                yield event.plain_result(f"生成失败：{exc}")
                return
            yield event.plain_result(f"已发送（{kind}）：{text}")
            return

        yield event.plain_result(
            "用法：/ritual status、/ritual test morning、/ritual test night、/ritual test random"
        )
