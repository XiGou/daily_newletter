#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import html
import argparse
import os
import re
import time
from datetime import datetime, timezone, timedelta

import feedparser
import requests
from dotenv import load_dotenv
from openai import OpenAI

from feeds_config import RSS_FEEDS
from mock_templates import get_mock_summary, get_mock_articles

# 如果 .env 文件存在，自动加载环境变量
load_dotenv()

MAX_PER_FEED = int(os.getenv("MAX_PER_FEED", "6"))
MAX_PER_SECTION_INPUT = int(os.getenv("MAX_PER_SECTION_INPUT", "20"))
MAX_MATTERMOST_TEXT = int(os.getenv("MAX_MATTERMOST_TEXT", "12000"))
OUTPUT_HTML_PATH = os.getenv("OUTPUT_HTML_PATH", "output/newsletter.html")
SUMMARY_FILE_PATH = os.getenv("SUMMARY_FILE_PATH", "output/summary.md")

AI_API_KEY = os.getenv("AI_API_KEY")
AI_API_BASE = os.getenv("AI_API_BASE")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
ENABLE_AI_SEARCH = os.getenv("ENABLE_AI_SEARCH", "").lower() in ("1", "true", "yes")
SKIP_RSS_FETCH = os.getenv("SKIP_RSS_FETCH", "").lower() in ("1", "true", "yes")
MOCK_MODE = os.getenv("MOCK_MODE", "").lower()  # 模式: "full"(假数据+假总结), "articles"(假数据调真AI), "0"或无(正常流程)
MATTERMOST_WEBHOOK_URL = os.getenv("MATTERMOST_WEBHOOK_URL")
MATTERMOST_USERNAME = os.getenv("MATTERMOST_USERNAME", "Daily Newsletter Bot")
MATTERMOST_ICON_URL = os.getenv("MATTERMOST_ICON_URL", "")
NEWSLETTER_HTML_URL = os.getenv("NEWSLETTER_HTML_URL", "")


def _clean_text(value: str, max_len: int = 320) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


def fetch_rss_articles(feeds: dict[str, list[str]]) -> dict[str, list[dict[str, str]]]:
    articles_by_section: dict[str, list[dict[str, str]]] = {}
    seen_links: set[str] = set()

    total_feeds = sum(len(urls) for urls in feeds.values())
    success_count = 0
    fail_count = 0

    print(f"  开始抓取 {total_feeds} 个 RSS 源...")
    print()

    for section, urls in feeds.items():
        print(f"  [{section}]")
        section_articles: list[dict[str, str]] = []

        for url in urls:
            try:
                parsed = feedparser.parse(url)
                feed_domain = url.split("/")[2] if "//" in url else url

                if parsed.get("bozo") or not parsed.entries:
                    reason = str(parsed.get("bozo_exception", "无条目"))[:50] if parsed.get("bozo") else "无条目"
                    print(f"    ✗ {url}")
                    print(f"      失败原因: {reason}")
                    fail_count += 1
                    continue

                articles_before = len(section_articles)
                for entry in parsed.entries[:MAX_PER_FEED]:
                    title = _clean_text(getattr(entry, "title", ""), max_len=180)
                    link = getattr(entry, "link", "").strip()
                    summary = _clean_text(
                        getattr(entry, "summary", "") or getattr(entry, "description", ""),
                        max_len=320,
                    )

                    if not title or not link or link in seen_links:
                        continue

                    seen_links.add(link)
                    section_articles.append(
                        {
                            "title": title,
                            "link": link,
                            "summary": summary,
                        }
                    )

                    if len(section_articles) >= MAX_PER_SECTION_INPUT:
                        break

                articles_fetched = len(section_articles) - articles_before
                print(f"    ✓ {feed_domain} - 成功 ({articles_fetched} 条)")
                success_count += 1

                if len(section_articles) >= MAX_PER_SECTION_INPUT:
                    break
                time.sleep(0.12)

            except Exception as e:
                print(f"    ✗ {url}")
                print(f"      异常: {str(e)[:80]}")
                fail_count += 1

        print(f"    小计: {len(section_articles)} 条新闻")
        print()
        articles_by_section[section] = section_articles

    print(f"  抓取完成: {success_count} 成功, {fail_count} 失败, 共 {sum(len(v) for v in articles_by_section.values())} 条新闻")
    return articles_by_section


def _build_prompt(articles_by_section: dict[str, list[dict[str, str]]]) -> str:
    prompt = [
        "你是资深国际新闻编辑，请以输入新闻为基础整理为中文‘每日简报’。",
        "要求：",
        "1) 每个板块最多保留5条最重要新闻，由你依据新闻本身的重要性来判断。",
        "分四个板块，每板块科技与AI、全球政治与地缘、中东/以色列、经济与金融、军事和武器",
        "2) 每条格式：- 标题｜[🔗](URL)｜核心内容（2句）｜影响（1句）",
        "3) 最后输出'今日总结'：5条趋势判断。",
        "4) 避免夸张措辞，不确定信息要标注'待进一步确认'。",
        "5) 输出必须是纯 Markdown（禁止任何 HTML 标签，所有链接用 [符号](url) 格式表示）。",
        "6) 输出内容必须基于输入新闻，不能凭空捏造信息。",
        "7) 请使用模型内置 websearch 工具来在你认为有价值的来源进行搜索更多的新闻， 不要局限于输入的新闻，"
        "将搜索到的结果与输入的新闻结合进行分析验证新闻的准确性，补充背景信息，交叉验证关键事件，获取最新进展等，"
        "以提升简报的质量和时效性。",
        "8) 如果确信我提供的新闻是不准确的或者过时的，请忽略并用 websearch 工具搜索最新信息，不要基于错误信息进行分析。",
        "",
        "新闻输入：",
    ]

    for section, articles in articles_by_section.items():
        prompt.append(f"\n## {section}")
        if not articles:
            prompt.append("- 无有效新闻")
            continue
        for item in articles:
            source = item["link"].split("/")[2] if "//" in item["link"] else "unknown"
            prompt.append(f"- {item['title']} | {source} | {item['link']} | {item['summary']}")

    return "\n".join(prompt)


def generate_ai_summary(articles_by_section: dict[str, list[dict[str, str]]]) -> str:
    if not AI_API_KEY:
        raise ValueError("AI_API_KEY 未设置")

    prompt = _build_prompt(articles_by_section)

    # 构建系统提示词
    system_content = "你输出高质量中文国际新闻日报，结构清晰、客观克制、适合企业IM阅读。"

    # 检测是否使用 Grok
    is_grok = (
        AI_API_BASE and "x.ai" in AI_API_BASE.lower()
    ) or (
        AI_MODEL and "grok" in AI_MODEL.lower()
    )

    client = OpenAI(api_key=AI_API_KEY, base_url=AI_API_BASE)

    # 如果是 Grok，使用 OpenAI SDK 的 responses.create() 和 web_search 工具
    if is_grok:
        tools = []
        if ENABLE_AI_SEARCH:
            print("[INFO] 启用 Grok web_search 工具进行实时信息增强")
            tools.append({"type": "web_search"})
            system_content += "\n\n你可以使用搜索工具来：1) 验证新闻准确性；2) 补充背景信息；3) 交叉验证关键事件；4) 获取最新进展。"

        response = client.responses.create(
            model=AI_MODEL,
            input=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": prompt},
            ],
            tools=tools,
            temperature=0.35,
        )
        content = response.output_text
        if not content:
            raise RuntimeError("Grok 返回为空")
        return content.strip()
    else:
        # 其他 OpenAI 兼容模型，使用 chat.completions.create()
        if ENABLE_AI_SEARCH:
            print("[INFO] AI搜索功能已启用，使用增强型提示词")
            system_content += "\n\n请结合你的知识库和输入新闻进行综合分析。"

        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": prompt},
            ],
            temperature=0.35,
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("AI 返回为空")
        return content.strip()



def _markdown_to_html(md: str) -> str:
    """轻量级Markdown渲染，支持加粗、标题、列表、链接"""

    def _escape_and_format(text: str) -> str:
        """转义HTML，处理加粗、链接和脚注引用"""
        # 1. 提取所有 markdown 链接，用占位符替换（防止双重转义）
        links = []
        def store_link(match):
            link_text = match.group(1)
            link_url = match.group(2)
            # 链接内容单独 escape
            link_html = f'<a href="{html.escape(link_url)}" target="_blank">{html.escape(link_text)}</a>'
            links.append(link_html)
            return f"__LINK_{len(links)-1}__"

        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', store_link, text)

        # 1b. 提取所有 [[数字]](url) 脚注格式，渲染为上标链接
        citations = []
        def store_citation(match):
            citation_num = match.group(1)
            citation_url = match.group(2)
            # 渲染为 <sup> 上标脚注链接
            citation_html = f'<sup><a href="{html.escape(citation_url)}" target="_blank" class="citation">[{html.escape(citation_num)}]</a></sup>'
            citations.append(citation_html)
            return f"__CITATION_{len(citations)-1}__"

        text = re.sub(r'\[\[([^\]]+)\]\]\(([^)]+)\)', store_citation, text)

        # 2. HTML转义其余文本
        text = html.escape(text)

        # 3. 处理加粗 **text**
        text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
        text = text.replace("&lt;strong&gt;", "<strong>").replace("&lt;/strong&gt;", "</strong>")

        # 4. 恢复所有链接占位符
        for i, link_html in enumerate(links):
            text = text.replace(f"__LINK_{i}__", link_html)

        # 4b. 恢复所有脚注占位符
        for i, citation_html in enumerate(citations):
            text = text.replace(f"__CITATION_{i}__", citation_html)

        return text

    lines = md.split("\n")
    html_lines = []
    in_list = False
    in_section = False

    for line in lines:
        stripped = line.strip()

        # 空行
        if not stripped:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            if in_section:
                html_lines.append("</section>")
                in_section = False
            continue

        # ## 二级标题（板块标题）
        if stripped.startswith("## "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            if in_section:
                html_lines.append("</section>")
            section_title = html.escape(stripped[3:].strip())
            html_lines.append(f'<section class="section"><h2>{section_title}</h2>')
            in_section = True
            continue

        # # 一级标题
        if stripped.startswith("# "):
            title = html.escape(stripped[2:].strip())
            html_lines.append(f'<div class="doc-subtitle">{title}</div>')
            continue

        # - 列表项
        if stripped.startswith("- "):
            if not in_list:
                html_lines.append('<ul>')
                in_list = True
            content = stripped[2:].strip()
            content = _escape_and_format(content)
            html_lines.append(f"<li>{content}</li>")
            continue

        # 数字列表
        if re.match(r'^\d+\.\s', stripped):
            content = re.sub(r'^\d+\.\s+', '', stripped)
            content = _escape_and_format(content)
            html_lines.append(f'<div class="summary-item">{content}</div>')
            continue

        # 普通段落
        if in_list:
            html_lines.append("</ul>")
            in_list = False
        content = _escape_and_format(stripped)
        html_lines.append(f"<p>{content}</p>")

    if in_list:
        html_lines.append("</ul>")
    if in_section:
        html_lines.append("</section>")

    return "\n".join(html_lines)


def _build_html(newsletter_markdown: str, created_at: str) -> str:
    body_html = _markdown_to_html(newsletter_markdown)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>每日新闻简报 - Daily Intelligence Brief</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: 'Times New Roman', Times, Georgia, serif;
      font-size: 14px;
      line-height: 1.65;
      color: #1a1a1a;
      background: #f8f9fa;
      margin: 0;
      padding: 20px;
    }}

    .document {{
      max-width: 900px;
      margin: 0 auto;
      background: #ffffff;
      box-shadow: 0 2px 8px rgba(0,0,0,0.1);
      border: 1px solid #d0d0d0;
    }}

    .doc-header {{
      background: linear-gradient(135deg, #1e3a5f 0%, #2c5282 100%);
      color: #ffffff;
      padding: 32px 40px;
      border-bottom: 4px solid #c9a961;
    }}

    .doc-title-main {{
      font-size: 28px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 1.5px;
      margin-bottom: 8px;
      font-family: Arial, sans-serif;
    }}

    .doc-subtitle {{
      font-size: 16px;
      font-weight: 400;
      opacity: 0.95;
      margin-top: 12px;
      padding-top: 12px;
      border-top: 1px solid rgba(255,255,255,0.3);
    }}

    .doc-meta {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 16px 40px;
      background: #f0f4f8;
      border-bottom: 2px solid #d0d7de;
      font-size: 12px;
    }}

    .doc-meta-item {{
      display: flex;
      align-items: center;
      gap: 6px;
      color: #57606a;
      font-family: Arial, sans-serif;
    }}

    .doc-meta-label {{
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}

    .doc-content {{
      padding: 40px 40px 48px 40px;
    }}

    .section {{
      margin-bottom: 36px;
      page-break-inside: avoid;
    }}

    .section h2 {{
      font-size: 16px;
      font-weight: 700;
      color: #1e3a5f;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      padding: 10px 16px;
      background: #e8eef5;
      border-left: 4px solid #2c5282;
      margin-bottom: 20px;
      font-family: Arial, sans-serif;
    }}

    .section ul {{
      list-style: none;
      margin: 0;
      padding: 0;
    }}

    .section li {{
      margin-bottom: 18px;
      padding-left: 24px;
      position: relative;
      line-height: 1.7;
      color: #24292f;
    }}

    .section li:before {{
      content: "\u25a0";
      position: absolute;
      left: 4px;
      color: #2c5282;
      font-size: 10px;
      top: 4px;
    }}

    .section li strong {{
      color: #1a1a1a;
      font-weight: 700;
    }}

    .summary-item {{
      padding: 12px 16px;
      margin-bottom: 12px;
      background: #fffbf0;
      border-left: 3px solid #c9a961;
      line-height: 1.65;
      color: #24292f;
    }}

    .summary-item strong {{
      color: #1a1a1a;
      font-weight: 700;
    }}

    .citation {{
      color: #8899aa;
      font-size: 0.85em;
      text-decoration: none;
      transition: color 0.2s;
    }}

    .citation:hover {{
      color: #2c5282;
      text-decoration: underline;
    }}

    .doc-footer {{
      padding: 24px 40px;
      background: #f6f8fa;
      border-top: 2px solid #d0d7de;
      font-size: 11px;
      color: #656d76;
      text-align: center;
      line-height: 1.6;
      font-family: Arial, sans-serif;
    }}

    @media print {{
      body {{ background: white; padding: 0; }}
      .document {{ box-shadow: none; border: none; }}
      .section {{ page-break-inside: avoid; }}
    }}

    @media (max-width: 768px) {{
      body {{ padding: 0; }}
      .document {{ border: none; }}
      .doc-header {{ padding: 24px 20px; }}
      .doc-title-main {{ font-size: 22px; }}
      .doc-meta {{ flex-direction: column; gap: 8px; align-items: flex-start; padding: 12px 20px; }}
      .doc-content {{ padding: 24px 20px; }}
      .section h2 {{ font-size: 14px; padding: 8px 12px; }}
      .section li {{ font-size: 13px; }}
    }}
  </style>
</head>
<body>
  <div class="document">
    <div class="doc-header">
      <div class="doc-title-main">Daily Intelligence Brief</div>
      <div class="doc-subtitle">每日新闻简报</div>
    </div>

    <div class="doc-meta">
      <div class="doc-meta-item">
        <span class="doc-meta-label">Date:</span>
        <span>{html.escape(created_at.split()[0])}</span>
      </div>
      <div class="doc-meta-item">
        <span class="doc-meta-label">Classification:</span>
        <span>UNCLASSIFIED</span>
      </div>
      <div class="doc-meta-item">
        <span class="doc-meta-label">Distribution:</span>
        <span>INTERNAL USE</span>
      </div>
    </div>

    <div class="doc-content">
      {body_html}
    </div>

    <div class="doc-footer">
      This brief is generated using AI-assisted analysis of international news sources.<br>
      Information is provided for reference purposes only and does not represent any official position.
    </div>
  </div>
</body>
</html>
"""


def write_html(newsletter_markdown: str, path: str) -> str:
    created_at = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S %Z")
    html_content = _build_html(newsletter_markdown, created_at)

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(path, "w", encoding="utf-8") as file:
        file.write(html_content)
    return path


def write_summary_markdown(summary_markdown: str, path: str) -> str:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(path, "w", encoding="utf-8") as file:
        file.write(summary_markdown)
    return path


def read_summary_markdown(path: str) -> str:
    with open(path, "r", encoding="utf-8") as file:
        return file.read().strip()


def _trim_for_mattermost(text: str) -> str:
    if len(text) <= MAX_MATTERMOST_TEXT:
        return text
    truncated = text[: MAX_MATTERMOST_TEXT - 80].rstrip()
    return f"{truncated}\n\n---\n内容过长已截断，请查看 HTML 全文。"


def send_to_mattermost(summary_markdown: str) -> None:
    if not MATTERMOST_WEBHOOK_URL:
        raise ValueError("MATTERMOST_WEBHOOK_URL 未设置")

    text = _trim_for_mattermost(summary_markdown)
    if NEWSLETTER_HTML_URL:
        text += f"\n\n📎 HTML 预览：{NEWSLETTER_HTML_URL}"

    payload: dict[str, object] = {
        "username": MATTERMOST_USERNAME,
        "text": text,
    }
    if MATTERMOST_ICON_URL:
        payload["icon_url"] = MATTERMOST_ICON_URL

    response = requests.post(MATTERMOST_WEBHOOK_URL, json=payload, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f"Webhook 发送失败: {response.status_code} {response.text}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daily newsletter pipeline")
    parser.add_argument(
        "--mode",
        choices=["all", "generate", "send"],
        default="all",
        help="all=生成并发送；generate=仅生成HTML和summary；send=仅发送已有summary",
    )
    parser.add_argument(
        "--summary-file",
        default=SUMMARY_FILE_PATH,
        help="summary markdown 文件路径",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.mode == "send":
        print("[send] 读取 summary 并发送 Mattermost ...")
        summary_md = read_summary_markdown(args.summary_file)
        if not summary_md:
            raise RuntimeError("summary 文件为空，无法发送")
        send_to_mattermost(summary_md)
        print("发送完成 ✅")
        return

    # 完全模拟模式：跳过所有数据获取和 AI 调用，直接返回假日报
    if MOCK_MODE == "full":
        print("[DEBUG] MOCK_MODE=full，使用完全模拟数据（跳过 RSS 和 AI 调用）")
        summary_md = get_mock_summary()
        print("[1/2] 已加载模拟日报")

        print("[2/2] 导出 HTML ...")
        html_path = write_html(summary_md, OUTPUT_HTML_PATH)
        print(f"HTML 已生成: {html_path}")

        if args.mode == "generate":
            print("生成完成 ✅")
            return

        print("[3/3] 发送 Mattermost ...")
        send_to_mattermost(summary_md)
        print("完成 ✅")
        return

    # Markdown-only 模式：读取已有 markdown 文件，直接转 HTML（测试渲染流程）
    if MOCK_MODE == "md_only":
        print("[DEBUG] MOCK_MODE=md_only，读取已有 markdown 文件并测试转 HTML")
        try:
            summary_md = read_summary_markdown(args.summary_file)
            print(f"[1/2] 已读取 markdown: {args.summary_file}")
        except FileNotFoundError:
            raise RuntimeError(f"markdown 文件不存在: {args.summary_file}")

        print("[2/2] 导出 HTML ...")
        html_path = write_html(summary_md, OUTPUT_HTML_PATH)
        print(f"HTML 已生成: {html_path}")

        if args.mode == "generate":
            print("生成完成 ✅")
            return

        print("[3/3] 发送 Mattermost ...")
        send_to_mattermost(summary_md)
        print("完成 ✅")
        return

    # 根据 MOCK_MODE 选择数据源
    if MOCK_MODE == "articles":
        print("[1/4] 使用模拟文章数据（MOCK_MODE=articles）...")
        articles = get_mock_articles()
        print(f"已加载 {sum(len(v) for v in articles.values())} 篇模拟文章用于测试 AI 调用")
    elif SKIP_RSS_FETCH:
        print("[1/4] 已跳过 RSS 抓取（SKIP_RSS_FETCH=1），仅使用 AI 生成日报...")
        articles = {section: [] for section in RSS_FEEDS}
    else:
        print("[1/4] 抓取 RSS ...")
        articles = fetch_rss_articles(RSS_FEEDS)

    print("[2/4] 生成 AI 简报 ...")
    summary_md = generate_ai_summary(articles)

    print("[3/5] 保存 summary ...")
    summary_path = write_summary_markdown(summary_md, args.summary_file)
    print(f"summary 已保存: {summary_path}")

    print("[4/5] 导出 HTML ...")
    html_path = write_html(summary_md, OUTPUT_HTML_PATH)
    print(f"HTML 已生成: {html_path}")

    if args.mode == "generate":
        print("生成完成 ✅")
        return

    print("[5/5] 发送 Mattermost ...")
    send_to_mattermost(summary_md)
    print("完成 ✅")


if __name__ == "__main__":
    main()