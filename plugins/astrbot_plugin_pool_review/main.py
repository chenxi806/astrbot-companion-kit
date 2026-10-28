"""示例池审查（AstrBot v4）。

- 每周日 20:30：生成小审查（重复、超配、长期没抽中）
- 每月 1 号 21:00：生成大审查（含低使用率归档）
- 用户回「池通过 1,3」执行
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
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.message_components import MessageChain, Plain
except Exception:  # pragma: no cover
    MessageChain = None  # type: ignore
    Plain = None  # type: ignore


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", str(text or ""))


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


@register("astrbot_plugin_pool_review", "local", "示例池审查：每周小审查、每月大审查，用户只回通过", "1.0.0")
class PoolReview(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._week_task = asyncio.create_task(self._weekly_loop())
        self._month_task = asyncio.create_task(self._monthly_loop())

    def _cfg(self, key: str, default=None):
        value = self.config.get(key, default) if hasattr(self.config, "get") else None
        return default if value is None else value

    def _dir(self) -> Path:
        raw = str(self._cfg("memory_dir", "")).strip()
        if not raw:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            raw = str(get_astrbot_data_path() / "companion_memory")
        path = Path(raw) / "voice_pool"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _read(self) -> list[dict]:
        path = self._dir() / "examples.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        return rows

    def _write(self, rows: list[dict]) -> None:
        (self._dir() / "examples.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
        )

    def _save(self, name: str, data) -> None:
        path = self._dir() / name
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _load(self, name: str, default):
        try:
            return json.loads((self._dir() / name).read_text(encoding="utf-8"))
        except Exception:
            return default

    # ---------- 生成方案 ----------

    def _build(self, monthly: bool = False) -> list[dict]:
        rows = self._read()
        active = [r for r in rows if r.get("status", "active") == "active"]
        ops: list[dict] = []

        # 重复
        removed = set()
        for i in range(len(active)):
            for j in range(i + 1, len(active)):
                a, b = active[i], active[j]
                if a.get("text", "") in removed or b.get("text", "") in removed:
                    continue
                if _sim(a.get("text", ""), b.get("text", "")) >= 0.92:
                    keep, drop = (a, b) if int(a.get("likes", 0)) >= int(b.get("likes", 0)) else (b, a)
                    ops.append({"kind": "archive", "id": drop.get("id"), "text": drop.get("text", "")[:30], "reason": "重复"})
                    removed.add(drop.get("text", ""))

        # 场景超 8
        scenes: dict[str, list[dict]] = {}
        for row in active:
            if row.get("text", "") in removed:
                continue
            scenes.setdefault(row.get("scene", "日常"), []).append(row)
        for scene, items in scenes.items():
            if len(items) <= 8:
                continue
            items.sort(key=lambda r: (float(r.get("score", 0.4)), int(r.get("picked", 0))))
            for row in items[: len(items) - 8]:
                ops.append({"kind": "archive", "id": row.get("id"), "text": row.get("text", "")[:30], "reason": f"{scene} 超配"})
                removed.add(row.get("text", ""))

        # 大审查：长期没抽中且分低
        if monthly:
            for row in active:
                if row.get("text", "") in removed:
                    continue
                if int(row.get("picked", 0)) == 0 and float(row.get("score", 0.4)) < 0.45:
                    ops.append({"kind": "archive", "id": row.get("id"), "text": row.get("text", "")[:30], "reason": "从没抽中且分低"})
                    removed.add(row.get("text", ""))

        return ops

    def _format(self, ops: list[dict], monthly: bool = False) -> str:
        if not ops:
            return "示例池很干净，这周没有要处理的。"
        title = "示例池月度大审查" if monthly else "示例池周审查"
        lines = [f"{title}（{len(ops)} 项）："]
        for i, op in enumerate(ops, 1):
            lines.append(f"{i}. [归档] {op['text']}（{op['reason']}）")
        lines.append("")
        cmd = "池月通过" if monthly else "池通过"
        lines.append(f"回「{cmd}」全部执行；「{cmd} 1,3」只做这几项；「池驳回 2」跳过。")
        return "\n".join(lines)

    def _apply(self, ops: list[dict], selected: list[int] | None) -> tuple[str, list[dict]]:
        chosen = set(selected) if selected else set(range(1, len(ops) + 1))
        rows = self._read()
        by_id = {r.get("id"): r for r in rows}
        done = []
        rest = []
        for i, op in enumerate(ops, 1):
            if i not in chosen:
                rest.append(op)
                continue
            row = by_id.get(op.get("id"))
            if not row:
                continue
            row["status"] = "archived"
            row["archived_reason"] = op.get("reason", "")
            done.append(op.get("text", ""))
        self._write(rows)
        if not done:
            return "没有执行任何项。", rest
        return f"好，归档了 {len(done)} 条：{'；'.join(done[:3])}。", rest

    # ---------- 命令 ----------

    @filter.command("池审")
    async def weekly_show(self, event: AstrMessageEvent):
        ops = self._build(False)
        self._save("weekly_pending.json", {"items": ops})
        yield event.plain_result(self._format(ops, False))

    @filter.command("池通过")
    async def weekly_apply(self, event: AstrMessageEvent):
        raw = (getattr(event, "message_str", "") or "").strip()
        nums = [int(x) for x in re.findall(r"\d+", raw)]
        pending = self._load("weekly_pending.json", {"items": []})
        ops = pending.get("items") or []
        if not ops:
            yield event.plain_result("没有待执行的周审查项。")
            return
        message, rest = self._apply(ops, nums or None)
        self._save("weekly_pending.json", {"items": rest})
        yield event.plain_result(message)

    @filter.command("池月审")
    async def monthly_show(self, event: AstrMessageEvent):
        ops = self._build(True)
        self._save("monthly_pending.json", {"items": ops})
        yield event.plain_result(self._format(ops, True))

    @filter.command("池月通过")
    async def monthly_apply(self, event: AstrMessageEvent):
        raw = (getattr(event, "message_str", "") or "").strip()
        nums = [int(x) for x in re.findall(r"\d+", raw)]
        pending = self._load("monthly_pending.json", {"items": []})
        ops = pending.get("items") or []
        if not ops:
            yield event.plain_result("没有待执行的月度项。")
            return
        message, rest = self._apply(ops, nums or None)
        self._save("monthly_pending.json", {"items": rest})
        yield event.plain_result(message)

    @filter.command("池驳回")
    async def reject(self, event: AstrMessageEvent):
        raw = (getattr(event, "message_str", "") or "").strip()
        nums = [int(x) for x in re.findall(r"\d+", raw)]
        for name in ("weekly_pending.json", "monthly_pending.json"):
            pending = self._load(name, {"items": []})
            ops = pending.get("items") or []
            if not ops:
                continue
            if not nums:
                self._save(name, {"items": []})
                continue
            drop = set(nums)
            rest = [op for i, op in enumerate(ops, 1) if i not in drop]
            self._save(name, {"items": rest})
        yield event.plain_result("好，跳过了。")

    # ---------- 定时 ----------

    async def _weekly_loop(self):
        while True:
            try:
                now = datetime.now()
                days = (6 - now.weekday()) % 7
                target = (now + timedelta(days=days)).replace(hour=20, minute=30, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=7)
                await asyncio.sleep(max(60, (target - now).total_seconds()))
                ops = self._build(False)
                self._save("weekly_pending.json", {"items": ops})
                await self._notify("示例池周审查已生成，发 /池审 看清单。")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"[pool_review] 周任务失败: {exc}")
                await asyncio.sleep(3600)

    async def _monthly_loop(self):
        while True:
            try:
                now = datetime.now()
                year, month = now.year, now.month
                target = datetime(year + (month == 12), (month % 12) + 1, 1, 21, 0)
                await asyncio.sleep((target - now).total_seconds())
                ops = self._build(True)
                self._save("monthly_pending.json", {"items": ops})
                await self._notify("示例池月度大审查已生成，发 /池月审 看清单。")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"[pool_review] 月任务失败: {exc}")
                await asyncio.sleep(3600)

    async def _notify(self, text: str) -> None:
        umo = str(self._cfg("umo", "")).strip()
        if not umo:
            try:
                umo = json.loads((self._dir().parent / "card" / "last_umo.json").read_text(encoding="utf-8")).get("umo", "")
            except Exception:
                umo = ""
        if not umo or MessageChain is None:
            return
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception:
            pass
