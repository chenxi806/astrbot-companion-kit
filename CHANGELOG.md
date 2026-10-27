# Changelog

## v0.1.0 — 2026-10-27

首个公开版本。

### 新增

- `astrbot_plugin_growth_card`：判断卡注入 + raw 记录 + `/card` 命令
- `astrbot_plugin_lorebook`：世界书（关键词 + 概率 + 冷却）
- `astrbot_plugin_galgame`：关系阶段（认识→熟悉→亲近→亲密）+ 事件 + 选项
- `astrbot_plugin_state`：心情/精力慢漂移 + whim 池
- `astrbot_plugin_daily_ritual`：早晚安定时消息 + 补发窗口
- `astrbot_plugin_weekly_review`：事件→洞察→判断三层复盘 + NOOP + 置信度衰减
- `memory-template/`：store/render/whims/lorebook/核心人格模板
- `scripts/`：安装、初始化脚本
- `docs/`：七篇中文文档

### 设计

- 记忆全部外置，纯文本，可读可改可回滚
- 人格拆成「核心常驻 + 世界书分片」
- 关系与状态用慢变量，不用可刷的分数
- 判断变更强制引用洞察，且必须人工确认
