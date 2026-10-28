# 快速开始

## 前置条件

- AstrBot `>= 4.24`
- Python `3.10+`
- 已配置好的 LLM Provider
- 可选：一个支持 `/艾莉丝` 语音的 TTS 插件（本仓库不含）

## 1. 放插件

把 `plugins/` 下五个目录复制到：

```
<AstrBot>/data/plugins/
```

目录名必须以 `astrbot_plugin_` 开头，AstrBot 靠它加载。

## 2. 放配置

把 `config/` 下五个 JSON 复制到：

```
<AstrBot>/data/config/
```

然后修改每个文件里的 `memory_dir`，指向同一个目录，例如：

```json
"memory_dir": "D:\\companion-memory"
```

留空则使用 `<AstrBot>/data/companion_memory`。

### 关键配置

| 插件 | 键 | 说明 |
|---|---|---|
| `memory_card` | `enabled` | 总开关 |
| `card_review` | `time` | 每周整理时间，默认 20:00 |
| `card_review` | `min_messages` | 至少攒多少条才整理 |
| `voice_pool` | `examples_per_turn` | 每轮抽几条，默认 4 |
| `mode_switch` | `alice_minutes` / `maid_minutes` | 模式时长，默认 10 |

## 3. 放记忆模板

把 `memory-template/` 复制到 `memory_dir`：

```
companion-memory/
├── card/
├── voice_pool/
├── modes/
├── raw/
├── core-persona.example.txt
└── lorebook.example.json
```

`core-persona.example.txt` 只是占位，真正的人格在 AstrBot 的 persona 里。

## 4. 重启

插件和配置在启动时加载。

## 5. 验证

依次发：

```
/card
/审批
/通过
/good
/pool
```

预期：

- `/card` 回四类计数
- `/审批` 生成待审（raw 里要有聊天）
- `/通过` 写入
- `/good` 回“收下了”
- `/pool` 回条数

## 6. 模式

```
艾莉丝模式
（聊几句）
退出
```

```
女仆模式
退出
```

## 常见启动失败

| 现象 | 原因 |
|---|---|
| 插件列表没有 | 目录名不对，或没有 `metadata.yaml` |
| 命令无反应 | 多个 `@filter.command` 叠加；必须用 `alias=` |
| `/good` 说没记住上一句 | 她还没回过话 |
| 没有周通知 | `umo` 为空且没有最近私聊记录 |
| 判断卡不注入 | `store.json` 为空，或 `enabled=false` |
