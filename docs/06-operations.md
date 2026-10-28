# 运维

## 你的日常

正常聊天。只在想标记时动手：

| 想做什么 | 发 |
|---|---|
| 喜欢她刚才那句 | `/good` |
| 不喜欢 | `/bad` |
| 立刻记一条规则 | `/记 内容` |
| 记事实 | `/事实 内容` |

## 每周

周日晚上会收到两条通知。

### 判断卡

```
/审批     → 看本周提取
/通过 1,3 → 只写这几条
```

### 示例池

```
/池审     → 看重复、超配
/池通过 2 → 执行
```

## 每月

每月 1 号 21:00：

```
/月审          → 判断卡月度报告
/月通过 1,3    → 执行
/池月审        → 示例池月度审查
/池月通过 1,3  → 执行
```

## 改文件

| 改什么 | 生效 | 要重启吗 |
|---|---|---|
| `card/store.json` | 每轮直接读 | 不用 |
| `voice_pool/examples.jsonl` | 每轮抽样 | 不用 |
| `modes/alice_corpus.json` | 下一轮抽样本 | 不用 |
| `card/card.companion.txt` | 生成产物，不影响注入 | — |
| 插件 `main.py` | 部署后 | 要 |
| 插件配置 | 写入后 | 要 |

## 备份

至少备份：

```
memory/card/
memory/voice_pool/
memory/modes/
memory/raw/
```

都是纯文本，直接复制即可。

## 日志

AstrBot 日志位置：

```
<AstrBot>/data/logs/astrbot.log
```

常见关键字：

| 关键字 | 含义 |
|---|---|
| `astrbot_plugin_memory_card` | 判断卡 |
| `astrbot_plugin_voice_pool` | 示例池 |
| `astrbot_plugin_mode_switch` | 模式 |
| `iscoroutinefunction` | 钩子不是 async，检查装饰器位置 |

## 故障排查

| 现象 | 先查 |
|---|---|
| 命令没反应 | 是否多个 `@filter.command` 叠加；日志有无注册 |
| `/审批` 说没新聊天 | `raw/` 有没有当天文件；`cursor.json` 是否卡住 |
| `/通过` 没反应 | `/待审` 是否为空 |
| 收不到周/月通知 | 配置 `umo` 为空时用最近私聊 |
| 模式退不出 | 发「退出」；或删 `modes/state.json` |
| 判断卡不注入 | `store.json` 是否为空；`enabled` 是否为 true |
| 示例池不注入 | `examples.jsonl` 是否为空；`status` 是否全 archived |

## 重置

想从零开始：

1. 备份整个 `memory/`
2. 把 `memory-template/` 覆盖回去
3. 重启

不要只删 `store.json` 不删 `pending.json`，否则会残留旧提案。
