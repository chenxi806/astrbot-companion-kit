# -*- coding: utf-8 -*-
"""艾莉丝语音（按需）：/艾莉丝 <台词>

只在用户主动发命令时使用艾莉丝声线：
- 中文台词自动翻成日语（艾莉丝口吻），或直接给日语
- 调用交接好的 GPT-SoVITS v3 管线合成
- 以语音条发送
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import urllib.request

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star, register

HANDOVER = Path(r"E:\alice_voice_handover")
SYNTH = HANDOVER / "scripts" / "synthesize_eris_lora.py"
GEN_DIR = HANDOVER / "generated" / "level_matched"
PY = r"D:\tts\gptvenv\Scripts\python.exe"
CFG = Path(r"E:\bot\instances\cc74b61e-a5ba-4b2f-a339-ab44d5510b76\core\data\cmd_config.json")

KANA = re.compile(r"[\u3040-\u30ff]")

TRANSLATE_PROMPT = (
    "你是日语配音台本翻译。只输出一行日语台词，不要解释、不要引号、不要括号动作。\n"
    "严格保留人称：中文的「我」译成「私」，不要换成「你」；谁来了、谁硬撑，不能换人。\n"
    "例：你来了，我就不用一个人硬撑了。→ 来てくれたなら、私は一人で無理しなくて済む。\n"
    "角色：艾莉丝，傲娇、直率、嘴硬心软。用口语，保留原文语气和句末标点。"
)


@register("eris_voice", "local", "艾莉丝语音：/艾莉丝 <台词>（按需角色扮演）", "1.0.0")
class ErisVoice(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._busy = False

    def _alice_active(self) -> bool:
        raw = str(self.config.get("memory_dir", "") if hasattr(self.config, "get") else "" or "").strip()
        if not raw:
            return True
        try:
            state = json.loads((Path(raw) / "modes" / "state.json").read_text(encoding="utf-8"))
            if state.get("mode") != "alice":
                return False
            expires = datetime.fromisoformat(str(state.get("expires_at", "")))
            return datetime.now() < expires
        except Exception:
            return False

    def _cfg(self) -> tuple[str, str, str]:
        data = json.loads(CFG.read_text(encoding="utf-8-sig"))
        src = next(s for s in data.get("provider_sources", []) if s.get("id") == "deepseek")
        key = (src.get("key") or [""])[0]
        base = (src.get("api_base") or "https://api.deepseek.com/v1").rstrip("/")
        model = "deepseek-flash"
        return key, base, model

    @staticmethod
    def _clean_jp(raw: str) -> str:
        bad = ("需要", "翻译", "原文", "输出", "要求", "例：", "台词", "日语")
        best = ""
        best_score = -999
        for line in str(raw or "").splitlines():
            line = line.strip().strip("「」『』\"'")
            line = re.sub(r"^(喂|哎|诶|嗯|哦|啊|呀|唉)[…，,、\s]*", "", line)
            if not line:
                continue
            if any(ch in line for ch in "你我的了吗呢吧这那"):
                continue
            kana = len(re.findall(r"[\u3040-\u30ff]", line))
            if kana == 0:
                continue
            penalty = sum(2 for word in bad if word in line)
            score = kana - penalty * 5
            if score > best_score:
                best, best_score = line, score
        return best if best_score > 0 else ""

    def _translate(self, text: str) -> str:
        if KANA.search(text):
            return text
        try:
            key, base, model = self._cfg()
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": TRANSLATE_PROMPT},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.7,
                "max_tokens": 800,
            }
            req = urllib.request.Request(
                base + "/chat/completions",
                data=json.dumps(body).encode(),
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read().decode())
            msg = res["choices"][0]["message"]
            out = self._clean_jp((msg.get("content") or "").strip())
            if not out:
                retry = {"model": model, "messages": [
                    {"role": "system", "content": "只输出一行日语台词，不要解释。"},
                    {"role": "user", "content": text},
                ], "temperature": 0.2}
                req2 = urllib.request.Request(
                    base + "/chat/completions",
                    data=json.dumps(retry).encode(),
                    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req2, timeout=60) as r2:
                    out = self._clean_jp((json.loads(r2.read().decode())["choices"][0]["message"].get("content") or ""))
            if not out:
                # deepseek-flash 可能把内容全放进 reasoning_content
                reason = msg.get("reasoning_content") or ""
                lines = [x.strip() for x in reason.splitlines() if x.strip()]
                jp = [x for x in lines if KANA.search(x)]
                out = jp[-1] if jp else ""
            out = out.strip("「」\"'“”")
            return out or text
        except Exception as exc:
            logger.warning(f"[eris_voice] translate failed: {exc}")
            return text

    def _cached_wav(self, text: str) -> Path | None:
        cache = HANDOVER / "cache_bank"
        manifest = cache / "manifest.json"
        if not manifest.exists():
            return None
        try:
            import json as _json

            mtime = manifest.stat().st_mtime
            if getattr(self, "_cache_mtime", None) != mtime:
                data = _json.loads(manifest.read_text(encoding="utf-8"))
                mapping = {}
                for run in data.get("runs", []):
                    src = str((run.get("request") or {}).get("text", "")).strip()
                    name = str(run.get("file", "")).strip()
                    if src and name:
                        wav = cache / "level_matched" / name
                        mapping[src] = wav if wav.exists() else cache / name
                self._cache_map = mapping
                self._cache_mtime = mtime
            return getattr(self, "_cache_map", {}).get(str(text).strip())
        except Exception as exc:
            logger.warning(f"[eris_voice] cache lookup failed: {exc}")
            return None

    def _synthesize(self, jp: str) -> Path | None:
        started = time.time()
        try:
            proc = subprocess.run(
                [PY, "-X", "utf8", str(SYNTH), "--text", jp],
                cwd=str(HANDOVER),
                capture_output=True,
                timeout=240,
            )
            tail = (proc.stdout or b"").decode("utf-8", "replace")[-400:]
            logger.info(f"[eris_voice] synth rc={proc.returncode} tail={tail}")
        except Exception as exc:
            logger.warning(f"[eris_voice] synth failed: {exc}")
            return None

        candidates = [p for p in GEN_DIR.glob("*.wav") if p.stat().st_mtime >= started - 2]
        if not candidates:
            return None
        wav = max(candidates, key=lambda p: p.stat().st_mtime)
        try:
            import wave

            with wave.open(str(wav), "rb") as fh:
                duration = fh.getnframes() / float(fh.getframerate() or 1)
            limit = max(10.0, len(jp) * 0.9)
            if duration > limit:
                logger.warning(f"[eris_voice] discard {duration:.1f}s audio for {len(jp)} chars: {jp[:30]}")
                return None
        except Exception as exc:
            logger.warning(f"[eris_voice] duration check failed: {exc}")
        return wav

    @staticmethod
    def _clean_line(text: str) -> str:
        text = str(text or "").strip()
        text = re.sub(r"[（(][^）)]*[）)]", "", text)
        text = re.sub(r"[【\[][^】\]]*[】\]]", "", text)
        text = re.sub(r"^[^：:]{1,8}[：:]", "", text)
        text = text.strip().strip("「」『』\"'“”")
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _pick_fragment(self, emotion: str, text: str) -> tuple[str, str]:
        text = str(text or "").strip()
        if re.search(r"[\u3040-\u30ff]", text) and len(text) <= 20:
            return text, ""
        try:
            import json as _json
            import random

            data = _json.loads((Path(__file__).with_name("alice_voice_bank.json")).read_text(encoding="utf-8"))
            rows = [v for v in data.get("voices", []) if v.get("jp")]
            same = [v for v in rows if v.get("emotion") == emotion]
            pool = same or rows
            recent = getattr(self, "_recent_fragments", [])
            fresh = [v for v in pool if v["jp"] not in recent] or pool
            pick = random.choice(fresh)
            recent = (recent + [pick["jp"]])[-6:]
            self._recent_fragments = recent
            return pick["jp"], pick.get("zh", "")
        except Exception as exc:
            logger.warning(f"[eris_voice] pick fragment failed: {exc}")
            return "……べ、別に。", "……才没有。"

    def _voice_dir(self) -> Path:
        return Path(__file__).parent

    def _full_allowed(self) -> bool:
        import json as _json
        import time

        cooldown = 180
        try:
            policy = _json.loads((self._voice_dir() / "alice_voice_policy.json").read_text(encoding="utf-8"))
            cooldown = int(policy.get("cooldown_seconds", 180))
        except Exception:
            pass
        try:
            state = _json.loads((self._voice_dir() / "alice_voice_state.json").read_text(encoding="utf-8"))
        except Exception:
            state = {}
        return time.time() - float(state.get("last_full", 0)) >= cooldown

    def _mark_full(self) -> None:
        import json as _json
        import time

        try:
            (self._voice_dir() / "alice_voice_state.json").write_text(
                _json.dumps({"last_full": time.time()}), encoding="utf-8"
            )
        except Exception:
            pass

    @filter.llm_tool(name="eris_tts_speak")
    async def eris_tts_speak(
        self,
        event: AstrMessageEvent,
        emotion: str = "傲娇",
        mode: str = "short",
        text: str = "",
    ):
        """发一条艾莉丝语音。默认只发情绪碎片；值得听的整句才用 mode=full。
        emotion：害羞/傲娇/生气/开心/吃醋/关心/撒娇/惊讶/嘴硬/软。
        mode：short=2~12字碎片；full=把 text 这一句完整念出来（最多 45 字，只传那一句，不要带动作和旁白）。
        text：mode=full 时填中文或日语原句；short 可留空。"""
        if self._busy:
            alive = [t for t in getattr(self, "_tasks", set()) if not t.done()]
            if alive:
                return "上一条语音还在合成，这条先不发了"
            self._busy = False
        clean = self._clean_line(text)
        downgraded = False
        if mode == "full" and clean and not self._full_allowed():
            mode = "short"
            downgraded = True
        subtitle = ""
        if mode in ("full", "line") and clean:
            if len(clean) > 45:
                clean = re.split(r"[。！？!?]", clean)[0][:45]
            payload, translate = clean, not bool(re.search(r"[\u3040-\u30ff]", clean))
            if mode == "full":
                self._mark_full()
        else:
            payload, subtitle = self._pick_fragment(emotion, text)
            translate = False
        self._busy = True
        self._tasks = getattr(self, "_tasks", set())
        task = asyncio.create_task(self._deliver_voice(event, payload, translate, subtitle))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        tier = "整句" if translate or mode == "full" else "碎片"
        if downgraded:
            tier = "碎片（整句冷却中）"
        return f"语音稍后送达（{emotion}，{tier}）；继续用中文回复正文"

    async def _deliver_voice(self, event: AstrMessageEvent, text: str, translate: bool = False, subtitle: str = "") -> None:
        logger.info(f"[eris_voice] deliver start translate={translate} subtitle={subtitle!r} text={text[:40]}")
        try:
            jp = await asyncio.to_thread(self._translate, text) if translate else text
            wav = self._cached_wav(jp)
            if wav is None:
                wav = await asyncio.to_thread(self._synthesize, jp)
            if wav is None:
                wav = self._cached_wav("……べ、別に。")
            if not wav:
                logger.warning("[eris_voice] synth produced no wav")
                return
            await event.send(event.chain_result([Record(file=str(wav))]))
            if subtitle:
                await event.send(event.chain_result([Plain(subtitle)]))
        except Exception as exc:
            logger.warning(f"[eris_voice] deliver failed: {exc}")
        finally:
            self._busy = False

    @filter.command("艾莉丝")
    async def eris(self, event: AstrMessageEvent):
        raw = (getattr(event, "message_str", "") or "").strip()
        text = re.sub(r"^[/／]?艾莉丝", "", raw).strip()
        if not text:
            yield event.plain_result(
                "用法：/艾莉丝 <台词>\n"
                "中文会自动翻成日语，也可以直接写日语。\n"
                "例：/艾莉丝 我才不是担心你呢"
            )
            return
        if self._busy:
            yield event.plain_result("まだ作ってる途中だよ、ちょっと待って。")
            return

        self._busy = True
        try:
            yield event.plain_result("ちょっと待って、今作ってる……")
            jp = await asyncio.to_thread(self._translate, text)
            wav = await asyncio.to_thread(self._synthesize, jp)
            if not wav:
                yield event.plain_result("今のは失敗しちゃった。もう一回やってみる？")
                return
            yield event.plain_result(f"「{jp}」")
            await event.send(event.chain_result([Record(file=str(wav))]))
        finally:
            self._busy = False
