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

# 如果 .env 文件存在，自动加载环境变量
load_dotenv()

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
ENABLE_AI_SEARCH = os.getenv("ENABLE_AI_SEARCH", "").lower() in ("1", "true", "yes")
MOCK_MODE = os.getenv("MOCK_MODE", "").lower() in ("1", "true", "yes")
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


def _generate_mock_summary() -> str:
    """生成假 summary 用于前端调试，不需要 AI API。"""
    return """# 每日简报 · 2026-03-05

## 科技与AI

- **OpenAI 发布 GPT-5 预览版** | openai.com | 新模型性能提升 40%，支持实时视频处理。主要应用于企业自动化。| 有望推动生成式 AI 商用落地，可能重塑行业格局。
- **Meta 开源 Llama 3.5** | meta.com | 开源社区获得高性能语言模型，参数规模 405B。社区反应积极，已有超过 100 万次下载。| 开源与商业模型角力加剧，可能加速 AI 民主化。
- **Google DeepMind AlphaFold 3 突破** | deepmind.com | 预测蛋白质结构准确率达 99%，支持药物设计加速。已与制药公司达成合作。| 生物医学应用前景广阔，可能加速新药开发。
- **微软 Azure AI 新增多模态支持** | microsoft.com | 云服务平台增强企业级 AI 能力，支持文本、图像、音频混合推理。| 利好云服务商，企业数字化转型提速。
- **英伟达发布 H200 高端芯片** | nvidia.com | GPU 性能再次翻倍，专为大规模语言模型优化。库存已被提前预定。| AI 芯片军备竞赛激烈，算力成为战略资源。

## 全球政治与地缘

- **美国宣布对中国新政策** | washington.gov | 贸易谈判新进展，双方代表将在瑞士重启磋商。| 贸易摩擦可能波及科技产业，市场关注度高。
- **欧盟通过数字市场法实施细则** | europa.eu | 科技巨头将面临更严监管，罚款可达营收 10%。| 欧洲监管权力加强，全球科技治理重构。
- **日本宣布与印度深化防务合作** | mofa.go.jp | 两国签署防务协议，计划联合军演。背景是印太地区形势复杂化。| 印太战略联盟强化，可能改变亚洲力量格局。
- **俄乌冲突胶着状态持续** | un.org | 双方炮火不减，人道危机加剧。国际和平谈判无进展。| 冲突长期化明显，难民潮或持续。
- **中东局势仍存不确定性** | bbc.com | 以色列-巴勒斯坦停火谈判再次中断。国际调解前景渺茫。| 地区紧张局势难以缓解，油价可能承压。

## 经济与金融

- **美联储维持基准利率** | federalreserve.gov | 利率保持在 4.5%，释放观望信号。市场预期年内可能两次降息。| 金融市场波动可能加大，风险资产承压。
- **欧洲央行面临增长放缓压力** | ecb.europa.eu | 经济数据疲弱，降息呼声高涨。下月会议可能调整政策。| 欧元承压，跨国出口竞争力下降。
- **中国三月制造业 PMI 下滑** | stats.gov.cn | 制造业景气度有所回落，经济下行压力持续显现。| 消费需求疲软，外汇压力可能增加。
- **比特币突破历史新高** | coinmarketcap.com | BTC 价格逼近 100K 美元，机构投资增加。市场情绪偏乐观。| 加密货币风险资产属性突出，波动可能加大。
- **国际油价因地缘波动** | iea.org | 布伦特原油维持 75-80 美元区间。中东紧张局势是主要推手。| 能源成本对全球通胀影响持续，企业利润承压。

## 军事和武器

- **美国增加印太军事部署** | defense.gov | 第三舰队派遣增强编队，驻扎新加坡和日本。| 区域军事力量对比变化，中美竞争加剧。
- **北约宣布新武器援助计划** | nato.int | 向乌克兰交付防空系统和无人机。美国领导援助总额超 200 亿。| 冲突升级成本上升，战局可能延长。
- **台湾购入新一代防空导弹** | mnd.gov.tw | 获得 8 套爱国者系统，合同金额 16 亿美元。称为防卫自主努力。| 两岸军力对比微调，区域紧张升温。
- **日本自卫队演习规模创新高** | mod.go.jp | 参与 NATO 联合演习，参与国超 20 个。展示日本军事存在感。| 日本战略地位上升，亚太安全格局微调。
- **俄罗斯试射新型导弹系统** | tass.com | 宣称新系统精准度提升，射程逾 3000 公里。引发西方关注。| 俄方军事现代化持续，冷战氛围加重。

## 今日总结

1. **AI 产业热度不减**：多家科技巨头推新品，市场竞争白热化，算力成瓶颈。
2. **地缘政治复杂化**：美中贸易纠纷、中东局势、印太军事紧张交织，全球秩序重构。
3. **经济增长放缓信号**：央行谨慎、制造业疲软、金融市场波动，通胀压力难消。
4. **能源与安全挂钩**：油价受地缘影响，国防支出继续增加，国家竞争激烈。
5. **技术与政治纠缠**：监管趋严、供应链调整、科技民族主义升温，商业环境复杂。"""


def generate_ai_summary(articles_by_section: dict[str, list[dict[str, str]]]) -> str:
    if MOCK_MODE:
        print("[DEBUG] 使用 MOCK_MODE 直接返回假 summary")
        return _generate_mock_summary()

    if not AI_API_KEY:
        raise ValueError("AI_API_KEY 未设置")

    client = OpenAI(api_key=AI_API_KEY, base_url=AI_API_BASE)
    prompt = _build_prompt(articles_by_section)

    # 构建系统提示词
    system_content = "你输出高质量中文国际新闻日报，结构清晰、客观克制、适合企业IM阅读。"

    # 检测是否使用 Grok
    is_grok = (
        AI_API_BASE and "x.ai" in AI_API_BASE.lower()
    ) or (
        AI_MODEL and "grok" in AI_MODEL.lower()
    )

    # 如果是 Grok 且启用搜索功能，使用官方推荐的 web_search 方式
    if is_grok and ENABLE_AI_SEARCH:
        print("[INFO] 使用 Grok web_search 功能进行实时信息增强")
        system_content += "\n\n你可以使用实时搜索功能来：1) 验证新闻准确性；2) 补充背景信息；3) 交叉验证关键事件；4) 获取最新进展。请结合搜索结果和输入新闻综合分析。"

        response = client.responses.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": prompt},
            ],
            tools=[{
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the web for real-time information"
                }
            }],
            temperature=0.35,
        )
    else:
        # 标准 OpenAI 兼容调用
        if ENABLE_AI_SEARCH and not is_grok:
            print("[INFO] AI搜索功能已启用，但当前模型非 Grok，使用标准提示词")
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
    """轻量级Markdown渲染，支持加粗、标题、列表"""
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
            # 处理加粗 **text**
            content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', content)
            content = html.escape(content).replace("&lt;strong&gt;", "<strong>").replace("&lt;/strong&gt;", "</strong>")
            html_lines.append(f"<li>{content}</li>")
            continue

        # 数字列表
        if re.match(r'^\d+\.\s', stripped):
            content = re.sub(r'^\d+\.\s+', '', stripped)
            content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', content)
            content = html.escape(content).replace("&lt;strong&gt;", "<strong>").replace("&lt;/strong&gt;", "</strong>")
            html_lines.append(f'<div class="summary-item">{content}</div>')
            continue

        # 普通段落
        if in_list:
            html_lines.append("</ul>")
            in_list = False
        content = html.escape(stripped)
        content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', content)
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