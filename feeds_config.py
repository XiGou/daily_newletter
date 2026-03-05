#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS 源配置文件
维护国际新闻 RSS 订阅源列表
"""

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
