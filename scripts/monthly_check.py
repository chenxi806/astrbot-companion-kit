#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""月度体检：备份、列判断、归档、重渲染。

用法：
    python monthly_check.py --memory "D:\\path\\to\\memory"
    python monthly_check.py --memory "..." --archive dev-why,companion-focus
    python monthly_check.py --memory "..." --render-only
    python monthly_check.py --memory "..." --no-backup

只依赖标准库。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


def load_render(memory: Path):
    script = memory / "render.py"
    if not script.exists():
        raise SystemExit(f"找不到 {script}")
    spec = importlib.util.spec_from_file_location("memory_render", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def backup(memory: Path) -> Path:
    folder = memory / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = folder / f"memory-{stamp}"
    shutil.copytree(memory, target, ignore=shutil.ignore_patterns("backups", "__pycache__"))
    olds = sorted(folder.glob("memory-*"))
    for old in olds[:-6]:
        shutil.rmtree(old, ignore_errors=True)
    return target


def render(memory: Path) -> None:
    module = load_render(memory)
    store = module.load_store(memory / "store.json")
    (memory / "card.md").write_text(module.render_markdown(store), encoding="utf-8")
    (memory / "card.companion.txt").write_text(module.render_companion(store), encoding="utf-8")


def report(memory: Path) -> None:
    store = json.loads((memory / "store.json").read_text(encoding="utf-8"))
    today = date.today()

    print("\n== 判断 ==")
    actives = [j for j in store.get("judgments", []) if j.get("status", "active") == "active"]
    if not actives:
        print("（没有 active 判断）")
    for j in actives:
        try:
            days = (today - date.fromisoformat(str(j.get("updated") or j.get("created")))).days
        except Exception:
            days = 0
        flags = []
        if float(j.get("confidence", 0.6)) < 0.45:
            flags.append("分数低")
        if days > 30:
            flags.append(f"{days} 天没更新")
        if not (j.get("insights") or []):
            flags.append("无洞察")
        print(f"- {j['id']:<20} {j.get('confidence'):<5} {days:>3}天  {j['text'][:28]}")
        if flags:
            print(f"    -> {' / '.join(flags)}")

    print("\n== 洞察（在观察） ==")
    insights = [i for i in store.get("insights", []) if i.get("status") not in ("promoted", "dropped")]
    if not insights:
        print("（暂无）")
    for i in insights:
        print(f"- {i['id']:<20} {i.get('status'):<10} {i['text'][:30]}")

    print("\n== 世界书 ==")
    lore_path = memory / "lorebook.json"
    if lore_path.exists():
        lore = json.loads(lore_path.read_text(encoding="utf-8"))
        entries = lore.get("entries", [])
        print(f"条目数：{len(entries)}" + ("（偏多，建议合并）" if len(entries) > 25 else "（正常）"))
        counts: dict[str, int] = {}
        for e in entries:
            for k in e.get("keys") or []:
                counts[k] = counts.get(k, 0) + 1
        dup = {k: v for k, v in counts.items() if v > 1}
        if dup:
            top = sorted(dup.items(), key=lambda x: -x[1])[:5]
            print("重复关键词：" + "、".join(f"{k} x {v}" for k, v in top))

    print("\n== 文件 ==")
    for name in ("store.json", "card.md", "card.companion.txt"):
        p = memory / name
        size = p.stat().st_size if p.exists() else 0
        print(f"- {name:<20} {size} 字节")


def main() -> int:
    parser = argparse.ArgumentParser(description="月度体检")
    parser.add_argument("--memory", required=True, help="memory 目录")
    parser.add_argument("--archive", default="", help="要归档的判断 id，逗号分隔")
    parser.add_argument("--render-only", action="store_true", help="只重渲染")
    parser.add_argument("--no-backup", action="store_true", help="不备份")
    args = parser.parse_args()

    memory = Path(args.memory)
    if not (memory / "store.json").exists():
        raise SystemExit(f"不是记忆目录：{memory}")

    if args.render_only:
        render(memory)
        print("已重渲染 card.md / card.companion.txt")
        return 0

    if not args.no_backup:
        target = backup(memory)
        print(f"已备份到：{target}")

    if args.archive:
        ids = [x.strip() for x in args.archive.split(",") if x.strip()]
        store_path = memory / "store.json"
        store = json.loads(store_path.read_text(encoding="utf-8"))
        by_id = {j["id"]: j for j in store.get("judgments", [])}
        changed = []
        for jid in ids:
            entry = by_id.get(jid)
            if not entry:
                print(f"跳过（找不到）：{jid}")
                continue
            entry["status"] = "archived"
            entry["updated"] = date.today().isoformat()
            changed.append(jid)
        if changed:
            store["updated"] = date.today().isoformat()
            store_path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
            render(memory)
            print("已归档并重渲染：" + "、".join(changed))

    report(memory)
    print("\n提示：归档用 --archive id1,id2；只重渲染用 --render-only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
