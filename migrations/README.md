# 数据库迁移

云端模式以 PostgreSQL 为权威数据源。迁移命令从 `LUMENX_DATABASE_URL` 读取连接地址：

```bash
alembic upgrade head
```

桌面模式不需要运行这些迁移。
