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
from openai import OpenAI

RSS_FEEDS = {
    "科技与AI": [
        "https://www.wired.com/feed/rss",
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://www.technologyreview.com/feed/",
        "http://feeds.arstechnica.com/arstechnica/index",
    ],
    "全球政治与地缘": [
        "https://www.reutersagency.com/feed/?best-topics=politics",
        "https://www.theguardian.com/world/rss",
        "https://www.politico.com/rss/politics08.xml",
    ],
    "中东/以色列": [
        "https://www.timesofisrael.com/feed/",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.middleeasteye.net/rss",
    ],
    "经济与金融": [
        "https://feeds.bloomberg.com/markets/news.rss",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    ],
    "军事和武器": [
        "https://www.defensenews.com/arc/outboundfeeds/rss/",
        "https://breakingdefense.com/feed/",
        "https://www.thedrive.com/the-war-zone/feed",
    ],
}

MAX_PER_FEED = int(os.getenv("MAX_PER_FEED", "6"))
MAX_PER_SECTION_INPUT = int(os.getenv("MAX_PER_SECTION_INPUT", "20"))
MAX_MATTERMOST_TEXT = int(os.getenv("MAX_MATTERMOST_TEXT", "12000"))
OUTPUT_HTML_PATH = os.getenv("OUTPUT_HTML_PATH", "output/newsletter.html")
SUMMARY_FILE_PATH = os.getenv("SUMMARY_FILE_PATH", "output/summary.md")

AI_API_KEY = os.getenv("AI_API_KEY")
AI_API_BASE = os.getenv("AI_API_BASE")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
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

    for section, urls in feeds.items():
        section_articles: list[dict[str, str]] = []
        for url in urls:
            parsed = feedparser.parse(url)
            if parsed.get("bozo"):
                print(f"[WARN] RSS 解析异常: {url}")

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

            if len(section_articles) >= MAX_PER_SECTION_INPUT:
                break
            time.sleep(0.12)

        articles_by_section[section] = section_articles
    return articles_by_section


def _build_prompt(articles_by_section: dict[str, list[dict[str, str]]]) -> str:
    prompt = [
        "你是资深国际新闻编辑，请将输入新闻整理为中文‘每日简报’。",
        "要求：",
        "1) 每个板块最多保留5条最重要新闻。",
        "2) 每条格式：- 标题｜来源域名｜核心内容（2句）｜影响（1句）",
        "3) 最后输出‘今日总结’：5条趋势判断。",
        "4) 避免夸张措辞，不确定信息要标注‘待进一步确认’。",
        "5) 输出必须是 Markdown，适配 Mattermost（禁止 HTML 标签）。",
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

    client = OpenAI(api_key=AI_API_KEY, base_url=AI_API_BASE)
    prompt = _build_prompt(articles_by_section)

    response = client.chat.completions.create(
        model=AI_MODEL,
        messages=[
            {
                "role": "system",
                "content": "你输出高质量中文国际新闻日报，结构清晰、客观克制、适合企业IM阅读。",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.35,
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("AI 返回为空")
    return content.strip()


def _build_html(newsletter_markdown: str, created_at: str) -> str:
    body = html.escape(newsletter_markdown)
    body = re.sub(r"^##\s+(.+)$", r"</pre><h2>\1</h2><pre>", body, flags=re.MULTILINE)
    body = body.replace("\n", "\n")

    return f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Daily Newsletter</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 0; background: #f5f7fb; color: #1f2937; }}
    .wrap {{ max-width: 920px; margin: 32px auto; padding: 0 16px; }}
    .card {{ background: #fff; border-radius: 12px; box-shadow: 0 8px 24px rgba(15,23,42,.08); padding: 24px; }}
    h1 {{ margin: 0 0 6px; font-size: 24px; }}
    .meta {{ color: #6b7280; margin-bottom: 18px; font-size: 14px; }}
    h2 {{ margin-top: 22px; margin-bottom: 10px; font-size: 18px; color: #111827; }}
    pre {{ white-space: pre-wrap; word-wrap: break-word; margin: 0; line-height: 1.6; font-size: 14px; }}
    .tip {{ margin-top: 16px; font-size: 12px; color: #6b7280; }}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"card\">
      <h1>每日新闻简报</h1>
      <div class=\"meta\">生成时间：{html.escape(created_at)}</div>
      <pre>{body}</pre>
      <div class=\"tip\">提示：Mattermost 端建议查看 Markdown 主文；HTML 适合浏览器阅读与归档。</div>
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