# 飞书多维表格只读资料库

此模块独立于原金山文档的招聘/宣讲同步。飞书 Base 的原始数据、字段和视图只写入 `feishu_*` 表，所有查询接口位于 `/api/admin/feishu/*`，必须登录管理员。现有 `/api/public` 不读取这些表，访客可见性保持原样。

## 接入

该 Base 当前已开启公开查看。同步使用公开网页的只读接口和匿名访客 Cookie，不需要 App ID、App Secret 或任何写权限。将下列值写入 WSL 部署目录的私有 `.env`：

```dotenv
FEISHU_BASE_TOKEN=FBysbrfNWa22l7s6a2scDsfEnTf
FEISHU_BASE_URL=https://wcn5yiabqbxh.feishu.cn/base/FBysbrfNWa22l7s6a2scDsfEnTf?table=tblPA2KMUBFbcATH&view=vewQBnaGZU
FEISHU_TABLE_ID=tblPA2KMUBFbcATH
FEISHU_VIEW_ID=vewQBnaGZU
FEISHU_PUBLIC_SYNC=1
FEISHU_SYNC_INTERVAL_SECONDS=3600
```

然后在部署目录执行 `docker compose -f compose.yaml -f compose.wsl.yaml up -d --build app`。登录管理后台，打开“飞书资料库”，可手动点“全量同步”。服务以上次同步尝试的完成时间为基准，每 1 小时自动同步一次；重启不会额外触发一次大表读取。后台会显示每次同步的成功、失败和数量。公开分享被关闭时，旧数据保留，错误信息显示在管理界面。

## 读取范围与保存方式

同步使用飞书公开网页的只读接口，枚举 Base 的全部数据表，分页读取各表的字段、视图与全部记录。目标 URL 中的 `view_id` 用于建立公开访客上下文；记录接口随后按数据表总量读取完整记录。原始字段 JSON 按 `(base_token, table_id, record_id)` 保存，字段元数据按 `field_id` 保存，以适应字段重命名。字段值中的附件和关联信息目前按公开接口返回的原始结构保存；尚未批量下载附件，也未把临时下载链接当成永久地址。公开分享被关闭后，同步会失败并保留上一次成功结果。

所有远程分页读取完成后，才在一个数据库事务中更新镜像。源表移除的记录保留在本地并标为 `source_missing`，管理员列表只显示当前存在的记录。网络、权限或单页错误不会覆盖上一次成功结果。

管理员“飞书资料库”按常用求职字段建立本地检索索引，可以搜索原始字段，组合筛选行业、企业性质、招聘类型、地点、学历、笔试、状态、优先级和自定义标签，按更新时间等字段排序，并在列表中显示选定分组。详情保留飞书原始字段。每条记录可保存跟进状态、0–3 级优先级、标签和备注；标记存入服务端数据库，管理员在不同设备登录后可见。默认状态为“待筛选、关注、已投递、笔试、面试、Offer、暂不考虑”。同步更新原始字段时不会覆盖个人标记。首次访问时会为既有镜像在本地补建索引，无需重新读取飞书。

当前搜索为本地字段子串匹配，分组用于列表展示；尚未提供语义检索、Agent、跨记录公司聚合或投递提醒。`feishu_record_search` 和 `feishu_annotations` 与原始镜像分表保存，便于后续扩展。

## 管理员接口

- `GET /api/admin/feishu/status`：配置与运行状态，不返回密钥。
- `POST /api/admin/feishu/sync`：启动一次全量同步。
- `GET /api/admin/feishu/manifest`：数据表、字段、视图与数量。
- `GET /api/admin/feishu/records`：分页查询当前记录，支持 `q`、`status`、`priority`、`industry`、`company_type`、`recruitment_type`、`location`、`education`、`exam`、`tag`、`sort`、`direction`、`group_by`、`limit`、`offset`。
- `GET /api/admin/feishu/annotations`：管理员保存的全部跟进标记。
- `PUT /api/admin/feishu/annotations/{table_id}/{record_id}`：更新状态、优先级、标签和备注。

飞书 API 的错误、权限不足和分页异常会记录到 `feishu_sync_runs`。本模块不修改原有招聘/宣讲记录和录音数据。
