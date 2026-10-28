# -*- coding: utf-8 -*-
"""把人格文件写进 AstrBot 数据库（personas.default）。

用法：
  python write_persona.py --db "E:\\bot\\instances\\xxx\\core\\data\\data_v4.db" --file "D:\\companion\\memory\\core-persona.txt"
"""

import argparse
import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="AstrBot 的 data_v4.db 路径")
    ap.add_argument("--file", required=True, help="人格文本文件")
    ap.add_argument("--persona-id", default="default")
    args = ap.parse_args()

    db = Path(args.db)
    core = Path(args.file)
    if not db.exists():
        print("找不到数据库：", db)
        return 2
    if not core.exists():
        print("找不到人格文件：", core)
        return 2

    text = core.read_text(encoding="utf-8-sig")
    backup = db.parent / f"backup-persona-{datetime.now():%m%d-%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)

    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(backup / db.name)
    src.backup(dst)
    dst.close()
    src.close()

    con = sqlite3.connect(db)
    con.execute("PRAGMA busy_timeout = 8000")
    row = con.execute("select system_prompt from personas where persona_id=?", (args.persona_id,)).fetchone()
    if row is None:
        print("数据库里没有这个 persona_id：", args.persona_id)
        return 3
    old = row[0]
    con.execute(
        "update personas set system_prompt=?, updated_at=? where persona_id=?",
        (text, datetime.now().isoformat(timespec="seconds"), args.persona_id),
    )
    con.commit()
    new = con.execute("select system_prompt from personas where persona_id=?", (args.persona_id,)).fetchone()[0]
    con.close()

    print("旧长度", len(old), "->", "新长度", len(new))
    print("md5", hashlib.md5(new.encode()).hexdigest()[:10])
    print("备份", backup)
    print("下一步：重启 AstrBot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
