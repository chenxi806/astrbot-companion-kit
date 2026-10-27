# 记忆目录说明

这个目录是「外置记忆」，六个插件和你的 AI 共用。建议放在：

```
<AstrBot 数据目录>/companion_memory/
```

## 文件

| 文件 | 作用 | 谁来写 |
|---|---|---|
| `store.json` | 唯一事实源：事实 / 判断 / 雷区 / 洞察 | 你、复盘插件 |
| `card.md` | 渲染出来的完整判断卡（给开发者/你自己看） | `render.py` |
| `card.companion.txt` | 给聊天模型注入的精简版 | `render.py` |
| `events/YYYY-MM.md` | 事件流，只追加 | 你、`/card log` |
| `raw/YYYY-MM-DD.jsonl` | 每轮真实问答 | growth_card 自动写 |
| `proposals/` | 复盘提案 | weekly_review |
| `lorebook.json` | 世界书条目（人格的分片） | 你 |
| `whims.json` | 状态机的小事池 | 你 |
| `changelog.md` | 判断变更记录 | 复盘插件 |

## 三层内容

- **事实（fact）**：确认过、基本不变。生日、忌口。
- **判断（judgment）**：从相处里得出的结论，带置信度和证据，会被推翻。
- **洞察（insight）**：还没定论的观察，攒够两条证据才能升成判断。

## 渲染

```bash
python render.py
```

改完 `store.json` 一定要重跑，否则注入的还是旧卡。
