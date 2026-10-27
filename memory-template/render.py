#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 memory/store.json 渲染成两张卡。

  card.md             —— 给人看的完整版（dev + companion）
  card.companion.txt  —— 给聊天插件注入的精简版（只含 companion）

用法：
    python memory/render.py          # 渲染并写入
    python memory/render.py --check  # 只校验，不写文件

store.json 结构见 memory/README.md。只用标准库，AstrBot 的 venv 和系统 Python 都能跑。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "store.json"
CARD = ROOT / "card.md"
COMPANION = ROOT / "card.companion.txt"

SCOPES = {"dev", "companion"}


def load_store(path: Path = STORE) -> dict:
    if not path.exists():
        raise SystemExit(f"[render] 找不到 {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[render] store.json 不是合法 JSON：{exc}")

    seen: set[str] = set()
    for key in ("facts", "judgments", "taboos"):
        items = data.get(key)
        if not isinstance(items, list):
            raise SystemExit(f"[render] store.json 缺少数组字段 {key!r}")
        for item in items:
            if not isinstance(item, dict):
                raise SystemExit(f"[render] {key} 里有非对象条目：{item!r}")
            for field in ("id", "text", "scope", "confidence"):
                if field not in item:
                    raise SystemExit(f"[render] {key}/{item.get('id', '?')} 缺少字段 {field!r}")
            if item["id"] in seen:
                raise SystemExit(f"[render] id 重复：{item['id']!r}")
            seen.add(item["id"])
            bad = set(item["scope"]) - SCOPES
            if bad:
                raise SystemExit(f"[render] {item['id']!r} 的 scope 非法：{sorted(bad)}")
            if not 0.0 <= float(item["confidence"]) <= 1.0:
                raise SystemExit(f"[render] {item['id']!r} 的 confidence 必须在 0~1 之间")
    return data


def conf_label(value: float) -> str:
    if value >= 0.85:
        return "确信"
    if value >= 0.6:
        return "大致"
    return "待验证"


def pick(items: list[dict], scope: str) -> list[dict]:
    return [i for i in items if scope in i["scope"]]


def render_markdown(store: dict) -> str:
    today = date.today().isoformat()
    lines = [
        f"# 判断卡 · {today}",
        "",
        "> 由 `memory/store.json` 渲染，不要直接改这个文件；改 store.json 后跑 `python memory/render.py`。",
        "",
        "## 事实（确认过的）",
        "",
    ]
    facts = store["facts"]
    if facts:
        for f in facts:
            lines.append(f"- {f['text']}（{conf_label(float(f['confidence']))}）")
    else:
        lines.append("- （暂无）")

    lines += ["", "## 判断（会随证据更新）", ""]
    judgments = [j for j in store["judgments"] if j.get("status", "active") == "active"]
    if judgments:
        for j in sorted(judgments, key=lambda x: -float(x["confidence"])):
            tags = "/".join(j["scope"])
            lines.append(f"- [{conf_label(float(j['confidence']))}][{tags}] {j['text']}")
    else:
        lines.append("- （暂无）")

    lines += ["", "## 洞察（在观察，还没定论）", ""]
    insights = [
        i for i in store.get("insights", [])
        if i.get("status", "emerging") not in ("promoted", "dropped")
    ]
    if insights:
        for ins in sorted(insights, key=lambda x: -float(x.get("confidence", 0.5))):
            evidence = len([e for e in (ins.get("evidence") or []) if e])
            lines.append(
                f"- [{ins.get('status', 'emerging')}][{conf_label(float(ins.get('confidence', 0.5)))}]"
                f" {ins['text']}（证据 {evidence} 条）"
            )
    else:
        lines.append("- （暂无）")

    lines += ["", "## 雷区", ""]
    taboos = store["taboos"]
    if taboos:
        for t in taboos:
            tags = "/".join(t["scope"])
            lines.append(f"- [{tags}] {t['text']}")
    else:
        lines.append("- （暂无）")
    lines.append("")
    return "\n".join(lines)


def render_companion(store: dict) -> str:
    facts = pick(store["facts"], "companion")
    judgments = [j for j in pick(store["judgments"], "companion") if j.get("status", "active") == "active"]
    taboos = pick(store["taboos"], "companion")

    lines = ["【你记得的关于哥哥的事】"]
    if facts:
        lines += [f"- {f['text']}。" for f in facts]
    else:
        lines.append("- （还没有记下什么。）")

    lines += ["", "【你对他这个人的判断（可以随着相处慢慢改）】"]
    if judgments:
        for j in sorted(judgments, key=lambda x: -float(x["confidence"])):
            lines.append(f"- {j['text']}。（{conf_label(float(j['confidence']))}）")
    else:
        lines.append("- （还在了解他。）")

    if taboos:
        lines += ["", "【绝对不能碰的】"]
        lines += [f"- {t['text']}。" for t in taboos]

    lines += [
        "",
        "这些是你自己记得的事，聊天时自然地用，不要逐条复述，也不要提到「记忆」「设定」「卡片」这类词。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="渲染判断卡")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args(argv)

    store = load_store()
    markdown = render_markdown(store)
    companion = render_companion(store)

    if args.check:
        print(f"[render] OK：{len(store['facts'])} 条事实，"
              f"{len(store['judgments'])} 条判断，{len(store['taboos'])} 条雷区")
        return 0

    CARD.write_text(markdown, encoding="utf-8")
    COMPANION.write_text(companion, encoding="utf-8")
    print(f"[render] 已写入 {CARD.name}（{len(markdown)} 字）")
    print(f"[render] 已写入 {COMPANION.name}（{len(companion)} 字）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
