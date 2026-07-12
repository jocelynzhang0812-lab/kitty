# Kitty 10 分钟上线

这条路径适合第一次部署：先准备一个公网 HTTPS 地址和飞书企业自建应用，Kitty 负责其余配置检查。

## 0—2 分钟：安装

```bash
git clone https://github.com/jocelynzhang0812-lab/kitty.git
cd kitty/kitty-runtime
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
```

## 2—5 分钟：运行配置向导

```bash
.venv/bin/kitty setup
```

浏览器会打开四步向导：

1. 定义机器人名称、职责和 System Prompt；
2. 填写 OpenAI-compatible 模型的 Base URL、模型名称和 API Key；
3. 填写飞书 App ID、App Secret、Verification Token、Encrypt Key 和公网地址；
4. 同时检测配置、存储、扩展、模型和飞书，再保存 `.env`。

向导只监听本机 `127.0.0.1`，使用随机访问令牌。模型和飞书密钥不会显示在结果中，生成的 `.env` 权限为 `0600`。

## 5—7 分钟：配置飞书

在[飞书开放平台](https://open.feishu.cn/)创建企业自建应用并启用机器人：

- 添加接收单聊、群聊 @ 消息和机器人发送消息权限；
- 在“事件与回调”中订阅 `im.message.receive_v1`；
- 选择“将事件发送至开发者服务器”；
- 粘贴向导给出的 `https://你的域名/feishu/events`；
- 确认 Verification Token 和 Encrypt Key 与向导中一致，然后发布应用。

本机开发可以使用可信 HTTPS 隧道；真实生产环境应使用固定域名、TLS 网关和持久卷。

## 7—8 分钟：再次体检

```bash
.venv/bin/kitty doctor --env-file .env --live
```

只有五项都显示通过才继续。`--live` 会产生一次极小的模型请求，并验证飞书应用凭据。

## 8—10 分钟：启动并验收

```bash
.venv/bin/kitty serve --env-file .env
```

检查：

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

最后在飞书中向机器人发送一条单聊消息，并在群聊中分别测试“不 @ 不回复”和“@ 后回复”。

若使用 Docker、反向代理或需要故障重放，请继续阅读[飞书生产部署指南](production-deployment.md)。
