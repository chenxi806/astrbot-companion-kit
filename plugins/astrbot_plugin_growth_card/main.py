"""判断卡插件（AstrBot v4）。

把 memory/card.companion.txt 注入到每一轮 LLM 请求里，让角色始终带着
"关于哥哥的事实 / 判断 / 雷区" 说话；并提供 /card 命令做查看与维护。

设计原则：
- 只追加、不覆盖：注入走 extra_user_content_parts，和 livingmemory 同一条通道。
- 卡文件是外置的普通文本，人可以直接改；改完不用重载插件（按 mtime 自动刷新）。
- 插件不碰 livingmemory 的数据库，两者互不干扰。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

try:  # AstrBot >= 4.24
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover - 老版本兜底

    class TextPart:  # type: ignore[no-redef]
        def __init__(self, text: str):
            self.text = text

        def mark_as_temp(self):
            return self




def _default_memory_dir() -> Path:
    """默认记忆目录：<AstrBot data>/companion_memory。

    想换位置就在插件配置里填 memory_dir（所有插件填同一个）。
    """
    try:
        base = Path(StarTools.get_data_dir("astrbot_plugin_growth_card"))
        return base.parent.parent / "companion_memory"
    except Exception:
        return Path.home() / "companion_memory"

CARD_HEADER = "<GrowthCard>"
CARD_FOOTER = "</GrowthCard>"
DEFAULT_MEMORY_DIR = ""
# 别的插件插进回复里的控制标签，记录前先剥掉，免得污染复盘素材
CONTROL_TAG_RE = re.compile(r"<pc_[a-z_]+>.*?</pc_[a-z_]+>", re.S)


@register(
    "astrbot_plugin_growth_card",
    "local",
    "把外置记忆卡（事实/判断/雷区）注入每轮对话，并提供 /card 维护命令",
    "1.0.0",
)
class GrowthCardPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._cache_key: tuple[int, int] | None = None
        self._cache_text: str = ""
        self._lock = asyncio.Lock()
        self._logged_once = False
        self._raw_logged = False

    # ---------- 路径 ----------

    def _cfg(self, key: str, default):
        try:
            value = self.config.get(key, default)
        except AttributeError:
            value = default
        return default if value is None else value

    def _card_path(self) -> Path:
        raw = str(self._cfg("card_path", "")).strip()
        if not raw:
            raw = str(Path(self._memory_dir()) / "card.companion.txt")
        return Path(os.path.expandvars(os.path.expanduser(raw)))

    def _memory_dir(self) -> Path:
        raw = str(self._cfg("memory_dir", "")).strip()
        return Path(os.path.expandvars(os.path.expanduser(raw))) if raw else _default_memory_dir()

    def _raw_dir(self) -> Path:
        raw = str(self._cfg("raw_dir", "")).strip()
        if raw:
            return Path(os.path.expandvars(os.path.expanduser(raw)))
        return self._memory_dir() / "raw"

    def _in_scope(self, event: AstrMessageEvent) -> bool:
        """默认只对私聊注入/记录，避免把哥哥的个人信息带进群聊。"""
        scope = str(self._cfg("scope", "private")).strip().lower()
        if scope == "private":
            try:
                if event.get_message_type() == MessageType.GROUP_MESSAGE:
                    return False
            except Exception:
                pass
        allowed = self._cfg("allowed_umos", [])
        if isinstance(allowed, str):
            allowed = [line.strip() for line in allowed.splitlines() if line.strip()]
        if allowed:
            return getattr(event, "unified_msg_origin", "") in allowed
        return True

    # ---------- 读取 ----------

    async def _read_card(self) -> str:
        path = self._card_path()
        try:
            stat = path.stat()
        except OSError:
            return ""
        key = (stat.st_mtime_ns, stat.st_size)
        if key == self._cache_key:
            return self._cache_text

        async with self._lock:
            if key == self._cache_key:
                return self._cache_text
            try:
                text = path.read_text(encoding="utf-8-sig").strip()
            except OSError as exc:
                logger.warning(f"[growth_card] 读取判断卡失败: {exc}")
                return ""
            self._cache_key, self._cache_text = key, text
            logger.debug(f"[growth_card] 判断卡已刷新（{len(text)} 字）")
            return text

    # ---------- 注入 ----------

    @filter.on_llm_request()
    async def inject_card(self, event: AstrMessageEvent, req: ProviderRequest):
        if not bool(self._cfg("enabled", True)):
            return
        if not self._in_scope(event):
            return
        text = await self._read_card()
        if not text:
            return

        # 同一请求被重试时先清掉上一份，避免叠加
        self._strip_previous(req)
        req.extra_user_content_parts.append(
            TextPart(text=f"{CARD_HEADER}\n{text}\n{CARD_FOOTER}").mark_as_temp()
        )
        if not getattr(self, "_logged_once", False):
            self._logged_once = True
            logger.info(f"[growth_card] 判断卡已注入（{len(text)} 字）")

    @staticmethod
    def _strip_previous(req: ProviderRequest) -> None:
        parts = getattr(req, "extra_user_content_parts", None)
        if not parts:
            return
        kept = [
            part
            for part in parts
            if CARD_HEADER not in (getattr(part, "text", "") or "")
        ]
        if len(kept) != len(parts):
            req.extra_user_content_parts = kept

    @filter.on_llm_response()
    async def record_turn(self, event: AstrMessageEvent, resp):
        """把本轮问答落到 raw/YYYY-MM-DD.jsonl，供每周复盘使用。"""
        if not bool(self._cfg("record_raw", True)):
            return
        if not self._in_scope(event):
            return
        user = (getattr(event, "message_str", "") or "").strip()
        assistant = (getattr(resp, "completion_text", "") or "").strip()
        assistant = CONTROL_TAG_RE.sub("", assistant).strip()
        if not user or not assistant:
            return
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "umo": getattr(event, "unified_msg_origin", ""),
            "user": user[:2000],
            "assistant": assistant[:2000],
        }
        try:
            await asyncio.to_thread(self._append_raw, record)
            if not self._raw_logged:
                self._raw_logged = True
                logger.info(f"[growth_card] 开始记录原始问答 -> {self._raw_dir()}")
        except OSError as exc:
            logger.warning(f"[growth_card] 写原始记录失败: {exc}")

    def _append_raw(self, record: dict) -> None:
        folder = self._raw_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{datetime.now():%Y-%m-%d}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ---------- 命令 ----------

    @filter.command("card")
    async def card_command(self, event: AstrMessageEvent):
        """查看或维护判断卡。用法：/card、/card reload、/card log <内容>、/card review"""
        action, arg = self._parse_args(event)
        admin_only = bool(self._cfg("admin_only", True))

        if action in ("show", ""):
            text = await self._read_card()
            if not text:
                yield event.plain_result(
                    f"判断卡还没生成。先看 {self._card_path()} 是否存在，"
                    "或运行 python memory/render.py。"
                )
                return
            yield event.plain_result(text)
            return

        if admin_only and not self._can_manage(event):
            yield event.plain_result("只有管理员能改判断卡。")
            return

        if action == "reload":
            self._cache_key = None
            self._cache_text = ""
            text = await self._read_card()
            yield event.plain_result(
                f"已重新读取，{len(text)} 字。" if text else "文件不存在或为空。"
            )
            return

        if action == "log":
            if not arg.strip():
                yield event.plain_result("用法：/card log 今天他说……")
                return
            try:
                path = self._append_event(arg.strip())
            except OSError as exc:
                yield event.plain_result(f"写事件流失败：{exc}")
                return
            yield event.plain_result(f"记下了 → {path.name}")
            return

        if action == "review":
            yield event.plain_result(self._list_proposals())
            return

        yield event.plain_result(
            "用法：/card（查看）、/card reload（重读）、"
            "/card log 内容（记一条）、/card review（看待审提案）"
        )

    # ---------- 辅助 ----------

    @staticmethod
    def _parse_args(event: AstrMessageEvent) -> tuple[str, str]:
        raw = (getattr(event, "message_str", "") or "").strip()
        tokens = raw.split(maxsplit=2)
        if tokens and tokens[0].lstrip("/").lower() == "card":
            tokens = tokens[1:]
        action = tokens[0].lower() if tokens else "show"
        arg = tokens[1] if len(tokens) > 1 else ""
        return action, arg

    def _can_manage(self, event: AstrMessageEvent) -> bool:
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

    def _append_event(self, text: str) -> Path:
        events = self._memory_dir() / "events"
        events.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        path = events / f"{now:%Y-%m}.md"
        is_new = not path.exists()
        with path.open("a", encoding="utf-8") as handle:
            if is_new:
                handle.write(f"# {now:%Y-%m} 事件流\n\n")
            handle.write(f"- [{now:%Y-%m-%d %H:%M}] {text}\n")
        return path

    def _list_proposals(self) -> str:
        folder = self._memory_dir() / "proposals"
        if not folder.is_dir():
            return "还没有待审提案。"
        files = sorted(folder.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return "还没有待审提案。"
        lines = ["待审提案（看完回复 DSH「同意」或指出要改的条目）："]
        lines += [f"- {p.name}（{time.strftime('%Y-%m-%d %H:%M', time.localtime(p.stat().st_mtime))}）" for p in files[:10]]
        return "\n".join(lines)
