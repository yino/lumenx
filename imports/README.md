# 本地导入暂存区

云端部署只允许平台管理员从该目录发现并导入旧版 LumenX 本地数据。
Docker Compose 默认把宿主机 `./imports` 只读挂载到后端 `/imports`。

每个待导入目录可包含 `output/projects.json`、`output/series.json`、
`output/library_assets.json`、可选的 `output/playground_history.json`，以及这些
JSON 引用的本地媒体文件。不要在此目录放置平台密钥或数据库备份。
