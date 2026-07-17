# -*- coding: utf-8 -*-
"""
康养平台自适应爬虫

核心思路：
  不编写任何 XPath / CSS 选择器。
  页面下载后直接将 HTML 原文交给 LLM 做语义解析，
  页面改版后 LLM 自动重新适配，零代码维护。

两种工作模式：
  1. 单页模式（默认）：直接对 --url 指定的页面做 LLM 解析
  2. 跟随模式（--follow）：列表页只发现链接，详情页才交给 LLM 解析
     - 自动过滤同域名链接，避免跑偏
     - 可用 --detail-pattern 指定详情页 URL 正则，精确控制哪些页面需要解析
"""

import re
import time
import logging
from urllib.parse import urlparse

import scrapy

from kangyang.items import RawPageItem

logger = logging.getLogger(__name__)


class KangyangSpider(scrapy.Spider):
    name = "kangyang"
    allowed_domains = []  # 运行时动态设置

    custom_settings = {
        "CLOSESPIDER_ITEMCOUNT": 0,
        "CLOSESPIDER_PAGECOUNT": 0,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.start_urls = kwargs.get("start_urls", [])
        self.task_name = kwargs.get("task_name", "康养机构采集")
        self.schema_name = kwargs.get("schema_name", "institution.json")

        # ---- 跟随模式参数 ----
        self.follow = kwargs.get("follow", False)
        # 详情页 URL 正则（如果指定了，只有匹配的 URL 才交给 LLM 解析）
        self.detail_pattern = kwargs.get("detail_pattern", None)
        # 最大抓取详情页数量（防止无限爬取）
        self.max_items = int(kwargs.get("max_items", 50))

        # 从 start_urls 中提取 allowed_domains
        if self.start_urls:
            domains = set()
            for url in self.start_urls:
                parsed = urlparse(url)
                if parsed.hostname:
                    domains.add(parsed.hostname)
            self.allowed_domains = list(domains)

        # 已处理的详情页计数
        self._detail_count = 0
        # 已入队的 URL 集合（去重）
        self._seen_urls = set()

    @classmethod
    def configure(
        cls,
        start_urls: list,
        task_name: str = "康养机构采集",
        schema_name: str = "institution.json",
        allowed_domains: list = None,
        follow: bool = False,
        detail_pattern: str = None,
        max_items: int = 50,
    ):
        params = {
            "start_urls": start_urls,
            "task_name": task_name,
            "schema_name": schema_name,
            "follow": follow,
            "detail_pattern": detail_pattern,
            "max_items": max_items,
        }
        if allowed_domains:
            params["allowed_domains"] = allowed_domains
        return params

    def start_requests(self):
        if not self.start_urls:
            self.logger.error("未配置 start_urls，请通过 --url 或 --urls 传入")
            return

        for url in self.start_urls:
            self._seen_urls.add(url)
            yield scrapy.Request(
                url=url,
                callback=self.parse,
                errback=self.on_error,
                dont_filter=True,
            )

    def parse(self, response):
        """
        核心解析方法：
          - 跟随模式 OFF：直接将当前页 HTML 交给 Pipeline
          - 跟随模式 ON：
              - 如果是列表页（start_url），只发现链接不解析
              - 如果是详情页（匹配 detail_pattern 或非 start_url），交给 LLM 解析
        """
        is_start_url = response.url in self.start_urls
        is_detail = self._is_detail_page(response.url, is_start_url)

        if is_detail:
            # ---- 详情页：交给 LLM 解析 ----
            self._detail_count += 1
            self.logger.info(
                f"详情页 [{self._detail_count}/{self.max_items}]: {response.url} ({len(response.text)} 字符)"
            )

            item = RawPageItem()
            item["task_name"] = self.task_name
            item["url"] = response.url
            item["html"] = response.text
            item["schema_name"] = self.schema_name
            item["fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

            yield item
        else:
            # ---- 列表页：只发现链接 ----
            self.logger.info(f"列表页，发现链接中: {response.url}")

        # ---- 跟随模式：从当前页面发现详情页链接 ----
        if self.follow and self._detail_count < self.max_items:
            links_found = 0
            for link in response.css("a::attr(href)").getall():
                full_url = response.urljoin(link)

                # 域名过滤：只爬同域名
                if not self._is_same_domain(full_url):
                    continue

                # 去重
                if full_url in self._seen_urls:
                    continue
                self._seen_urls.add(full_url)

                # 如果指定了 detail_pattern，只入队匹配的链接
                # 否则入队所有非 start_url 的链接
                is_link_detail = self._is_detail_page(full_url, is_start_url=False)

                if is_link_detail or (not self.detail_pattern and not self._is_same_as_start(full_url)):
                    links_found += 1
                    yield scrapy.Request(
                        url=full_url,
                        callback=self.parse,
                        errback=self.on_error,
                    )

                    if self._detail_count >= self.max_items:
                        self.logger.info(f"已达到最大采集数 {self.max_items}，停止发现新链接")
                        break

            if links_found:
                self.logger.info(f"从 {response.url} 发现 {links_found} 个新链接")

    def _is_detail_page(self, url: str, is_start_url: bool) -> bool:
        """判断 URL 是否是详情页"""
        # start_url 本身不是详情页
        if is_start_url:
            return False

        # 如果指定了 detail_pattern，用正则判断
        if self.detail_pattern:
            return bool(re.search(self.detail_pattern, url))

        # 没有指定 pattern 时，默认非 start_url 的页面都是详情页
        return True

    def _is_same_domain(self, url: str) -> bool:
        """检查 URL 是否属于 allowed_domains"""
        parsed = urlparse(url)
        if not parsed.hostname:
            return False
        if not self.allowed_domains:
            return True
        return parsed.hostname in self.allowed_domains

    def _is_same_as_start(self, url: str) -> bool:
        """检查 URL 是否是 start_url 之一"""
        return url in self.start_urls

    def on_error(self, failure):
        self.logger.error(f"请求失败: {failure.request.url} - {failure.value}")
