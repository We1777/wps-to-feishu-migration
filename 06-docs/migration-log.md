# 迁移日志

| 日期 | 阶段 | 操作人 | 操作内容 | 结果 | 备注 |
|---|---|---|---|---|---|
| 2026-05-27 | 准备 | Fiona | 工程目录搭建、配置文件与脚本生成 | 完成 | |
| | 准备 | | 填写target-space.json | | 飞书folder_token |
| | 下载 | | WPS批量下载至02-downloads | | |
| | 扫描 | | python scripts/scan-source.py | | |
| | 校验 | | python scripts/verify-migration.py | | |
| | 上传 | | python scripts/upload-to-feishu.py | | |
| | 清理 | | WPS云盘保留30天后清理 | | |
