"""判断卡（AstrBot v4）——只管“她记得的关于你的事”。

四类：事实 / 判断 / 雷区 / 洞察。规范见 memory/card/规范.md。
- /记 内容：加一条判断（相似自动合并）
- /事实 内容：加一条事实
- /雷区 内容：加一条雷区
- /card：看卡；/card list 看编号
"""

from __future__ import annotations

import difflib
import json
import re
from datetime import datetime
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


HEADER = "<MemoryCard>"
FOOTER = "</MemoryCard>"
LIMITS = {"facts": 30, "judgments": 40, "taboos": 10, "insights": 50}
BUDGET = 1800


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", str(text or ""))


def _similar(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


@register("astrbot_plugin_memory_card", "local", "判断卡：事实/判断/雷区/洞察，注入 + /记", "1.1.0")
class MemoryCard(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}

    def _cfg(self, key: str, default=None):
        value = self.config.get(key, default) if hasattr(self.config, "get") else None
        return default if value is None else value

    def _dir(self) -> Path:
        raw = str(self._cfg("memory_dir", "")).strip()
        if not raw:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            raw = str(get_astrbot_data_path() / "companion_memory")
        path = Path(raw) / "card"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _store_path(self) -> Path:
        return self._dir() / "store.json"

    def _load(self) -> dict:
        path = self._store_path()
        data = {"facts": [], "judgments": [], "taboos": [], "insights": []}
        if path.exists():
            try:
                data.update(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                pass
        for key in data:
            data.setdefault(key, [])
        for key, rows in data.items():
            for row in rows:
                row.setdefault("type", "fact" if key == "facts" else "judgment" if key == "judgments" else "taboo" if key == "taboos" else "insight")
                row.setdefault("status", "active")
                row.setdefault("source", "user")
                row.setdefault("evidence", [])
                row.setdefault("created", datetime.now().date().isoformat())
                row.setdefault("updated", row["created"])
                if key in ("judgments", "insights"):
                    row.setdefault("confidence", 0.7)
        return data

    def _save(self, data: dict) -> None:
        path = self._store_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _render(self, data: dict) -> tuple[str, str]:
        facts = data.get("facts", [])
        active = [j for j in data.get("judgments", []) if j.get("status", "active") == "active"]
        active.sort(key=lambda j: -float(j.get("confidence", 0.6)))
        taboos = data.get("taboos", [])

        md = ["# 她的判断卡", ""]
        md.append("## 事实")
        md += [f"- {f.get('text', '')}" for f in facts] or ["- （空）"]
        md.append("")
        md.append("## 判断")
        for j in active:
            ev = "；".join(str(e.get("quote", "")) for e in (j.get("evidence") or [])[:2])
            md.append(f"- `{j.get('id')}` [{j.get('confidence')}] {j.get('text', '')}" + (f"（证据：{ev}）" if ev else ""))
        if not active:
            md.append("- （空）")
        md.append("")
        md.append("## 雷区")
        md += [f"- {t.get('text', '')}" for t in taboos] or ["- （空）"]
        md.append("")

        if not (facts or active or taboos):
            return "\n".join(md), ""

        picked = list(active)
        while True:
            lines = ["关于他，你记得的："]
            if facts:
                lines.append("【事实】" + "；".join(f.get("text", "") for f in facts))
            if picked:
                lines.append("【判断】" + "；".join(f"{j.get('text', '')}" for j in picked))
            if taboos:
                lines.append("【雷区】" + "；".join(t.get("text", "") for t in taboos))
            lines.append("（记得就自然用，不要复述这张卡，也不要提到“卡”“设定”。）")
            companion = "\n".join(lines)
            if len(companion) <= BUDGET or not picked:
                break
            picked.pop()
        return "\n".join(md), companion

    def _write_cards(self, data: dict) -> str:
        md, companion = self._render(data)
        (self._dir() / "card.md").write_text(md, encoding="utf-8")
        (self._dir() / "card.companion.txt").write_text(companion, encoding="utf-8")
        return companion

    def _in_scope(self, event: AstrMessageEvent) -> bool:
        if str(self._cfg("scope", "private")).lower() != "private":
            return True
        try:
            return event.get_message_type() != MessageType.GROUP_MESSAGE
        except Exception:
            return True

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest):
        if not bool(self._cfg("enabled", True)) or not self._in_scope(event):
            return
        _, companion = self._render(self._load())
        if not companion.strip():
            return
        self._strip(req)
        req.extra_user_content_parts.append(TextPart(text=f"{HEADER}\n{companion}\n{FOOTER}").mark_as_temp())

    @staticmethod
    def _strip(req: ProviderRequest) -> None:
        parts = getattr(req, "extra_user_content_parts", None)
        if not parts:
            return
        kept = [p for p in parts if HEADER not in (getattr(p, "text", "") or "")]
        if len(kept) != len(parts):
            req.extra_user_content_parts = kept

    @filter.on_llm_response()
    async def record_raw(self, event: AstrMessageEvent, resp):
        if not self._in_scope(event):
            return
        import asyncio

        user = (getattr(event, "message_str", "") or "").strip()
        assistant = (getattr(resp, "completion_text", "") or "").strip()
        if not user or not assistant:
            return
        raw_dir = self._dir().parent / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        path = raw_dir / f"{datetime.now():%Y-%m-%d}.jsonl"
        mode = ""
        try:
            state = json.loads((self._dir().parent / "modes" / "state.json").read_text(encoding="utf-8"))
            if state.get("mode"):
                expires = datetime.fromisoformat(str(state.get("expires_at", "")))
                if datetime.now() < expires:
                    mode = str(state["mode"])
        except Exception:
            mode = ""
        record = json.dumps(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "user": user[:2000],
                "assistant": assistant[:2000],
                "mode": mode,
            },
            ensure_ascii=False,
        )
        try:
            await asyncio.to_thread(lambda: path.open("a", encoding="utf-8").write(record + "\n"))
        except OSError as exc:
            logger.warning(f"[memory_card] 写 raw 失败: {exc}")

    # ---------- 命令 ----------

    @staticmethod
    def _body(event: AstrMessageEvent) -> str:
        raw = (getattr(event, "message_str", "") or "").strip()
        parts = raw.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    @filter.command("记", alias={"记住"})
    async def add_judgment(self, event: AstrMessageEvent):
        body = self._body(event)
        if not body:
            yield event.plain_result("用法：/记 以后别在半夜讲道理")
            return
        data = self._load()
        for row in data["judgments"]:
            if row.get("status", "active") != "active":
                continue
            if _similar(body, row.get("text", "")) >= 0.8:
                row["evidence"] = list(row.get("evidence") or []) + [{"date": f"{datetime.now():%m-%d}", "quote": body[:160]}]
                row["confidence"] = min(0.95, float(row.get("confidence", 0.7)) + 0.03)
                row["updated"] = datetime.now().date().isoformat()
                self._save(data)
                self._write_cards(data)
                yield event.plain_result(f"这条和「{row['text'][:20]}」很像，已经合并，置信度提到 {row['confidence']:.2f}。")
                return
        active = [j for j in data["judgments"] if j.get("status", "active") == "active"]
        if len(active) >= LIMITS["judgments"]:
            yield event.plain_result(f"判断已经 {len(active)} 条，到上限了；先归档旧的再加。")
            return
        now = datetime.now()
        data["judgments"].append({
            "id": f"j{now:%y%m%d%H%M%S}",
            "type": "judgment",
            "text": body[:120],
            "confidence": 0.85,
            "status": "active",
            "source": "user",
            "created": now.date().isoformat(),
            "updated": now.date().isoformat(),
            "last_reviewed": now.date().isoformat(),
            "supersedes": None,
            "evidence": [{"date": f"{now:%m-%d}", "quote": body[:160]}],
        })
        self._save(data)
        self._write_cards(data)
        yield event.plain_result(f"记下了：{body[:40]}")

    @filter.command("事实")
    async def add_fact(self, event: AstrMessageEvent):
        body = self._body(event)
        if not body:
            yield event.plain_result("用法：/事实 我周三有课")
            return
        data = self._load()
        if len(data["facts"]) >= LIMITS["facts"]:
            yield event.plain_result("事实已经 30 条，先清理旧的。")
            return
        data["facts"].append({"type": "fact", "text": body[:120], "created": datetime.now().date().isoformat()})
        self._save(data)
        self._write_cards(data)
        yield event.plain_result(f"事实已记：{body[:40]}")

    @filter.command("雷区")
    async def add_taboo(self, event: AstrMessageEvent):
        body = self._body(event)
        if not body:
            yield event.plain_result("用法：/雷区 别提前任")
            return
        data = self._load()
        if len(data["taboos"]) >= LIMITS["taboos"]:
            yield event.plain_result("雷区已经 10 条，先清理旧的。")
            return
        data["taboos"].append({"type": "taboo", "text": body[:120], "created": datetime.now().date().isoformat()})
        self._save(data)
        self._write_cards(data)
        yield event.plain_result(f"雷区已记：{body[:40]}")

    @filter.command("观察")
    async def add_insight(self, event: AstrMessageEvent):
        body = self._body(event)
        if not body:
            yield event.plain_result("用法：/观察 他最近好像睡得晚")
            return
        data = self._load()
        now = datetime.now()
        data["insights"].append({
            "id": f"i{now:%y%m%d%H%M%S}",
            "type": "insight",
            "text": body[:120],
            "confidence": 0.5,
            "status": "emerging",
            "source": "user",
            "created": now.date().isoformat(),
            "updated": now.date().isoformat(),
            "evidence": [{"date": f"{now:%m-%d}", "quote": body[:160]}],
        })
        self._save(data)
        yield event.plain_result(f"先记在观察区：{body[:40]}")

    @filter.command("忘")
    async def forget(self, event: AstrMessageEvent):
        body = self._body(event).strip("` ")
        if not body:
            yield event.plain_result("用法：/忘 j251028143000")
            return
        data = self._load()
        for key in ("judgments", "insights"):
            for row in data.get(key, []):
                if row.get("id") == body:
                    row["status"] = "archived"
                    row["updated"] = datetime.now().date().isoformat()
                    self._save(data)
                    self._write_cards(data)
                    yield event.plain_result(f"已归档：{row.get('text', '')[:30]}")
                    return
        yield event.plain_result(f"没找到 {body}，发 /card list 看编号。")

    @filter.command("card")
    async def card(self, event: AstrMessageEvent):
        raw = (getattr(event, "message_str", "") or "").strip()
        data = self._load()
        active = [j for j in data.get("judgments", []) if j.get("status", "active") == "active"]
        if "list" in raw.lower():
            lines = [f"判断 {len(active)} 条："]
            lines += [f"- `{j.get('id')}` [{j.get('confidence')}] {j.get('text', '')}" for j in active]
            yield event.plain_result("\n".join(lines) or "（空）")
            return
        lines = [
            f"事实 {len(data.get('facts', []))} | 判断 {len(active)} | 雷区 {len(data.get('taboos', []))} | 洞察 {len(data.get('insights', []))}",
            "用法：/记 内容、/事实 内容、/雷区 内容、/card list",
        ]
        yield event.plain_result("\n".join(lines))
