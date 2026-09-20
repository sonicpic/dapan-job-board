# 大潘的就业情报站

简明技术说明见 [`docs/TECHNICAL.md`](docs/TECHNICAL.md)。

- 网站：https://job.dapanclaw.top/
- 管理后台：https://job.dapanclaw.top/admin
- 数据源：https://www.kdocs.cn/l/REPLACE_ME
- 运行主机：本机 WSL Ubuntu，项目目录：`/home/zhihongpan/services/dapan-job-board`
- 入口服务器：AWS `43.213.3.74`，由 FRPS `58112` 转发到 WSL

## 功能

招聘信息支持关键词、城市、学历、单位类型及状态筛选，卡片和列表切换，详情与浏览器本地收藏。宣讲会支持日期分组、日历选择、今日及往期查看。管理员登录后，前台还会显示“面试记录”栏目，可直接增删改记录、上传面试录音并启动转写和复盘总结。使用 React、Ant Design 和 Ant Design Icons；按钮、表单、表格、抽屉、日历等均为开源组件。

后台支持手动添加、编辑、删除、访客可见、归档、置顶及恢复信息；状态、访客可见、归档和置顶列均可排序。关闭“访客可见”后，访客看不到该条信息，管理员登录后的前台仍可见；归档后所有前台都不显示，只在后台保留。置顶不会绕过这两条可见规则。人工调整不会回写金山文档；点击“恢复”会清除此条记录的全部人工覆盖。删除原表记录时会保存墓碑，防止后续同步重新加入。

网评分析采用“搜索层 + 模型分析层”。后台按公司提交任务后，服务先取得可核验的公开网页结果，再让模型基于这些材料归纳求职相关的常见正面反馈、常见顾虑和 0–100 推荐参考分。前台卡片和列表以绿色、蓝绿色、橙色、红色区分推荐程度，详情页展示完整总结、分析时间和可点击来源。没有配置服务密钥时功能保持停用，不会生成模拟评分或虚构网评。

## 网评分析配置

密钥只通过部署目录下的私有 `.env` 提供，不写入网页、SQLite 或 Git。配置后重建或重启容器，再到后台“网评分析”逐个提交，或确认 API 费用后批量补齐。任务和结果保存在 SQLite；服务重启时，未完成任务会回到队列。

方案一使用带联网搜索工具的 OpenAI Responses API：

```dotenv
REVIEW_MODE=openai-web-search
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.example.com/v1
OPENAI_MODEL=gpt-5.6-luna
OPENAI_PROVIDER_NAME=OpenAI 兼容联网搜索
OPENAI_REASONING_EFFORT=max
OPENAI_MAX_OUTPUT_TOKENS=1200
OPENAI_USER_AGENT=Mozilla/5.0 (compatible; JobBoardReview/1.0)
# 只有 API 服务仍使用旧工具名时才设置为 web_search_preview
OPENAI_WEB_SEARCH_TOOL=web_search
```

后台批量分析每次最多加入 5 家。OpenAI Responses 返回的输入、输出、总 Token 和搜索次数会随分析结果保存，并在后台显示总 Token，便于核对用量。

方案二先由 Tavily 搜索，再交给任意支持 `/chat/completions` 和 JSON 输出的 OpenAI 兼容模型：

```dotenv
REVIEW_MODE=tavily-openai-compatible
TAVILY_API_KEY=...
TAVILY_BASE_URL=https://api.tavily.com
LLM_API_KEY=...
LLM_BASE_URL=https://your-provider.example/v1
LLM_MODEL=your-model-name
```

方案三使用阿里云百炼 Responses API 的内置联网搜索。`web_search` 负责多轮搜索并返回来源，模型负责总结和评分：

```dotenv
REVIEW_MODE=bailian-web-search
DASHSCOPE_API_KEY=...
BAILIAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
BAILIAN_MODEL=qwen3.8-flash
BAILIAN_ENABLE_THINKING=false
```

默认关闭思考模式并只挂载 `web_search`，以控制单家公司分析的延迟和 Token 消耗。百炼仍会以 agent 方式进行多轮搜索并返回可核验来源。

普通大模型 API 通常不能自行访问互联网。采用方案二时，搜索由 Tavily 完成；采用方案一时，搜索由模型 API 自带的 Web Search 工具完成。两种模式都由服务器后端直接调用，不要求本地 Agent 常驻。来源链接取自搜索接口的真实返回值，模型只负责归纳和评分。评分是公开网评的量化参考，可能存在样本偏差或时效问题，不代表事实定论。

### 宣讲会与面试录音

管理员可从详情或后台“录音管理”上传、替换和删除宣讲会或面试录音，并手动启动转写。录音由百炼文件转写模型处理，现有 OpenAI Responses 兼容模型负责分段提取事实并生成全文总结：

```env
ASR_DASHSCOPE_API_KEY=...
ASR_MODEL=qwen-audio-3.0-asr-flash-filetrans
ASR_PUBLIC_ORIGIN=http://YOUR_SERVER_IP
MAX_AUDIO_UPLOAD_MB=500
```

`ASR_PUBLIC_ORIGIN` 必须是百炼能够访问的本站地址，用于生成仅在任务期间有效的随机录音取件链接。服务会用 FFmpeg 生成单声道 ASR 副本并开启百炼说话人分离，原始上传文件保持不变；转写按“说话人 1、说话人 2……”组织连续对话。录音文件、原始转写和总结保存在 `data` 持久卷中。宣讲会总结可由管理员单独公开；面试录音、转写和复盘总结始终只有管理员可见。两类长录音使用各自的 Markdown 总结提示词，过滤无关声音并如实标明缺失或听不清的部分。

## 同步与数据说明

FastAPI 后台线程每 900 秒读取金山文档“招聘信息”和“宣讲会信息”。同步间隔从上次尝试结束计算，进度跨重启保存在 SQLite。每次同时检查两个表的版本和完整性，再通过数据库事务统一更新。失败时保留上次成功数据并在页面提示。浏览器每 15 分钟重新请求数据，返回浏览器标签页时也会刷新。

访客收藏保存在当前浏览器；管理员登录后使用服务器收藏，并按管理员账号在不同设备间同步。

原表需保持公开可读。适配器使用金山文档公开查看接口；上游协议、表头或分享权限发生变化时可能需要更新 `backend/source.py` 或 `backend/binvar.py`。更换源链接应使用同样的工作表和表头。同步不会修改原表。

时间均按北京时间解释。原表未填时间、截止日期、地点等信息时明确标记未注明。二维码及嵌入图片暂不转存，通过“查看原表”查阅。

## 管理员凭据

初始账号为 `admin`。随机初始密码保存在部署目录下的 `data/initial-admin.txt`（仅属主可读），不纳入源码。首次登录后请在“安全设置”修改密码。修改后所有管理会话失效，服务器初始凭据文件自动移除。

管理会话有效期 8 小时，Cookie 设置 Secure、HttpOnly、SameSite=Strict；密码采用 scrypt 哈希。后台写接口验证来源并限制登录尝试。

## 部署架构

Cloudflare 橙云 → AWS Nginx HTTPS → AWS FRPS `58112` → Windows FRPC → WSL `127.0.0.1:58112` → Docker FastAPI/SQLite。

Nginx 直接提供 `frontend/dist/assets` 静态资源，应用负责页面入口及 API。容器以非 root 用户运行，根文件系统只读；持久数据为 `data/jobs.sqlite3`。保持单 worker，避免重复启动同步线程。

AWS Nginx 配置挂载在 `/home/web/conf.d/job.dapanclaw.top.conf`，仓库中的 `deploy/nginx/job.dapanclaw.top.conf` 是可迁移模板。Cloudflare SSL/TLS 模式使用 **Full (strict)**，不要对 `/api/*` 配置强制缓存规则。

## 构建与发布

以下命令在 WSL 执行。前端由 Docker 多阶段构建自动编译，无需先在宿主机安装 Node 依赖。

```bash
cd /home/zhihongpan/services/dapan-job-board
docker compose build
docker compose -f compose.yaml -f compose.wsl.yaml up -d
docker compose -f compose.yaml -f compose.wsl.yaml ps
curl -fsS http://127.0.0.1:58112/api/health
curl -fsS https://job.dapanclaw.top/api/health
```

基础镜像、Debian 软件包和 Python 依赖使用官方 HTTPS 源，依赖分别在 `frontend/package-lock.json` 和 `backend/requirements.lock` 锁定。当前 WSL 的 Clash TUN 对 Docker bridge 的部分 fake-IP 回程不可用，因此私有 `.env` 设置 `BUILD_NETWORK=host`，运行时叠加 `compose.wsl.yaml`。覆盖文件使用 WSL host 网络访问外部服务，但 Uvicorn 仍只绑定 `127.0.0.1:58112`。

```bash
# 运行日志 / 重启
cd /home/zhihongpan/services/dapan-job-board
docker compose logs --tail=100 app
docker compose -f compose.yaml -f compose.wsl.yaml restart app

# 测试环境与后端回归
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.lock pytest httpx
.venv/bin/pytest tests -q
```

`scripts/production_check.py` 在初始密码尚未修改时可验证线上登录、后台读取和设置保存，不输出密码。它使用原设置执行一次保存，会留下操作审计。

## 备份与恢复

`dapan-job-board-backup.timer` 每日北京时间 03:20 左右执行 SQLite 在线一致性备份并检查完整性，保留最近 14 份，位置为项目目录下的 `backups`。备份脚本默认从自身路径识别项目根目录，也可用 `JOB_BOARD_ROOT` 指定。备份含密码哈希、设置和会话信息，应仅允许管理员读取。它是同机恢复副本，主机损坏时仍需另行保留异地备份。

```bash
python3 scripts/backup.py
systemctl list-timers dapan-job-board-backup.timer
```

恢复时先停容器，将整个现有 `data` 目录移到带时间戳的安全位置留作回退，新建 `data` 目录，把所选备份复制为 `data/jobs.sqlite3`，将目录及数据库属主设为 `10001:10001`，数据库权限设为 `600`，再启动容器。不要把备份覆盖到运行中的 SQLite 或残留 WAL 文件上。恢复后清空 `sessions` 表使旧会话失效，并在后台核对同步状态。

源码、配置与备份彼此独立：Git 不追踪 `data`、`backups`、诊断资料或密钥。首次排查同步问题可查看后台“数据同步”，原始诊断样本保留在服务器 `research` 目录，未公开发布。
