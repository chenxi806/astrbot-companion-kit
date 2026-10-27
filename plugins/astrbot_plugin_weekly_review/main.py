"""每周复盘插件（AstrBot v4）。

每周日 21:00 自动跑一次"复盘"：
1. 读 store.json / 最近事件 / 最近一周聊天 / 当前判断
2. 让 LLM 产出「加 / 改 / 删」变更提案（带证据，最多 5 条）
3. 写到 memory/proposals/，并私聊通知你
4. 你回一句「同意」，它把变更写进 store.json、跑 render.py、更新判断卡
5. 回「驳回」就不动

命令：
- /review status   看有没有待审提案、下次什么时候跑
- /review show     看提案内容
- /review now      立刻跑一次（强制覆盖旧提案）
- /review reject   丢弃待审提案
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
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


def _default_memory_dir() -> Path:
    """默认记忆目录：<AstrBot data>/companion_memory。

    想换位置就在插件配置里填 memory_dir（所有插件填同一个）。
    """
    try:
        base = Path(StarTools.get_data_dir("astrbot_plugin_weekly_review"))
        return base.parent.parent / "companion_memory"
    except Exception:
        return Path.home() / "companion_memory"

SCOPES = {"dev", "companion"}

APPROVE_RE = r"^[/／]?(同意|批准|确认)[。.!！]?$"
REJECT_RE = r"^[/／]?(驳回|算了|先放着|不要)[。.!！]?$"


def _ev_text(evidence) -> str:
    """证据既可能是旧字符串，也可能是 {date, quote} 结构。"""
    if isinstance(evidence, dict):
        return str(evidence.get("quote", ""))
    return str(evidence or "")


@register(
    "astrbot_plugin_weekly_review",
    "local",
    "每周自动复盘：读记忆和聊天，产出判断变更提案，回「同意」就写入",
    "1.0.0",
)
class WeeklyReviewPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ---------- 生命周期 ----------

    async def initialize(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info("[weekly_review] 调度器已启动")

    async def terminate(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ---------- 基础 ----------

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

    def _memory_dir(self) -> Path:
        raw = str(self._cfg("memory_dir", "")).strip()
        return Path(raw) if raw else _default_memory_dir()

    def _state_path(self) -> Path:
        try:
            base = Path(StarTools.get_data_dir("astrbot_plugin_weekly_review"))
        except Exception:
            base = self._memory_dir() / "review_state"
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
            logger.warning(f"[weekly_review] 保存状态失败: {exc}")

    def _resolve_umo(self) -> str:
        configured = str(self._cfg("umo", "")).strip()
        if configured:
            return configured
        raw = self._memory_dir() / "raw"
        for path in reversed(sorted(raw.glob("*.jsonl"))):
            for line in reversed(path.read_text(encoding="utf-8", errors="ignore").splitlines()):
                try:
                    umo = json.loads(line).get("umo") or ""
                except Exception:
                    continue
                if umo:
                    return umo
        return ""

    @staticmethod
    def _is_private(event: AstrMessageEvent) -> bool:
        try:
            return event.get_message_type().value == "FriendMessage"
        except Exception:
            return False

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        """AstrBot 的 is_admin 只看平台 role，私聊里通常是 member，所以单独判。"""
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        owner = str(self._cfg("owner_id", "")).strip()
        if not owner:
            return True  # 没配 owner_id 时，私聊里就认作主人
        try:
            return str(event.get_sender_id()) == owner
        except Exception:
            return False

    # ---------- 调度 ----------

    def _next_run(self, now: datetime) -> datetime:
        weekday = int(self._cfg("weekday", 6))  # 0=周一 … 6=周日
        raw = str(self._cfg("time", "21:00")).strip()
        try:
            hour, minute = (int(x) for x in raw.split(":"))
        except Exception:
            hour, minute = 21, 0
        for offset in range(0, 9):
            day = (now + timedelta(days=offset)).date()
            if day.weekday() != weekday:
                continue
            moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=now.tzinfo)
            if moment > now + timedelta(seconds=15):
                return moment
        raise RuntimeError("算不出下一次复盘时间")

    async def _loop(self):
        await asyncio.sleep(15)
        while True:
            try:
                if self._due_now():
                    await self._run(force=False)
                await asyncio.sleep(1200)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[weekly_review] 调度循环出错: {exc}", exc_info=True)
                await asyncio.sleep(300)

    def _count_messages(self) -> int:
        raw = self._memory_dir() / "raw"
        count = 0
        for path in raw.glob("*.jsonl"):
            try:
                count += sum(
                    1
                    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
                    if line.strip()
                )
            except OSError:
                continue
        return count

    def _due_now(self) -> bool:
        """周日到点，或累积够量，就跑一次；有待审提案时不跑。"""
        state = self._load_state()
        if state.get("pending"):
            return False
        now = datetime.now(self._tz())
        today = now.date().isoformat()
        weekday = int(self._cfg("weekday", 6))
        try:
            hour, minute = (int(x) for x in str(self._cfg("time", "21:00")).split(":"))
        except Exception:
            hour, minute = 21, 0
        if (
            now.weekday() == weekday
            and (now.hour, now.minute) >= (hour, minute)
            and state.get("last_scheduled") != today
        ):
            return True
        threshold = int(self._cfg("volume_trigger", 50))
        if threshold > 0:
            if self._count_messages() - int(state.get("last_trigger_total", 0)) >= threshold:
                return True
        return False

    def _decay(self, store: dict) -> bool:
        """没被强化的判断，按周轻微降置信度；低于 0.45 标 stale。"""
        if not bool(self._cfg("decay_enabled", True)):
            return False
        today = date.today()
        changed = False
        for item in store.get("judgments", []):
            if item.get("status", "active") != "active":
                continue
            try:
                updated = date.fromisoformat(str(item.get("updated") or item.get("created")))
            except Exception:
                continue
            weeks = (today - updated).days // 7
            done = int(item.get("decayed_weeks", 0))
            if weeks <= done:
                continue
            drop = min(weeks - done, 5) * 0.01
            item["confidence"] = round(max(0.3, float(item.get("confidence", 0.6)) - drop), 3)
            item["decayed_weeks"] = weeks
            item["stale"] = item["confidence"] < 0.45
            changed = True
        return changed

    # ---------- 收集素材 ----------

    def _read_store(self) -> dict:
        path = self._memory_dir() / "store.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _collect_events(self, days: int = 14) -> str:
        cutoff = (datetime.now(self._tz()) - timedelta(days=days)).date().isoformat()
        lines: list[str] = []
        for path in sorted((self._memory_dir() / "events").glob("*.md")):
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                match = re.match(r"^- \[(\d{4}-\d{2}-\d{2})\]\s*(.+)$", line.strip())
                if match and match.group(1) >= cutoff:
                    lines.append(f"- {match.group(1)} {match.group(2)[:200]}")
        return "\n".join(lines[-40:])

    def _collect_dialogue(self, days: int = 7, limit: int = 120) -> str:
        cutoff = (datetime.now(self._tz()) - timedelta(days=days)).isoformat()
        rows: list[dict] = []
        for path in sorted((self._memory_dir() / "raw").glob("*.jsonl"))[-8:]:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if str(row.get("ts", "")) >= cutoff:
                    rows.append(row)
        rows = rows[-limit:]
        out: list[str] = []
        for row in rows:
            stamp = str(row.get("ts", ""))[5:16].replace("T", " ")
            user = re.sub(r"\s+", " ", str(row.get("user", ""))).strip()
            assistant = re.sub(r"\s+", " ", str(row.get("assistant", ""))).strip()
            if user:
                out.append(f"[{stamp}] 他：{user[:120]}")
            if assistant:
                out.append(f"[{stamp}] 她：{assistant[:120]}")
        return "\n".join(out)

    @staticmethod
    def _judgments_digest(store: dict) -> str:
        rows = []
        for item in store.get("judgments", []):
            if item.get("status", "active") != "active":
                continue
            linked = ",".join(item.get("insights") or []) or "无"
            rows.append(
                f"- {item['id']}（{item.get('confidence', 0.5)}，{'/'.join(item.get('scope', []))}，洞察:{linked}）：{item['text']}"
            )
        return "\n".join(rows) or "（暂无）"

    @staticmethod
    def _insights_digest(store: dict) -> str:
        rows = []
        for item in store.get("insights", []):
            if item.get("status") in ("promoted", "dropped"):
                continue
            rows.append(
                f"- {item['id']}（{item.get('confidence', 0.5)}，{item.get('status', 'emerging')}，"
                f"证据{len(item.get('evidence') or [])}条）：{item['text']}"
            )
        return "\n".join(rows) or "（暂无）"

    # ---------- 让模型产出提案 ----------

    async def _ask_llm(self, store: dict, events: str, dialogue: str) -> dict | None:
        umo = self._resolve_umo()
        provider = self.context.get_using_provider(umo) if umo else self.context.get_using_provider()
        if provider is None:
            logger.error("[weekly_review] 没有可用的聊天 Provider")
            return None

        system = "你是一个记忆整理助手。你只输出一个 JSON 对象，不输出任何解释、前后缀或代码块标记。"
        prompt = f"""下面是关于"他"的现有洞察和判断、最近两周的事件、最近一周的聊天。

【现有洞察（还没定论的观察）】
{self._insights_digest(store)}

【现有判断（已经决定的做法）】
{self._judgments_digest(store)}

【最近事件】
{events or "（无）"}

【最近聊天】
{dialogue or "（无）"}

任务：按「事件 → 洞察 → 判断」三层整理。先判断这段时间说明了什么（洞察，允许错），
再从洞察里挑够格的升成判断。只输出 JSON：

{{
  "insights": [{{"ref": "i1", "text": "这说明什么", "confidence": 0.55, "evidence": "哪天的什么事"}}],
  "add": [{{"text": "新的判断", "scope": ["companion"], "confidence": 0.6, "evidence": "证据", "insight": "i1"}}],
  "update": [{{"id": "现有判断id", "text": "改后的句子（不改可省略）", "confidence": 0.8, "reason": "为什么", "evidence": "证据", "insight": "i1 或已有洞察id"}}],
  "delete": [{{"id": "要归档的判断id", "reason": "为什么", "insight": "i1 或已有洞察id"}}],
  "noop": [{{"id": "看过了但决定不改的判断id", "reason": "为什么这次不动"}}],
  "observe": ["还拿不准的现象"]
}}

规则：
1. 单次情绪只能写进 insights，不能直接改判断。判断变更必须引用 insight。
2. insight 可以引用本次新建的 ref（i1、i2），也可以引用上面【现有洞察】里的 id。
3. 判断变更总数（add+update+delete）最多 3 条；洞察最多 5 条。
4. 每条都必须有 evidence，写清日期和原话大意。
5. add 的 scope 只能是 "companion" / "dev" / 两者；不知道就填 ["companion"]。
6. 没有要改的就返回空数组，不要为了改而改。
7. 不要新增事实（facts）。
8. 【矛盾检查】把现有判断两两过一遍，同 scope 且互相打架的（比如「累了先陪着」和「累了要哄」），必须出一条洞察并给出改/删建议。
9. 【NOOP】认真看过但决定不改的判断，放进 noop 并写一句理由；这是给下周的你看的，不是凑数。
"""
        try:
            response = await provider.text_chat(
                prompt=prompt, system_prompt=system, contexts=[]
            )
            text = getattr(response, "completion_text", "") or ""
        except Exception as exc:
            logger.error(f"[weekly_review] 调用模型失败: {exc}")
            return None

        data = self._extract_json(text)
        if data is None:
            logger.warning(f"[weekly_review] 模型输出不是合法 JSON: {text[:200]}")
            return None
        return self._normalize(data)

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : index + 1])
                    except json.JSONDecodeError:
                        return None
        return None

    @staticmethod
    def _normalize(data: dict) -> dict:
        def clamp(value, low=0.3, high=0.9):
            try:
                return max(low, min(high, float(value)))
            except Exception:
                return 0.6

        def scopes(value):
            out = [s for s in (value or []) if s in SCOPES]
            return out or ["companion"]

        def evidence(value):
            text = str(value or "").strip()[:200]
            if not text:
                return None
            match = re.match(r"(\d{4}-\d{2}-\d{2}|\d{2}-\d{2})", text)
            date = match.group(1) if match else ""
            return {"date": date, "quote": text}

        insights, add, update, delete, noop, observe = [], [], [], [], [], []
        for item in (data.get("insights") or [])[:5]:
            text = str(item.get("text", "")).strip()[:120]
            ref = str(item.get("ref", "")).strip()
            if text and ref:
                insights.append(
                    {
                        "ref": ref,
                        "text": text,
                        "confidence": clamp(item.get("confidence", 0.55), 0.3, 0.8),
                        "evidence": evidence(item.get("evidence")),
                    }
                )
        for item in (data.get("add") or [])[:3]:
            text = str(item.get("text", "")).strip()[:120]
            if text:
                add.append(
                    {
                        "text": text,
                        "scope": scopes(item.get("scope")),
                        "confidence": clamp(item.get("confidence", 0.6)),
                        "evidence": evidence(item.get("evidence")),
                        "insight": str(item.get("insight", "")).strip(),
                    }
                )
        for item in (data.get("update") or [])[:3]:
            target = str(item.get("id", "")).strip()
            if target:
                update.append(
                    {
                        "id": target,
                        "text": str(item.get("text", "")).strip()[:120] or None,
                        "confidence": clamp(item.get("confidence", 0.7)),
                        "reason": str(item.get("reason", "")).strip()[:200],
                        "evidence": evidence(item.get("evidence")),
                        "insight": str(item.get("insight", "")).strip(),
                    }
                )
        for item in (data.get("delete") or [])[:3]:
            target = str(item.get("id", "")).strip()
            if target:
                delete.append(
                    {
                        "id": target,
                        "reason": str(item.get("reason", "")).strip()[:200],
                        "insight": str(item.get("insight", "")).strip(),
                    }
                )
        for item in (data.get("noop") or [])[:5]:
            target = str(item.get("id", "")).strip()
            if target:
                noop.append(
                    {
                        "id": target,
                        "reason": str(item.get("reason", "")).strip()[:160],
                    }
                )
        for item in (data.get("observe") or [])[:3]:
            text = str(item).strip()[:120]
            if text:
                observe.append(text)

        return {
            "insights": insights,
            "add": add,
            "update": update,
            "delete": delete,
            "noop": noop,
            "observe": observe,
        }

    # ---------- 写提案 ----------

    def _week_label(self, now: datetime) -> str:
        return f"{now:%Y}-W{now.isocalendar().week:02d}"

    def _write_proposal(self, result: dict) -> tuple[Path, Path]:
        now = datetime.now(self._tz())
        label = self._week_label(now)
        folder = self._memory_dir() / "proposals"
        folder.mkdir(parents=True, exist_ok=True)
        md_path = folder / f"{label}.md"
        json_path = folder / f"{label}.json"

        lines = [f"# 复盘 {label}（{now:%m-%d}）", ""]
        lines.append("## 洞察（先说这说明什么）")
        lines += [
            f"- [{i['ref']}] {i['text']}（{i['confidence']}）← 证据：{_ev_text(i.get('evidence')) or '（无）'}"
            for i in result["insights"]
        ] or ["- （无）"]
        lines.append("")
        lines.append("## 判断：加")
        lines += [
            f"- [{i['confidence']}][{'/'.join(i['scope'])}] {i['text']} ← 来自 {i['insight'] or '（无洞察）'}；证据：{_ev_text(i.get('evidence')) or '（无）'}"
            for i in result["add"]
        ] or ["- （无）"]
        lines.append("")
        lines.append("## 判断：改")
        lines += [
            f"- `{i['id']}`：→ {i['confidence']}，{i['text'] or '（只调置信度）'} ← 来自 {i['insight'] or '（无洞察）'}；{i['reason'] or _ev_text(i.get('evidence')) or ''}"
            for i in result["update"]
        ] or ["- （无）"]
        lines.append("")
        lines.append("## 判断：删")
        lines += [
            f"- `{i['id']}`：{i['reason']} ← 来自 {i['insight'] or '（无洞察）'}"
            for i in result["delete"]
        ] or ["- （无）"]
        lines.append("")
        lines.append("## 复审未变（NOOP）")
        lines += [
            f"- `{i['id']}`：{i['reason'] or '看过，决定不改'}" for i in result.get("noop", [])
        ] or ["- （无）"]
        lines.append("")
        lines.append("## 观察区")
        lines += [f"- {item}" for item in result["observe"]] or ["- （无）"]
        lines += ["", "---", "状态：待确认（回一句「同意」写入，或「驳回」丢弃）", ""]

        md_path.write_text("\n".join(lines), encoding="utf-8")
        payload = {"week": label, "created_at": now.isoformat(timespec="seconds"), **result}
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return md_path, json_path

    # ---------- 通知 ----------

    async def _notify(self, result: dict) -> bool:
        umo = self._resolve_umo()
        if not umo:
            logger.warning("[weekly_review] 找不到会话，提案写好了但没法通知")
            return False
        count = len(result["add"]) + len(result["update"]) + len(result["delete"])
        insight_count = len(result.get("insights") or [])
        if count == 0 and insight_count == 0:
            text = "这周我翻了翻我们聊的，好像没什么要改的。就先这样，下周再看。"
        else:
            parts = []
            if insight_count:
                parts.append(f"整理出 {insight_count} 条想法")
            if result["add"]:
                parts.append(f"新记 {len(result['add'])} 条")
            if result["update"]:
                parts.append(f"改 {len(result['update'])} 条")
            if result["delete"]:
                parts.append(f"归档 {len(result['delete'])} 条")
            detail = "、".join(parts)
            text = (
                f"我翻了翻这几天我们聊的，{detail}，想跟你对一下。\n"
                "同意就说「同意」，不想动就说「驳回」，我先不改。"
            )
        try:
            ok = await self.context.send_message(
                umo, MessageChain([Plain(text)], use_markdown_=False)
            )
            logger.info(f"[weekly_review] 通知投递结果={ok} umo={umo}")
            return bool(ok)
        except Exception as exc:
            logger.error(f"[weekly_review] 通知发送失败: {exc}", exc_info=True)
            return False

    # ---------- 应用提案 ----------

    async def _apply(self) -> str:
        state = self._load_state()
        pending = state.get("pending")
        if not pending:
            return "没有待确认的提案。"
        json_path = Path(pending["json"])
        if not json_path.exists():
            return "提案文件不见了，可能已经被处理过。"
        payload = json.loads(json_path.read_text(encoding="utf-8"))

        store_path = self._memory_dir() / "store.json"
        store = json.loads(store_path.read_text(encoding="utf-8"))
        insights = store.setdefault("insights", [])
        judgments = store.setdefault("judgments", [])
        by_insight = {item["id"]: item for item in insights}
        by_id = {item["id"]: item for item in judgments}
        now = datetime.now(self._tz())
        today = now.date().isoformat()
        applied: list[str] = []
        skipped: list[str] = []

        ref_map: dict[str, str] = {}
        for item in payload.get("insights", []):
            new_id = f"i-{now:%Y%m%d}-{len(insights) + 1}"
            while new_id in by_insight:
                new_id += "x"
            entry = {
                "id": new_id,
                "text": item["text"],
                "confidence": item.get("confidence", 0.55),
                "status": "emerging",
                "created": today,
                "updated": today,
                "promoted_to": None,
                "evidence": [item["evidence"]] if item.get("evidence") else [],
            }
            insights.append(entry)
            by_insight[new_id] = entry
            ref_map[str(item.get("ref", ""))] = new_id
            applied.append(f"洞察：{entry['text']}")

        def resolve(ref: str) -> str | None:
            ref = str(ref or "").strip()
            if not ref:
                return None
            return ref_map.get(ref) or (ref if ref in by_insight else None)

        for item in payload.get("add", []):
            insight_id = resolve(item.get("insight", ""))
            if not insight_id:
                skipped.append(f"加「{item.get('text', '')}」：没有引用洞察")
                continue
            new_id = f"j{now:%Y%m%d}-{len(judgments) + 1}"
            while new_id in by_id:
                new_id += "x"
            entry = {
                "id": new_id,
                "text": item["text"],
                "scope": item.get("scope") or ["companion"],
                "confidence": item.get("confidence", 0.6),
                "status": "active",
                "created": today,
                "updated": today,
                "supersedes": None,
                "insights": [insight_id],
                "evidence": [item["evidence"]] if item.get("evidence") else [],
            }
            judgments.append(entry)
            by_id[new_id] = entry
            applied.append(f"加：{entry['text']}")
            _promote(by_insight[insight_id], new_id)

        for item in payload.get("update", []):
            entry = by_id.get(item["id"])
            insight_id = resolve(item.get("insight", ""))
            if not entry or not insight_id:
                skipped.append(f"改「{item.get('id', '')}」：没有引用洞察或判断不存在")
                continue
            if item.get("text"):
                entry["text"] = item["text"]
            entry["confidence"] = item.get("confidence", entry.get("confidence", 0.6))
            entry["updated"] = today
            linked = entry.setdefault("insights", [])
            if insight_id not in linked:
                linked.append(insight_id)
            evidence = entry.setdefault("evidence", [])
            new_quote = _ev_text(item.get("evidence"))
            if new_quote and not any(_ev_text(old) == new_quote for old in evidence):
                evidence.append(item["evidence"])
            entry["last_reviewed"] = today
            applied.append(f"改：{entry['text']}（{entry['confidence']}）")

        for item in payload.get("delete", []):
            entry = by_id.get(item["id"])
            insight_id = resolve(item.get("insight", ""))
            if not entry or not insight_id:
                skipped.append(f"删「{item.get('id', '')}」：没有引用洞察或判断不存在")
                continue
            entry["status"] = "archived"
            entry["updated"] = today
            entry["last_reviewed"] = today
            applied.append(f"归档：{entry['text']}")
            _promote(by_insight[insight_id], None)

        for item in payload.get("noop", []):
            entry = by_id.get(item["id"])
            if not entry:
                continue
            entry["last_reviewed"] = today
            entry["noop_reason"] = item.get("reason", "")
            applied.append(f"复审未变：{entry['text'][:16]}")

        for insight in by_insight.values():
            if len([e for e in (insight.get("evidence") or []) if e]) >= 2 and insight.get("status") == "emerging":
                insight["status"] = "reinforced"

        store["updated"] = today
        tmp = store_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, store_path)

        self._append_changelog(payload, applied, today)
        await self._render_card()

        state.pop("pending", None)
        state["last_applied"] = today
        self._save_state(state)
        self._mark_md(payload["week"])

        if not applied:
            return "这周的提案没有实际变更，已经处理掉了。"
        head = "，".join(applied[:3])
        more = f" 等 {len(applied)} 条" if len(applied) > 3 else ""
        tail = f"（跳过 {len(skipped)} 条没引用洞察的）" if skipped else ""
        return f"好，我记下了：{head}{more}。以后我按这个来。{tail}"

    def _append_changelog(self, payload: dict, applied: list[str], today: str) -> None:
        path = self._memory_dir() / "changelog.md"
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n## {today}（{payload.get('week', '')} 自动复盘）\n\n")
                for line in applied:
                    handle.write(f"- {line}\n")
                handle.write("- 由 weekly_review 插件写入，人工已确认。\n")
        except OSError as exc:
            logger.warning(f"[weekly_review] 写 changelog 失败: {exc}")

    def _mark_md(self, week: str) -> None:
        path = self._memory_dir() / "proposals" / f"{week}.md"
        try:
            text = path.read_text(encoding="utf-8")
            text = text.replace("状态：待确认", f"状态：已确认 {datetime.now(self._tz()):%Y-%m-%d}")
            path.write_text(text, encoding="utf-8")
        except OSError:
            pass

    async def _render_card(self) -> None:
        """直接调用 memory/render.py 的函数重渲染，避免起子进程。"""
        script = self._memory_dir() / "render.py"
        if not script.exists():
            logger.warning("[weekly_review] 找不到 render.py，跳过重渲染")
            return
        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location("memory_render", script)
            if spec is None or spec.loader is None:
                raise RuntimeError("无法加载 render.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            store = module.load_store(self._memory_dir() / "store.json")
            (self._memory_dir() / "card.md").write_text(
                module.render_markdown(store), encoding="utf-8"
            )
            (self._memory_dir() / "card.companion.txt").write_text(
                module.render_companion(store), encoding="utf-8"
            )
        except Exception as exc:
            logger.error(f"[weekly_review] 重渲染判断卡失败: {exc}")

    # ---------- 主流程 ----------

    async def _run(self, force: bool) -> dict | None:
        async with self._lock:
            state = self._load_state()
            pending = state.get("pending")
            if pending and not force:
                try:
                    created = datetime.fromisoformat(pending["created_at"])
                    age = (datetime.now(self._tz()) - created).days
                except Exception:
                    age = 0
                if age > 14:
                    logger.info("[weekly_review] 旧提案超过 14 天没人处理，丢弃后重新生成")
                    state.pop("pending", None)
                    self._save_state(state)
                else:
                    logger.info("[weekly_review] 已有待确认提案，跳过本周")
                    return None
            store = self._read_store()
            if self._decay(store):
                store_path = self._memory_dir() / "store.json"
                tmp = store_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, store_path)
                logger.info("[weekly_review] 已按周衰减未强化的判断")
            events = self._collect_events()
            dialogue = self._collect_dialogue()
            result = await self._ask_llm(store, events, dialogue)
            if result is None:
                return None
            md_path, json_path = self._write_proposal(result)
            state["pending"] = {
                "week": self._week_label(datetime.now(self._tz())),
                "md": str(md_path),
                "json": str(json_path),
                "created_at": datetime.now(self._tz()).isoformat(timespec="seconds"),
            }
            state["last_run"] = datetime.now(self._tz()).isoformat(timespec="seconds")
            state["last_trigger_total"] = self._count_messages()
            run_now = datetime.now(self._tz())
            try:
                hh, mm = (int(x) for x in str(self._cfg("time", "21:00")).split(":"))
            except Exception:
                hh, mm = 21, 0
            if run_now.weekday() == int(self._cfg("weekday", 6)) and (run_now.hour, run_now.minute) >= (hh, mm):
                state["last_scheduled"] = run_now.date().isoformat()
            self._save_state(state)
            result["_notify_ok"] = await self._notify(result)
            return result

    # ---------- 命令 ----------

    @filter.command("review")
    async def review_command(self, event: AstrMessageEvent):
        raw = (getattr(event, "message_str", "") or "").strip()
        tokens = raw.split()
        if tokens and tokens[0].lstrip("/").lower() == "review":
            tokens = tokens[1:]
        action = tokens[0].lower() if tokens else "status"
        state = self._load_state()
        pending = state.get("pending")

        if action == "status":
            now = datetime.now(self._tz())
            nxt = self._next_run(now)
            lines = [f"下一次自动复盘：{nxt:%Y-%m-%d %H:%M}"]
            if pending:
                lines.append(f"待确认：{pending['week']}（{pending['created_at']}）")
            else:
                lines.append("没有待确认的提案。")
            yield event.plain_result("\n".join(lines))
            return

        if action == "show":
            if not pending:
                yield event.plain_result("现在没有待确认的提案。")
                return
            path = Path(pending["md"])
            text = path.read_text(encoding="utf-8") if path.exists() else "文件不见了"
            yield event.plain_result(text[:1500])
            return

        if action == "reject":
            if pending:
                state.pop("pending", None)
                self._save_state(state)
                yield event.plain_result("好，这周的提案我丢掉了。")
            else:
                yield event.plain_result("本来就没有待确认的提案。")
            return

        if action == "now":
            if not pending and not self._is_owner(event):
                yield event.plain_result("只有管理员能强制跑复盘。")
                return
            try:
                result = await self._run(force=True)
            except Exception as exc:
                yield event.plain_result(f"跑失败了：{exc}")
                return
            if result is None:
                yield event.plain_result("这次没生成提案（模型没返回或出错），看日志。")
                return
            count = len(result["add"]) + len(result["update"]) + len(result["delete"])
            if result.get("_notify_ok"):
                tail = "已发到你私聊。"
            else:
                tail = "但通知没发出去，先用 /review show 看内容。"
            yield event.plain_result(f"跑完了，{count} 条变更。{tail}")
            return

        yield event.plain_result("用法：/review status、/review show、/review now、/review reject")

    # ---------- 确认 ----------

    @filter.regex(APPROVE_RE)
    async def approve(self, event: AstrMessageEvent):
        if not self._is_private(event) or not self._is_owner(event):
            return
        state = self._load_state()
        if not state.get("pending"):
            return  # 没有待审提案就当普通消息放过去
        try:
            message = await self._apply()
        except Exception as exc:
            logger.error(f"[weekly_review] 应用提案失败: {exc}", exc_info=True)
            message = f"写入失败了：{exc}"
        yield event.plain_result(message)

    @filter.regex(REJECT_RE)
    async def reject(self, event: AstrMessageEvent):
        if not self._is_private(event) or not self._is_owner(event):
            return
        state = self._load_state()
        if not state.get("pending"):
            return
        state.pop("pending", None)
        self._save_state(state)
        yield event.plain_result("好，这周的提案我丢掉了，判断卡没动。")

def _promote(insight: dict, judgment_id: str | None) -> None:
    insight["status"] = "promoted"
    if judgment_id:
        insight["promoted_to"] = judgment_id
    insight["updated"] = datetime.now().isoformat(timespec="seconds")[:10]
