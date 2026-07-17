# -*- coding: utf-8 -*-
"""Scrapy 全局设置"""

import os
import sys

# ---- 项目路径 ----
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ---- 爬虫名称 ----
BOT_NAME = "kangyang"
SPIDER_MODULES = ["kangyang.spiders"]
NEWSPIDER_MODULE = "kangyang.spiders"

# ---- 基本行为 ----
ROBOTSTXT_OBEY = False
DOWNLOAD_DELAY = 2
RANDOMIZE_DOWNLOAD_DELAY = True
CONCURRENT_REQUESTS = 4
CONCURRENT_REQUESTS_PER_DOMAIN = 2
DOWNLOAD_TIMEOUT = 30

# ---- 重试 ----
RETRY_ENABLED = True
RETRY_TIMES = 3
RETRY_HTTP_CODES = [500, 502, 503, 504, 408, 429]

# ---- 缓存 ----
HTTPCACHE_ENABLED = False

# ---- 日志 ----
LOG_LEVEL = "INFO"
LOG_FILE = os.path.join(PROJECT_ROOT, "logs", "scrapy.log")
LOG_STDOUT = True

# ---- 用户代理 ----
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# ---- 下载中间件 ----
DOWNLOADER_MIDDLEWARES = {
    "kangyang.middlewares.KangyangDownloaderMiddleware": 543,
}

# ---- Item Pipeline ----
ITEM_PIPELINES = {
    "kangyang.pipelines.LLMParsingPipeline": 300,
    "kangyang.pipelines.ValidationPipeline": 400,
    "kangyang.pipelines.MySQLPipeline": 500,
}

# ---- 禁用不必要的 Pipeline ----
# （Scrapy 默认的一些 pipeline 不需要）
TELNETCONSOLE_ENABLED = False

# ---- Feed 导出（调试用） ----
FEEDS = {
    os.path.join(PROJECT_ROOT, "output", "results.json"): {
        "format": "json",
        "encoding": "utf8",
        "store_empty": False,
        "indent": 2,
        "overwrite": True,
    }
}
