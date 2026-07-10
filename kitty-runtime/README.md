# Kitty Runtime（CS-bot 飞书生产运行时）

这是一个根据 CS-bot 仓库现有接口反向实现的 Kitty 兼容运行时，不是原始 Kitty 源码。当前版本已经把 CS-bot 的知识库、业务工具、会话、模型调用和飞书收发链路接入同一个可部署服务。

## 当前能力

- 每个会话独立串行队列，不同会话可并发处理；
- SQLite 持久化会话历史和飞书 `message_id` 去重；
- 启动时自动加载 CS-bot、16 个业务工具和本地知识库；
- 自动恢复 CS-bot 的上下文历史；
- 支持 OpenAI-compatible 模型接口，也保留本地 mock 模式；
- 支持飞书 URL 校验、Verification Token、请求签名校验和 AES-256-CBC 解密；
- 支持单聊文字消息、群聊 @ 机器人消息和主动回复；
- 非文字消息返回固定引导语；
- 提供存活探针 `/health` 和就绪探针 `/ready`；
- 支持 Uvicorn 直接启动和 Docker 部署；
- 支持 `cli.wire`、`cli.turn_done` 与现有反馈 hook。

## 本地运行

需要 Python 3.11 及以上版本。

```bash
cd kitty-runtime
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m kitty --once "如何配置机器人？"
```

开发环境没有设置模型密钥时默认使用确定性的 mock 模型，不会发起外部请求。状态默认保存在 `kitty-runtime/.kitty/`。

## 生产启动

从仓库根目录执行：

```bash
cp kitty-runtime/.env.production.example .env.production
# 填写 .env.production 中的真实密钥和资源 ID
set -a
source .env.production
set +a
kitty-runtime/.venv/bin/uvicorn kitty.server:create_app \
  --factory --app-dir kitty-runtime --host 0.0.0.0 --port 8000 --proxy-headers
```

飞书事件订阅地址设置为：

```text
https://你的域名/feishu/events
```

完整的飞书权限、事件订阅、多维表字段、Docker 命令和上线验收步骤见 [生产部署指南](docs/production-deployment.md)。

## HTTP 接口

- `GET /health`：进程存活检查；
- `GET /ready`：CS-bot、知识库和运行时完成初始化后返回 200；
- `POST /feishu/events`：飞书加密事件入口；
- `POST /v1/messages`：内部调试接口；生产环境默认关闭，设置独立的 `KITTY_DEBUG_API_TOKEN` 后才启用。

生产环境会在进程启动阶段校验必需配置。缺少飞书密钥、模型密钥、多维表配置或内部通知 ID 时会直接启动失败，避免以半可用状态上线。

## Docker

从仓库根目录构建：

```bash
docker build -t cs-bot-kitty:local -f kitty-runtime/Dockerfile .
docker run --rm -p 8000:8000 \
  --env-file .env.production \
  -v kitty-data:/data/kitty \
  cs-bot-kitty:local
```

当前持久化层是 SQLite，因此生产部署默认使用单实例。要横向扩容，需要先把会话和事件去重迁移到共享存储。

容器安装使用 `requirements.lock` 中经过测试的精确依赖版本；升级依赖后应重新运行完整测试和生产启动检查。

## 测试

```bash
cd kitty-runtime
.venv/bin/python -m unittest discover -s tests -v
```

## 兼容边界

运行时实现的是从当前仓库可观察行为中推断出的兼容接口。原始 Kitty 是否使用远程队列、子进程、容器调度或独立授权系统仍然未知。Python hook 在运行时进程内执行，只应加载受信任代码。

更多说明见 [架构](docs/architecture.md)、[事件协议](docs/event-protocol.md) 和 [CS-bot 接入](docs/csbot-compatibility.md)。
