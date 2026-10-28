"""判断卡审批（AstrBot v4）。

自动从聊天记录里提取事实/判断/雷区/洞察，攒成待审清单；
用户回「通过 1,3」才写进判断卡。
"""

from __future__ import annotations

import asyncio
import difflib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.message_components import MessageChain, Plain
except Exception:  # pragma: no cover
    MessageChain = None  # type: ignore
    Plain = None  # type: ignore

HEADER = "判断卡待审"

PROMPT = """你是一个记忆整理助手。下面是你和主人的聊天记录。

请提取值得长期记住的信息，分四类：
- facts：稳定的事实（生日、课表、忌口、称呼、习惯）
- judgments：他希望你以后怎么做的规则或偏好（要写成“以后怎么做”）
- taboos：绝对不能碰的底线
- insights：还不确定、需要继续观察的猜测

要求：
1. 每条不超过 60 字，判断要具体、可执行
2. 每条必须带 quote，引用他的一句原话
3. 一次最多 6 条，宁缺毋滥
4. 不要重复已有的内容
5. 只输出 JSON，不要解释

输出格式：
{"facts":[{"text":"...","quote":"..."}],"judgments":[...],"taboos":[...],"insights":[...]}

已有内容：
{existing}

聊天记录：
{chat}
"""


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", str(text or ""))


def _similar(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


@register("astrbot_plugin_card_review", "local", "判断卡审批：自动提取四类信息，用户只回通过/驳回", "1.0.0")
class CardReview(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._task = asyncio.create_task(self._loop())
        self._month_task = asyncio.create_task(self._monthly_loop())

    def _cfg(self, key: str, default=None):
        value = self.config.get(key, default) if hasattr(self.config, "get") else None
        return default if value is None else value

    def _memory(self) -> Path:
        raw = str(self._cfg("memory_dir", "")).strip()
        if not raw:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            raw = str(get_astrbot_data_path() / "companion_memory")
        return Path(raw)

    def _card_dir(self) -> Path:
        path = self._memory() / "card"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _load(self, name: str, default):
        path = self._card_dir() / name
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _save(self, name: str, data) -> None:
        path = self._card_dir() / name
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _render_card_files(self) -> None:
        store = self._load("store.json", {"facts": [], "judgments": [], "taboos": [], "insights": []})
        facts = store.get("facts", [])
        judgments = [j for j in store.get("judgments", []) if j.get("status", "active") == "active"]
        taboos = store.get("taboos", [])
        md = ["# 她的判断卡", "", "## 事实"]
        md += [f"- {f.get('text', '')}" for f in facts] or ["- （空）"]
        md += ["", "## 判断"]
        md += [f"- `{j.get('id')}` [{j.get('confidence')}] {j.get('text', '')}" for j in judgments] or ["- （空）"]
        md += ["", "## 雷区"]
        md += [f"- {t.get('text', '')}" for t in taboos] or ["- （空）"]
        (self._card_dir() / "card.md").write_text("\n".join(md) + "\n", encoding="utf-8")
        lines = ["关于他，你记得的："]
        if facts:
            lines.append("【事实】" + "；".join(f.get("text", "") for f in facts))
        if judgments:
            lines.append("【判断】" + "；".join(j.get("text", "") for j in judgments))
        if taboos:
            lines.append("【雷区】" + "；".join(t.get("text", "") for t in taboos))
        lines.append("（记得就自然用，不要复述这张卡。）")
        (self._card_dir() / "card.companion.txt").write_text("\n".join(lines), encoding="utf-8")

    # ---------- 提取 ----------

    def _read_chat(self) -> tuple[str, str]:
        cursor = self._load("cursor.json", {})
        last = str(cursor.get("last_ts", ""))
        rows = []
        for path in sorted((self._memory() / "raw").glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                ts = str(row.get("ts", ""))
                if ts <= last:
                    continue
                if str(row.get("mode", "")) in ("alice", "maid"):
                    continue
                rows.append((ts, row.get("user", ""), row.get("assistant", "")))
        rows.sort()
        limit = int(self._cfg("max_messages", 120) or 120)
        rows = rows[-limit:]
        if not rows:
            return "", last
        text = "\n".join(f"[{ts}] 他：{u}\n她：{a}" for ts, u, a in rows)
        return text, rows[-1][0]

    def _existing_text(self) -> str:
        store = self._load("store.json", {})
        parts = []
        for key in ("facts", "judgments", "taboos", "insights"):
            for row in store.get(key, []):
                if row.get("status", "active") in ("active", "emerging"):
                    parts.append(f"- {row.get('text', '')}")
        return "\n".join(parts[:40]) or "（无）"

    async def _extract(self, umo: str, force: bool = False) -> str:
        chat, last_ts = self._read_chat()
        if not chat:
            return "这段时间没有新聊天，先不整理。"
        count = chat.count("他：")
        if not force and count < int(self._cfg("min_messages", 12) or 12):
            return f"新聊天只有 {count} 条，攒够再整理。"

        provider = self.context.get_using_provider(umo) if umo else self.context.get_using_provider()
        if provider is None:
            return "没有可用的模型，整理失败。"
        prompt = PROMPT.replace("{existing}", self._existing_text()).replace("{chat}", chat)
        try:
            response = await provider.text_chat(prompt=prompt, system_prompt="只输出 JSON。", contexts=[])
            text = getattr(response, "completion_text", "") or ""
        except Exception as exc:
            logger.error(f"[card_review] 提取失败: {exc}")
            return f"整理失败：{exc}"

        text = text.strip()
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
        try:
            data = json.loads(text)
        except Exception:
            match = re.search(r"\{[\s\S]*\}", text)
            if not match:
                return "模型输出不是 JSON，没整理出来。"
            try:
                data = json.loads(match.group(0))
            except Exception:
                return "模型输出解析失败。"

        store = self._load("store.json", {"facts": [], "judgments": [], "taboos": [], "insights": []})
        existing = []
        for key in ("facts", "judgments", "taboos", "insights"):
            existing += [row.get("text", "") for row in store.get(key, [])]

        items = []
        now = datetime.now()
        for key, kind in (("facts", "fact"), ("judgments", "judgment"), ("taboos", "taboo"), ("insights", "insight")):
            for row in (data.get(key) or [])[:4]:
                text_value = str(row.get("text", "")).strip()[:80]
                if not text_value:
                    continue
                if any(_similar(text_value, old) >= 0.82 for old in existing):
                    continue
                items.append({
                    "id": f"p{now:%y%m%d%H%M%S}{len(items)}",
                    "type": kind,
                    "text": text_value,
                    "quote": str(row.get("quote", ""))[:120],
                    "confidence": 0.75 if kind in ("judgment", "taboo") else 0.6,
                    "date": now.date().isoformat(),
                })
                existing.append(text_value)

        if not items:
            self._save("cursor.json", {"last_ts": last_ts})
            return "看完了，没有需要新记的。"

        pending = self._load("pending.json", {"items": []})
        pending["items"] = (pending.get("items") or []) + items
        pending["created_at"] = now.isoformat(timespec="seconds")
        self._save("pending.json", pending)
        self._save("cursor.json", {"last_ts": last_ts})
        return self._list_pending()

    def _list_pending(self) -> str:
        pending = self._load("pending.json", {"items": []})
        items = pending.get("items") or []
        if not items:
            return "没有待审的内容。"
        labels = {"fact": "事实", "judgment": "判断", "taboo": "雷区", "insight": "洞察"}
        lines = [f"{HEADER}（{len(items)} 条）："]
        for i, item in enumerate(items, 1):
            lines.append(f"{i}. [{labels.get(item['type'], item['type'])}] {item['text']}")
            if item.get("quote"):
                lines.append(f"   原话：{item['quote'][:40]}")
        lines.append("")
        lines.append("回「通过」全部写入；「通过 1,3」只写这几条；「驳回 2」不要这条。")
        return "\n".join(lines)

    # ---------- 审批 ----------

    def _apply(self, selected: list[int] | None) -> str:
        pending = self._load("pending.json", {"items": []})
        items = pending.get("items") or []
        if not items:
            return "没有待审的内容。"
        chosen = set(selected) if selected else set(range(1, len(items) + 1))
        store = self._load("store.json", {"facts": [], "judgments": [], "taboos": [], "insights": []})
        now = datetime.now()
        applied = []
        rest = []
        for i, item in enumerate(items, 1):
            if i not in chosen:
                rest.append(item)
                continue
            text = item["text"]
            kind = item["type"]
            if kind == "fact":
                store.setdefault("facts", []).append({"type": "fact", "text": text, "created": now.date().isoformat()})
                applied.append(f"事实：{text[:20]}")
            elif kind == "taboo":
                store.setdefault("taboos", []).append({"type": "taboo", "text": text, "created": now.date().isoformat()})
                applied.append(f"雷区：{text[:20]}")
            elif kind == "insight":
                store.setdefault("insights", []).append({
                    "id": f"i{now:%y%m%d%H%M%S}{i}", "type": "insight", "text": text,
                    "confidence": item.get("confidence", 0.55), "status": "emerging",
                    "created": now.date().isoformat(), "updated": now.date().isoformat(),
                    "evidence": [{"date": item.get("date"), "quote": item.get("quote", "")}],
                })
                applied.append(f"观察：{text[:20]}")
            else:
                merged = False
                for row in store.setdefault("judgments", []):
                    if row.get("status", "active") == "active" and _similar(text, row.get("text", "")) >= 0.8:
                        row["confidence"] = min(0.95, float(row.get("confidence", 0.7)) + 0.03)
                        row.setdefault("evidence", []).append({"date": item.get("date"), "quote": item.get("quote", "")})
                        row["updated"] = now.date().isoformat()
                        applied.append(f"并入：{row['text'][:20]}")
                        merged = True
                        break
                if not merged:
                    store.setdefault("judgments", []).append({
                        "id": f"j{now:%y%m%d%H%M%S}{i}", "type": "judgment", "text": text,
                        "confidence": item.get("confidence", 0.75), "status": "active",
                        "source": "review", "created": now.date().isoformat(),
                        "updated": now.date().isoformat(), "last_reviewed": now.date().isoformat(),
                        "supersedes": None,
                        "evidence": [{"date": item.get("date"), "quote": item.get("quote", "")}],
                    })
                    applied.append(f"判断：{text[:20]}")
        self._save("store.json", store)
        self._save("pending.json", {"items": rest})
        self._render_card_files()
        if not applied:
            return "没有写入任何内容。"
        head = "；".join(applied[:3])
        more = f" 等 {len(applied)} 条" if len(applied) > 3 else ""
        left = f" 还剩 {len(rest)} 条待审。" if rest else ""
        return f"好，写进去了：{head}{more}。{left}"

    # ---------- 命令 ----------

    def _remember_umo(self, event: AstrMessageEvent) -> None:
        umo = getattr(event, "unified_msg_origin", "")
        if umo:
            self._save("last_umo.json", {"umo": umo})


    # ---------- 月度体检 ----------

    def _build_monthly(self) -> list[dict]:
        store = self._load("store.json", {"facts": [], "judgments": [], "taboos": [], "insights": []})
        judgments = [j for j in store.get("judgments", []) if j.get("status", "active") == "active"]
        insights = [i for i in store.get("insights", []) if i.get("status", "emerging") == "emerging"]
        today = datetime.now().date()
        ops: list[dict] = []

        # 合并相似判断
        merged = set()
        for i in range(len(judgments)):
            for j in range(i + 1, len(judgments)):
                a, b = judgments[i], judgments[j]
                if a["id"] in merged or b["id"] in merged:
                    continue
                if _similar(a.get("text", ""), b.get("text", "")) >= 0.8:
                    keep, drop = (a, b) if float(a.get("confidence", 0)) >= float(b.get("confidence", 0)) else (b, a)
                    ops.append({"kind": "merge", "keep": keep["id"], "drop": drop["id"], "text": drop.get("text", "")[:30]})
                    merged.add(drop["id"])

        # 低分 / 过期
        for j in judgments:
            if j["id"] in merged:
                continue
            try:
                conf = float(j.get("confidence", 0.6))
            except Exception:
                conf = 0.6
            stamp = j.get("updated") or j.get("created") or ""
            try:
                days = (today - __import__("datetime").date.fromisoformat(str(stamp))).days
            except Exception:
                days = 0
            if conf < 0.45 or days > 60:
                ops.append({"kind": "archive", "id": j["id"], "text": j.get("text", "")[:30], "reason": f"分 {conf:.2f}，{days} 天没动"})

        # 洞察升判断
        for ins in insights:
            ev = [e for e in (ins.get("evidence") or []) if e]
            if len(ev) >= 2:
                ops.append({"kind": "promote", "id": ins["id"], "text": ins.get("text", "")[:30]})

        return ops

    def _format_monthly(self, ops: list[dict]) -> str:
        if not ops:
            return "判断卡这个月很干净，没有要处理的。"
        lines = [f"判断卡月度报告（{len(ops)} 项）："]
        for i, op in enumerate(ops, 1):
            if op["kind"] == "merge":
                lines.append(f"{i}. [合并] {op['text']} → {op['keep']}")
            elif op["kind"] == "archive":
                lines.append(f"{i}. [归档] {op['text']}（{op['reason']}）")
            else:
                lines.append(f"{i}. [升级] 观察转判断：{op['text']}")
        lines.append("")
        lines.append("回「月通过」全部执行；「月通过 1,3」只做这几项；「月驳回 2」跳过。")
        return "\n".join(lines)

    def _write_monthly(self, ops: list[dict]) -> None:
        self._save("monthly_pending.json", {"items": ops, "created_at": datetime.now().isoformat(timespec="seconds")})
        lines = [f"# 判断卡月度报告 {datetime.now():%Y-%m-%d}", ""]
        for i, op in enumerate(ops, 1):
            lines.append(f"{i}. {op['kind']} {op.get('text', '')} {op.get('reason', '')}")
        (self._card_dir() / "月度报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _apply_monthly(self, selected: list[int] | None) -> str:
        pending = self._load("monthly_pending.json", {"items": []})
        ops = pending.get("items") or []
        if not ops:
            return "没有待执行的月度项。"
        chosen = set(selected) if selected else set(range(1, len(ops) + 1))
        store = self._load("store.json", {"facts": [], "judgments": [], "taboos": [], "insights": []})
        by_id = {row["id"]: row for row in store.get("judgments", [])}
        ins_by_id = {row["id"]: row for row in store.get("insights", [])}
        today = datetime.now().date().isoformat()
        done = []
        rest = []
        for i, op in enumerate(ops, 1):
            if i not in chosen:
                rest.append(op)
                continue
            if op["kind"] == "merge":
                keep, drop = by_id.get(op["keep"]), by_id.get(op["drop"])
                if not keep or not drop:
                    continue
                keep["evidence"] = list(keep.get("evidence") or []) + list(drop.get("evidence") or [])
                keep["confidence"] = min(0.95, float(keep.get("confidence", 0.7)) + 0.03)
                keep["updated"] = today
                drop["status"] = "archived"
                drop["updated"] = today
                drop["archive_reason"] = f"合并到 {keep['id']}"
                done.append(f"合并 {drop['text'][:16]}")
            elif op["kind"] == "archive":
                row = by_id.get(op["id"])
                if row:
                    row["status"] = "archived"
                    row["updated"] = today
                    row["archive_reason"] = "月度体检"
                    done.append(f"归档 {row.get('text', '')[:16]}")
            elif op["kind"] == "promote":
                ins = ins_by_id.get(op["id"])
                if not ins:
                    continue
                store.setdefault("judgments", []).append({
                    "id": f"j{datetime.now():%y%m%d%H%M%S}{i}",
                    "type": "judgment",
                    "text": ins.get("text", ""),
                    "confidence": min(0.9, float(ins.get("confidence", 0.55)) + 0.1),
                    "status": "active",
                    "source": "monthly",
                    "created": today,
                    "updated": today,
                    "last_reviewed": today,
                    "supersedes": None,
                    "evidence": list(ins.get("evidence") or []),
                })
                ins["status"] = "promoted"
                done.append(f"升级 {ins.get('text', '')[:16]}")
        self._save("store.json", store)
        self._save("monthly_pending.json", {"items": rest})
        self._render_card_files()
        if not done:
            return "没有执行任何项。"
        left = f" 还剩 {len(rest)} 项。" if rest else ""
        return f"好，处理了 {len(done)} 项：{'；'.join(done[:3])}。{left}"

    async def _monthly_loop(self):
        while True:
            try:
                now = datetime.now()
                year, month = now.year, now.month
                target = datetime(year + (month == 12), (month % 12) + 1, 1, 21, 0)
                await asyncio.sleep((target - now).total_seconds())
                ops = self._build_monthly()
                self._write_monthly(ops)
                if ops:
                    umo = str(self._cfg("umo", "")).strip() or str(self._load("last_umo.json", {}).get("umo", ""))
                    if umo and MessageChain is not None:
                        try:
                            await self.context.send_message(
                                umo, MessageChain([Plain("判断卡月度报告已生成，发 /月审 看清单。")])
                            )
                        except Exception:
                            pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"[card_review] 月度任务失败: {exc}")
                await asyncio.sleep(3600)

    @filter.command("月审")
    async def monthly_show(self, event: AstrMessageEvent):
        self._remember_umo(event)
        pending = self._load("monthly_pending.json", {"items": []})
        ops = pending.get("items") or []
        if not ops:
            ops = self._build_monthly()
            self._write_monthly(ops)
        yield event.plain_result(self._format_monthly(ops))

    @filter.command("月通过")
    async def monthly_approve(self, event: AstrMessageEvent):
        raw = (getattr(event, "message_str", "") or "").strip()
        nums = [int(x) for x in re.findall(r"\d+", raw)]
        yield event.plain_result(self._apply_monthly(nums or None))

    @filter.command("月驳回")
    async def monthly_reject(self, event: AstrMessageEvent):
        raw = (getattr(event, "message_str", "") or "").strip()
        nums = [int(x) for x in re.findall(r"\d+", raw)]
        pending = self._load("monthly_pending.json", {"items": []})
        ops = pending.get("items") or []
        if not ops:
            yield event.plain_result("没有待执行的月度项。")
            return
        if not nums:
            self._save("monthly_pending.json", {"items": []})
            yield event.plain_result("月度项全丢了。")
            return
        drop = set(nums)
        rest = [op for i, op in enumerate(ops, 1) if i not in drop]
        self._save("monthly_pending.json", {"items": rest})
        yield event.plain_result(f"跳过了第 {'、'.join(map(str, sorted(drop)))} 项，还剩 {len(rest)} 项。")

    @filter.command("审批")

    async def review_now(self, event: AstrMessageEvent):
        self._remember_umo(event)
        pending = self._load("pending.json", {"items": []})
        if pending.get("items"):
            yield event.plain_result(self._list_pending())
            return
        umo = getattr(event, "unified_msg_origin", "")
        message = await self._extract(umo, force=True)
        yield event.plain_result(message)

    @filter.command("待审")
    async def show_pending(self, event: AstrMessageEvent):
        self._remember_umo(event)
        yield event.plain_result(self._list_pending())

    @filter.command("通过")
    async def approve(self, event: AstrMessageEvent):
        raw = (getattr(event, "message_str", "") or "").strip()
        nums = [int(x) for x in re.findall(r"\d+", raw)]
        yield event.plain_result(self._apply(nums or None))

    @filter.command("驳回")
    async def reject(self, event: AstrMessageEvent):
        raw = (getattr(event, "message_str", "") or "").strip()
        nums = [int(x) for x in re.findall(r"\d+", raw)]
        pending = self._load("pending.json", {"items": []})
        items = pending.get("items") or []
        if not items:
            yield event.plain_result("没有待审的内容。")
            return
        if not nums:
            self._save("pending.json", {"items": []})
            yield event.plain_result("全丢了，判断卡没动。")
            return
        drop = set(nums)
        rest = [item for i, item in enumerate(items, 1) if i not in drop]
        self._save("pending.json", {"items": rest})
        yield event.plain_result(f"丢掉了第 {'、'.join(map(str, sorted(drop)))} 条，还剩 {len(rest)} 条。")

    # ---------- 定时 ----------

    async def _loop(self):
        while True:
            try:
                now = datetime.now()
                hour, minute = 20, 0
                raw_time = str(self._cfg("time", "20:00")).strip()
                if ":" in raw_time:
                    hour, minute = [int(x) for x in raw_time.split(":")[:2]]
                days = (6 - now.weekday()) % 7
                target = (now + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=7)
                await asyncio.sleep(max(60, (target - now).total_seconds()))
                umo = str(self._cfg("umo", "")).strip()
                if not umo:
                    umo = str(self._load("last_umo.json", {}).get("umo", ""))
                if not umo:
                    continue
                await self._extract(umo, force=False)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"[card_review] 定时整理失败: {exc}")
                await asyncio.sleep(3600)
