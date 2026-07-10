# CS Bot Agent 配置

Kimi Claw 官方智能客服 Agent，基于 LLM Agent 架构，运行在飞书群聊中。

## Skills 目录

Agent 可用技能位于 `.agents/cs-bot/skills/`：

| Skill | 说明 | 触发关键词 |
|-------|------|-----------|
| `self-improve` | 对话复盘与持续学习：分析历史聊天记录，提取错误纠正、暗知识、用户偏好，改进 skill 和 MEMORY | 复盘、学习总结、self improve、daily review |
| `web-screenshot` | 网页截图：支持 MoonGate 内网服务、全页截图、元素定位、设备模拟 | 截图、screenshot、网页截图 |

### self-improve 使用指南

1. **获取对话记录**：
   ```bash
   bash .agents/cs-bot/skills/self-improve/scripts/daily-review.sh [YYYY-MM-DD] [chat_id]
   ```
   - 不指定参数时，默认拉取今天的记录，chat_id 自动从 `worker_oc_*.log` 检测
   - 常见群聊：`oc_246aa27c756db09301074cb7d9c2a26a`、`oc_8438587abb22f822bdda0d6281637b47`、`oc_863adb1ec4eb3dfa7839ce24d2fa4ef7`

2. **学习日志目录**：`.learning/`
3. **长期记忆文件**：`MEMORY.md`

### web-screenshot 使用指南

1. **基础用法**：
   ```bash
   python3 .agents/cs-bot/skills/web-screenshot/scripts/screenshot.py <url> -o output.png
   ```
2. **环境依赖**：需安装 Playwright 和 Pillow（`pip install playwright pillow && playwright install chromium`）
3. **MoonGate 内网服务**：自动读取 `$MOONGATE_ACCESS_TOKEN` 环境变量

## 开发规范

- 遵循现有代码风格
- 新增功能补充测试用例
- 禁止主动使用 SearchWeb 工具
