# Contributing

感谢你愿意一起完善这个项目。

## 可以贡献什么

- 新的记忆系统（比如“关系事件”“承诺清单”）
- 更好的审查策略（去重、聚类、权重）
- 文档、示例、翻译
- Bug 修复

## 开发流程

1. Fork 仓库
2. 新建分支 `feat/your-feature`
3. 修改代码/文档
4. 运行自检：

```bash
python tests/run.py
```

5. 提交 PR，说明改了什么、为什么

## 插件规范

- 目录名必须是 `astrbot_plugin_<name>`
- 必须有 `metadata.yaml`、`main.py`、`_conf_schema.json`
- 配置里的 `memory_dir` 默认留空，由用户填写
- 不要写死个人路径、API Key、聊天记录
- 多个命令别名用 `alias=`，不要叠加 `@filter.command`

## 数据规范

- 所有存储用 JSON / JSONL
- 写入用临时文件 + replace
- 删除一律用 `status: archived`，不物理删除

## 文档规范

- 中文为主，命令和字段保留英文
- 新增功能要更新 `docs/` 和 `README.md`
- 截图/示例不要包含真实用户数据

## 提交信息

```
feat: 增加示例池月度审查
fix: 修复模式退出后仍注入角色卡
docs: 补充判断卡字段说明
```
