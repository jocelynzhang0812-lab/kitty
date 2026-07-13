# PostgreSQL 分布式部署

分布式模式用于需要独立扩容 Agent Worker 的环境。它保留 SQLite 单进程模式，同时增加三个独立服务。

## 数据流

```text
Feishu
  -> Server: 验签、解密、Inbox 落盘、HTTP 200
  -> Agent Worker: Session Lease、模型与工具、会话保存、Outbox
  -> Sender: 飞书发送、稳定 UUID、退避重试、死信
```

PostgreSQL 是唯一事实源，保存会话、事件去重、Inbox、Session Lease 和 Outbox。第一版不依赖 Redis。

## 快速启动

```bash
cp kitty-runtime/.env.server.example kitty-runtime/.env.server
cp kitty-runtime/.env.worker.example kitty-runtime/.env.worker
cp kitty-runtime/.env.sender.example kitty-runtime/.env.sender
```

按角色填写配置后运行。Server 只获得验签密钥，Worker 只获得模型密钥，Sender 只获得飞书发送密钥：

```bash
docker compose -f docker-compose.distributed.yml up --build -d
curl http://127.0.0.1:8000/ready
```

扩容：

```bash
docker compose -f docker-compose.distributed.yml up -d \
  --scale worker=4 \
  --scale sender=2
```

Server 也可以放在外部负载均衡器后运行多个副本。Compose 示例默认只暴露一个 Server，避免本机端口冲突。

## 直接运行进程

三个进程读取相同的 `KITTY_DATABASE_URL`：

```bash
KITTY_DATABASE_URL=postgresql://... kitty server --env-file .env.server --host 0.0.0.0 --port 8000
KITTY_DATABASE_URL=postgresql://... kitty worker --env-file .env.worker
KITTY_DATABASE_URL=postgresql://... kitty sender --env-file .env.sender
```

常用配置：

```text
KITTY_DATABASE_URL=postgresql://user:password@postgres:5432/kitty
KITTY_WORKER_CONCURRENCY=4
KITTY_JOB_LEASE_SECONDS=180
KITTY_POLL_INTERVAL_SECONDS=0.25
KITTY_DELIVERY_MAX_ATTEMPTS=5
KITTY_DELIVERY_RETRY_BASE_SECONDS=1
```

生产环境不要使用 Compose 示例中的数据库密码，应改用 Secret Manager 或平台密钥注入。

## 一致性与恢复

- Inbox 和 Outbox 均采用至少一次处理；
- `job_id` 和 `inbox_job_id` 唯一约束负责去重；
- Session Lease 保证同一会话一次只由一个 Worker 执行；
- fencing token 阻止已经失去租约的 Worker 保存会话；
- Worker 或 Sender 崩溃后，租约到期的任务会被其他实例领取；
- Sender 对同一个 Outbox Job 始终使用同一个飞书 UUID；
- 回复在 Outbox 中持久化，因此发送重试不会重新调用模型和工具。

## 工作目录

会话历史已存入 PostgreSQL，但 Tools 和 Hooks 可能读写 `KITTY_WORKSPACE_ROOT`。同一会话可能在不同 Worker 副本间迁移，因此应满足以下任一条件：

1. Worker 共享同一个持久卷；
2. 工具保持无本地状态；
3. 工具把业务状态保存到外部数据库或对象存储。

跨主机部署不能使用普通 Docker named volume，需要 NFS、云盘或对象存储适配层。

## 运维

查看队列：

```bash
KITTY_DATABASE_URL=postgresql://... kitty jobs
```

重放死信：

```bash
KITTY_DATABASE_URL=postgresql://... kitty retry-job inbox JOB_ID
KITTY_DATABASE_URL=postgresql://... kitty retry-job outbox JOB_ID
```

告警建议：

- `inbox.dead > 0` 或 `outbox.dead > 0`；
- 最老 pending job 等待时间持续增长；
- processing 数量长期大于 Worker 并发；
- Server `/ready` 返回 503；
- PostgreSQL 连接池或磁盘空间不足。
