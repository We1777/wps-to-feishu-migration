# WPS云盘 → 飞书云盘 迁移工程

## 概述

将WPS云盘文件迁移至飞书云盘，会计档案严格按财政部电子凭证归档规则执行分类与保管期限管理。

**核心原则：不重命名任何文件**，仅按归档规则将文件分类到对应目录结构。

## 法规依据

| 文件编号 | 名称 |
|---|---|
| 第79号令 | 《会计档案管理办法》 |
| 财会〔2020〕6号 | 《关于规范电子会计凭证报销入账归档的通知》 |
| 财会〔2024〕11号 | 《会计信息化工作规范》 |
| DA/T 94-2022 | 《电子会计档案管理规范》 |
| 财会〔2025〕9号 | 《电子凭证会计数据标准》 |

## 目录结构

```
wps-to-feishu-migration/
├── 00-config/           配置中心（归档规则、源端/目标端映射）
├── 01-source-audit/     迁前审计（源端文件清单快照）
├── 02-downloads/        中转区（从WPS下载的原文件，按批次存放）
├── 03-staging/          整理区（按飞书目标结构重新组织）
├── 04-upload-logs/      上传日志
├── 05-verification/     迁后校验（完整性+合规性报告）
├── 06-docs/             项目文档（迁移日志、归档证明）
└── scripts/             自动化脚本
```

## 执行流程（SOP）

### 阶段一：准备

1. 填写 `00-config/source-account.json`（WPS源路径映射）
2. 填写 `00-config/target-space.json`（飞书云盘空间ID、folder_token）
3. 确认 `00-config/archive-rules.yaml` 归档规则无需调整

### 阶段二：下载（人工）

4. 从WPS云盘批量下载文件，按批次放入 `02-downloads/batch-xx/`

### 阶段三：扫描

5. 运行源端扫描：
   ```bash
   python scripts/scan-source.py
   ```
   生成 `01-source-audit/scan-report.csv`，记录全量文件清单与MD5

### 阶段四：分类整理

6. 运行自动分类：
   ```bash
   python scripts/organize-files.py
   ```
   文件按归档规则自动分类到 `03-staging/`，无法识别的进入 `03-staging/待分类/`

7. **人工复核**：检查 `03-staging/待分类/` 中的文件，手动移入正确目录

### 阶段五：校验

8. 运行发票校验（如有XML/OFD发票）：
   ```bash
   python scripts/validate-invoice.py
   ```

9. 运行完整性校验：
   ```bash
   python scripts/verify-migration.py
   ```
   确保源端与staging端文件数量、哈希一致

### 阶段六：上传

10. 运行飞书上传：
    ```bash
    python scripts/upload-to-feishu.py
    ```
    上传日志记录在 `04-upload-logs/`，失败文件进入 `error-retry-queue.txt`

11. **人工处理**：检查上传失败文件，排查原因后重传

### 阶段七：清理

12. 确认飞书云盘文件完整后，设置飞书权限（禁用外部分享）
13. WPS云盘保留30天过渡期后清理
14. 填写 `06-docs/archive-certificate.md` 归档合格证明

## 依赖

```bash
pip install pyyaml requests
```

## 人工 vs 自动化分工

| 步骤 | 执行方 |
|---|---|
| 填写配置文件 | 人工 |
| WPS云盘下载 | 人工 |
| 源端扫描 | 脚本 |
| 分类整理 | 脚本 + 人工复核待分类文件 |
| 发票校验 | 脚本 |
| 完整性校验 | 脚本 |
| 飞书上传 | 脚本 + 人工处理失败文件 |
| 权限设置 | 人工 |
| 归档证明 | 人工 |
