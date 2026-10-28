"""示例池（AstrBot v4）——只管“她说话的样子”。

- /good：把上一句收进池子（同一句只记次数）
- /bad：标记不喜欢的回复
- 每轮从池子里按权重抽 4 条注入
"""

from __future__ import annotations

import json
import re
import math
import random
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


HEADER = "<VoicePool>"
FOOTER = "</VoicePool>"

SCENES = {
    "哄睡": ("睡不着", "失眠", "困", "晚安", "睡吧", "闭眼"),
    "被夸": ("可爱", "好看", "漂亮", "厉害", "聪明", "最好了", "喜欢你"),
    "累": ("累", "疲", "忙", "加班", "撑不住"),
    "想你": ("想你", "想我", "在干嘛", "干嘛呢", "在吗"),
    "生气": ("烦", "滚", "别管", "讨厌", "生气"),
    "吃饭": ("吃", "饿", "饭", "外卖", "胃"),
    "技术": ("报错", "代码", "为什么", "怎么", "原理", "bug"),
    "照片": ("照片", "发张", "看看你"),
    "亲密": ("抱", "亲", "贴", "牵", "要你"),
}


@register("astrbot_plugin_voice_pool", "local", "示例池：/good 收藏、/bad 标记，按权重抽样注入", "1.0.0")
class VoicePool(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._last: dict[str, dict] = {}
        self._recent: list[str] = []

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

    def _read(self, name: str) -> list[dict]:
        path = self._dir() / name
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

    def _write(self, name: str, rows: list[dict]) -> None:
        path = self._dir() / name
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    def _in_scope(self, event: AstrMessageEvent) -> bool:
        if str(self._cfg("scope", "private")).lower() != "private":
            return True
        try:
            return event.get_message_type() != MessageType.GROUP_MESSAGE
        except Exception:
            return True

    @staticmethod
    def _scenes(text: str) -> list[str]:
        return [name for name, keys in SCENES.items() if any(k in (text or "") for k in keys)]

    # ---------- 抽样 ----------

    def _pick(self, user_text: str, count: int) -> list[dict]:
        pool = [r for r in self._read("examples.jsonl") if r.get("status", "active") == "active"]
        if not pool:
            return []
        scenes = self._scenes(user_text)
        weighted = []
        for row in pool:
            text = row.get("text", "")
            weight = 0.3 + float(row.get("score", 0.4)) * 2
            weight += 0.3 * math.log1p(int(row.get("likes", 0)))
            weight += 0.2 * math.log1p(int(row.get("picked", 0)))
            if scenes and row.get("scene") in scenes:
                weight *= 2
            if text in self._recent:
                weight *= 0.3
            weighted.append((max(0.01, weight), row))

        chosen = []
        used_scenes = set()
        guard = 0
        while weighted and len(chosen) < count and guard < count * 12:
            guard += 1
            total = sum(w for w, _ in weighted)
            roll = random.uniform(0, total)
            acc = 0.0
            index = 0
            for i, (w, _) in enumerate(weighted):
                acc += w
                if acc >= roll:
                    index = i
                    break
            weight, row = weighted.pop(index)
            scene = row.get("scene", "")
            if scene in used_scenes and len(weighted) >= count - len(chosen) and guard < count * 4:
                weighted.append((weight * 0.2, row))
                continue
            chosen.append(row)
            used_scenes.add(scene)
        return chosen

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest):
        if not bool(self._cfg("enabled", True)) or not self._in_scope(event):
            return
        count = int(self._cfg("examples_per_turn", 4) or 4)
        text = (getattr(event, "message_str", "") or "").strip()
        picked = self._pick(text, count)
        if not picked:
            return

        lines = [HEADER, "下面是她的语感示例：只学语气和节奏，不照抄内容。", ""]
        for row in picked:
            lines.append("<START>")
            lines.append(f"你：{row.get('user', '').strip()}")
            lines.append(f"她：{row.get('text', '').strip()}")
            lines.append("")
        lines.append(FOOTER)

        self._strip(req)
        req.extra_user_content_parts.append(TextPart(text="\n".join(lines)).mark_as_temp())

        rows = self._read("examples.jsonl")
        for row in rows:
            if row.get("text") in {p.get("text") for p in picked}:
                row["picked"] = int(row.get("picked", 0)) + 1
                row["score"] = round(0.4 + min(int(row.get("likes", 0)), 5) * 0.06 + min(int(row.get("picked", 0)), 20) * 0.01, 3)
        self._write("examples.jsonl", rows)
        for p in picked:
            self._recent.append(p.get("text", ""))
        self._recent = self._recent[-12:]

    @staticmethod
    def _strip(req: ProviderRequest) -> None:
        parts = getattr(req, "extra_user_content_parts", None)
        if not parts:
            return
        kept = [p for p in parts if HEADER not in (getattr(p, "text", "") or "")]
        if len(kept) != len(parts):
            req.extra_user_content_parts = kept

    # ---------- 记录 ----------

    @filter.on_llm_response()
    async def remember(self, event: AstrMessageEvent, resp):
        if not self._in_scope(event):
            return
        raw = (getattr(resp, "completion_text", "") or "").strip()
        if not raw:
            return
        text = self._clean_reply(raw)
        if not text:
            return
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        self._last[getattr(event, "unified_msg_origin", "")] = {
            "text": text[:1200],
            "line": self._pick_line(lines),
            "segments": len(lines),
            "user": (getattr(event, "message_str", "") or "").strip()[:200],
        }

    @filter.command("good", alias={"好", "存"})
    async def good(self, event: AstrMessageEvent):
        yield event.plain_result(self._save(event, True))

    @filter.command("bad", alias={"差"})
    async def bad(self, event: AstrMessageEvent):
        yield event.plain_result(self._save(event, False))

    @staticmethod
    def _clean_reply(text: str) -> str:
        text = str(text or "").replace("&lt;", "<").replace("&gt;", ">")
        text = re.sub(r"<pc_[^>]*>.*?</pc_[^>]*>", "", text, flags=re.S)
        text = re.sub(r"</?pc_[^>]*>", "", text)
        return text.strip()

    @staticmethod
    def _pick_line(lines: list[str]) -> str:
        if not lines:
            return ""
        spoken = [x for x in lines if not x.startswith(("（", "【"))]
        pick = spoken[-1] if spoken else lines[-1]
        pick = pick.strip().strip("「」『』")
        return pick[:120]

    def _save(self, event: AstrMessageEvent, positive: bool) -> str:
        last = self._last.get(getattr(event, "unified_msg_origin", ""))
        if not last:
            return "还没记住上一句，等我先回一条。"
        rows = self._read("examples.jsonl")
        if not positive:
            bad = self._read("bad.jsonl")
            bad.append({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "text": last["text"],
                "segments": last.get("segments", 1),
            })
            self._write("bad.jsonl", bad)
            return f"记下了，这条回复（{last.get('segments', 1)} 段）整条不进示例池（不喜欢的第 {len(bad)} 条）。"

        line = last.get("line") or last["text"]
        for row in rows:
            if row.get("text") == line:
                row["likes"] = int(row.get("likes", 0)) + 1
                row["status"] = "active"
                row["score"] = round(0.4 + min(row["likes"], 5) * 0.06 + min(int(row.get("picked", 0)), 20) * 0.01, 3)
                self._write("examples.jsonl", rows)
                return f"又记了一次（这条被你喜欢 {row['likes']} 次）。"

        mode = self._mode()
        rows.append({
            "id": f"e{datetime.now():%y%m%d%H%M%S}",
            "text": line,
            "scene": (f"模式:{mode}" if mode else (self._scenes(last.get("user", "")) or ["日常"])[0]),
            "likes": 1,
            "picked": 0,
            "status": "archived" if mode else "active",
            "score": 0.46,
            "created": datetime.now().isoformat(timespec="seconds"),
        })
        self._write("examples.jsonl", rows)
        if mode:
            return f"收下了（第 {len(rows)} 条）。模式中的句子先不进日常池，已标成「模式:{mode}」。"
        return f"收下了：{line[:30]}（第 {len(rows)} 条）。"

    def _mode(self) -> str:
        try:
            state = json.loads((self._dir().parent / "modes" / "state.json").read_text(encoding="utf-8"))
            mode = state.get("mode")
            if not mode:
                return ""
            expires = datetime.fromisoformat(str(state.get("expires_at", "")))
            return str(mode) if datetime.now() < expires else ""
        except Exception:
            return ""

    @filter.command("pool")
    async def pool(self, event: AstrMessageEvent):
        rows = self._read("examples.jsonl")
        active = [r for r in rows if r.get("status", "active") == "active"]
        bad = self._read("bad.jsonl")
        scenes: dict[str, int] = {}
        for row in active:
            scenes[row.get("scene", "日常")] = scenes.get(row.get("scene", "日常"), 0) + 1
        lines = [f"示例池：{len(rows)} 条，上场 {len(active)} 条，不喜欢 {len(bad)} 条。"]
        if scenes:
            lines.append("场景：" + "、".join(f"{k} {v}" for k, v in sorted(scenes.items(), key=lambda x: -x[1])))
        lines.append("用法：/good 收藏上一句，/bad 标记，/pool 看状态。")
        yield event.plain_result("\n".join(lines))
