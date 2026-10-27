# 贡献指南

## 提 Issue

- 先说清楚：AstrBot 版本、平台、插件版本
- 贴日志（`data/logs/astrbot.log` 里相关的那几行），**去掉 API key、QQ 号、openid**
- 如果是行为不符合预期，写下你期望的行为

## 提 PR

1. 一个插件一个 PR，别混在一起
2. 不引入第三方依赖（只用标准库 + AstrBot API），除非有充分理由
3. 配置项必须有 `_conf_schema.json` 里的 `description` 和 `hint`
4. 涉及数据文件的改动，要兼容旧格式（读的时候容忍缺字段）
5. 不提交任何个人数据：`store.json`、`raw/`、`card.companion.txt`、真实 QQ 号

## 代码风格

- Python 3.10+，类型注解能写就写
- 日志统一用 `[插件名]` 前缀
- 异常要吞得明白：捕获后要么降级、要么写日志，不要静默

## 测试

每个插件目录下可以放 `test_*.py`，用 `python test_xxx.py` 直接跑。
测试用的临时目录不要提交。

## 文档

- 面向新手的步骤放 `docs/03-快速开始.md`
- 参数解释放 `docs/04-配置手册.md`
- 设计原因放 `docs/07-设计取舍.md`
