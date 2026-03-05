# Daily Newsletter Bot

自动抓取 RSS 新闻，使用 AI 生成中文日报，通过 Mattermost Incoming Webhook 推送，并导出一份简洁 HTML 页面用于浏览器查看。

## 功能

- 多源 RSS 抓取（按板块聚合）
- AI 总结（Markdown 输出，适配 Mattermost）
- 自动截断超长消息，避免 Mattermost 长度问题
- 导出 `output/newsletter.html`（可作为归档或展示页）
- 自动部署 HTML 到 GitHub Pages，再将页面链接发到 Mattermost
- GitHub Actions 定时运行
- 每次文件名带日期时间戳，历史日报不会覆盖

## 目录

- `daily_newletter.py`：主程序
- `.github/workflows/newsletter.yml`：定时任务
- `pyproject.toml`：uv 依赖配置

## 环境变量

必填：

- `AI_API_KEY`：你的 LLM API Key
- `MATTERMOST_WEBHOOK_URL`：Mattermost Incoming Webhook URL

可选：

- `AI_API_BASE`：兼容 OpenAI 协议的网关地址
- `AI_MODEL`：模型名，默认 `gpt-4o-mini`
- `MATTERMOST_USERNAME`：发送者名字
- `MATTERMOST_ICON_URL`：发送者头像
- `OUTPUT_HTML_PATH`：HTML 输出路径，默认 `output/newsletter.html`
- `SUMMARY_FILE_PATH`：摘要中间文件路径，默认 `output/summary.md`
- `MAX_PER_FEED`：每个 RSS 源抓取条数，默认 `6`
- `MAX_PER_SECTION_INPUT`：每个板块送入 AI 的最大条数，默认 `20`
- `MAX_MATTERMOST_TEXT`：Mattermost 最大文本长度，默认 `12000`

## 本地运行

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh

uv sync

export AI_API_KEY="xxx"
export MATTERMOST_WEBHOOK_URL="https://mattermost.example/hooks/xxxx"
# 可选
# export AI_API_BASE="https://api.openai.com/v1"
# export AI_MODEL="gpt-4o-mini"

uv run python daily_newletter.py --mode all
```

执行后会：

1. 抓取 RSS
2. 生成日报 Markdown
3. 生成 `output/summary.md`
4. 生成 `output/newsletter.html`
5. 发到 Mattermost

## GitHub Actions 部署

将仓库推到 GitHub 后，在仓库 `Settings -> Secrets and variables -> Actions` 中配置：

必填 Secret：

- `AI_API_KEY`
- `MATTERMOST_WEBHOOK_URL`

建议 Secret：

- `AI_API_BASE`
- `AI_MODEL`
- `MATTERMOST_USERNAME`
- `MATTERMOST_ICON_URL`
- `NEWSLETTER_HTML_URL`

工作流文件：`.github/workflows/newsletter.yml`

- 每天 UTC `01:00` 自动执行（北京时间约 09:00）
- 也支持手动触发（`workflow_dispatch`）
- 执行顺序：`generate`（生成内容）→ `deploy`（部署 Pages）→ `notify`（发送 Mattermost）
- `notify` 会自动附带本次部署得到的 GitHub Pages 链接
- 每次会上传 `newsletter-html` 与 `newsletter-summary` Artifact
- Actions 内依赖安装使用 `uv sync`
- 页面文件命名为 `newsletter-YYYYMMDD-HHMMSS.html`，并同步更新 `latest.html`
- 部署到 `gh-pages` 分支时开启 `keep_files`，会保留历史文件

### 首次启用 GitHub Pages

1. 进入仓库 `Settings -> Pages`
2. `Source` 选择 `Deploy from a branch`
3. Branch 选择 `gh-pages`，目录选择 `/ (root)`
4. 保存后重新运行 workflow

## Mattermost 与 HTML 展示建议

- Mattermost 对 Markdown 支持最好，不建议直接发送 HTML。
- 最佳实践：
  - 主内容发送 Markdown（当前程序默认）
  - HTML 用于浏览器阅读（通过 GitHub Pages 自动发布）
  - Mattermost 消息附带本次 Pages URL，点击可查看完整样式页面
