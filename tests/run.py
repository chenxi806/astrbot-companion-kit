# -*- coding: utf-8 -*-
"""仓库自检：结构、配置、模板、脱敏。不依赖 AstrBot 运行。"""

import json
import py_compile
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILS = []
PASSES = []


def check(name, fn):
    try:
        fn()
        PASSES.append(name)
        print(f"[PASS] {name}")
    except Exception as exc:
        FAILS.append((name, str(exc)))
        print(f"[FAIL] {name}: {exc}")


PLUGINS = [
    "memory_card",
    "card_review",
    "voice_pool",
    "pool_review",
    "mode_switch",
]


def t_plugins():
    for name in PLUGINS:
        base = ROOT / "plugins" / f"astrbot_plugin_{name}"
        assert (base / "main.py").exists(), name
        assert (base / "metadata.yaml").exists(), name
        meta = (base / "metadata.yaml").read_text(encoding="utf-8")
        assert "name:" in meta and "display_name:" in meta, name
        py_compile.compile(str(base / "main.py"), doraise=True)


def t_configs():
    for name in PLUGINS:
        path = ROOT / "config" / f"astrbot_plugin_{name}_config.json"
        assert path.exists(), name
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "memory_dir" in data, name


def t_memory_template():
    store = json.loads((ROOT / "memory-template" / "card" / "store.json").read_text(encoding="utf-8"))
    assert set(store) == {"facts", "judgments", "taboos", "insights"}
    assert all(store[k] == [] for k in store)

    corpus = json.loads((ROOT / "memory-template" / "modes" / "alice_corpus.json").read_text(encoding="utf-8"))
    entries = corpus["entries"]
    assert len(entries) >= 40, len(entries)
    assert all(e.get("zh") for e in entries)


def t_no_personal_paths():
    patterns = [r"[A-Za-z]:\\deepseek", r"E:\\bot\\instances", r"sk-[A-Za-z0-9]{20,}"]
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix.lower() in {".png", ".jpg", ".webp", ".gif", ".zip"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for pattern in patterns:
            assert not re.search(pattern, text), f"{path}: {pattern}"


def t_readme():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for section in ("快速开始", "命令表", "设计原则", "隐私", "License"):
        assert section in text, section


def t_docs():
    for name in (
        "01-architecture.md",
        "02-quickstart.md",
        "03-judgment-card.md",
        "04-example-pool.md",
        "05-modes.md",
        "06-operations.md",
        "07-design.md",
        "08-faq.md",
    ):
        assert (ROOT / "docs" / name).exists(), name


check("五个插件与元数据", t_plugins)
check("配置模板", t_configs)
check("记忆模板", t_memory_template)
check("脱敏检查", t_no_personal_paths)
check("README 结构", t_readme)
check("文档齐全", t_docs)

print(f"\n结果：{len(PASSES)} 通过 / {len(FAILS)} 失败")
sys.exit(1 if FAILS else 0)
