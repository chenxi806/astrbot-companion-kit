# -*- coding: utf-8 -*-
"""她的相册：按场景/情绪挑照片发给哥哥。

图片在 D:\\deepseek图标\\her_album，meta.json 里每张带 kind / scenes / moods / tags。
- 85% 插画，15% 表情包
- 按 scene 关键词匹配 scenes/moods；找不到就随机
- 带「私房」标签的只在亲密类关键词时才进候选
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

ALBUM_DIR = Path(r"D:\deepseek图标\her_album")
META_FILE = ALBUM_DIR / "meta.json"

ALIASES = {
    "心情好": "开心",
    "高兴": "开心",
    "笑": "开心",
    "难过": "温柔",
    "不开心": "温柔",
    "累": "疲惫",
    "困": "疲惫",
    "睡不着": "疲惫",
    "害羞": "害羞",
    "撒娇": "撒娇",
    "想": "私房",
    "撩": "挑逗",
    "下雨": "雨夜",
    "雨": "雨夜",
    "海": "海边",
    "夜": "卧室",
    "吃": "厨房",
    "咖啡": "咖啡店",
    "奶茶": "咖啡店",
}
INTIMATE_HINTS = ("私房", "亲密", "想", "抱", "亲", "夜", "泳装", "内衣", "睡", "撩")


@register(
    "her_album",
    "local",
    "大肥鱼的自拍相册：按场景/情绪挑插画或表情包",
    "1.1.0",
)
class HerAlbum(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._recent: list[str] = []

    def _load(self) -> list[dict]:
        try:
            data = json.loads(META_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
        items = data.get("images") if isinstance(data, dict) else None
        return [item for item in (items or []) if isinstance(item, dict)]

    @staticmethod
    def _score(item: dict, text: str) -> int:
        fields = []
        fields.extend(str(x) for x in (item.get("scenes") or []))
        fields.extend(str(x) for x in (item.get("moods") or []))
        fields.extend(str(x) for x in (item.get("tags") or []))
        return sum(1 for f in fields if f and (f in text or text in f))

    def _pick(self, scene: str = "") -> dict | None:
        items = [item for item in self._load() if (ALBUM_DIR / str(item.get("file", ""))).exists()]
        if not items:
            return None

        illustrations = [i for i in items if i.get("kind") == "illustration"]
        memes = [i for i in items if i.get("kind") != "illustration"]
        if illustrations and memes:
            base_pool = illustrations if random.random() < 0.85 else memes
        else:
            base_pool = illustrations or items

        text = (scene or "").strip()
        intimate = any(h in text for h in INTIMATE_HINTS)
        if not intimate:
            filtered = [i for i in base_pool if "私房" not in (i.get("scenes") or [])]
            base_pool = filtered or base_pool

        if text:
            expanded = text + " " + " ".join(v for k, v in ALIASES.items() if k in text)
            scored = [(self._score(i, expanded), i) for i in base_pool]
            best = max((s for s, _ in scored), default=0)
            if best > 0:
                base_pool = [i for s, i in scored if s == best]

        pool = [i for i in base_pool if i.get("file") not in self._recent] or base_pool
        pick = random.choice(pool)
        self._recent = (self._recent + [str(pick.get("file"))])[-15:]
        return pick

    async def _send(self, event: AstrMessageEvent, scene: str = "") -> bool:
        pick = self._pick(scene)
        if not pick:
            return False
        path = ALBUM_DIR / str(pick["file"])
        await event.send(event.image_result(str(path)))
        logger.info(
            "[她的相册] 发送 %s 场景=%s 情绪=%s",
            pick.get("file"),
            pick.get("scenes") or pick.get("scene"),
            pick.get("moods") or "",
        )
        return True

    @filter.llm_tool(name="send_her_photo")
    async def send_her_photo(self, event: AstrMessageEvent, scene: str = ""):
        """把自己的照片/自拍发给哥哥。当他说「发张照片」「看看你」「你现在什么样」，问你在干嘛、穿什么，或聊到适合配图的场景（下雨、海边、吃饭、睡不着、节日），以及你心情好、想他、撒娇、安慰他时，都可以主动发一张，不要只用文字描述。

        Args:
            scene(string): 场景或情绪关键词，例如 雨夜、海边、咖啡店、深夜、想他、撒娇、心情好、女仆装；留空就随机。
        """
        ok = await self._send(event, scene)
        if not ok:
            return "相册里暂时没有照片，先用文字回答。"
        return "照片已经发出；接着用一句自然的话接住，不要报幕，也不要说这是生成的。"

    @filter.command("看照片")
    async def look_photo(self, event: AstrMessageEvent):
        text = (getattr(event, "message_str", "") or "").replace("/看照片", "", 1).strip()
        if not await self._send(event, text):
            yield event.plain_result("相册里还没有照片。")
