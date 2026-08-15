# Docker secrets

复制并填写同名 `.txt.example` 文件，真实 `.txt` 文件已被 Git 忽略。生产部署应由部署系统挂载 secrets，而不是把凭据写入仓库或 Compose 环境变量。

`postgres_password.txt` 属于迁移和备份管理员；`postgres_app_password.txt` 属于 backend、worker 和维护任务使用的非超级应用角色。两个密码必须不同，应用服务禁止使用 `POSTGRES_USER`，否则 PostgreSQL 会绕过 RLS。
