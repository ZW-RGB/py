# -*- coding: utf-8 -*-
"""Scrapy 中间件 —— 负责请求分发与响应预处理"""


class KangyangDownloaderMiddleware:
    """下载中间件：可在此处对 Request / Response 做统一处理"""

    @classmethod
    def from_crawler(cls, crawler):
        return cls()

    def process_request(self, request, spider):
        """请求发出前处理（可添加自定义 Header 等）"""
        return None

    def process_response(self, request, response, spider):
        """响应回来后处理"""
        return response

    def process_exception(self, request, exception, spider):
        """下载异常时的降级处理"""
        spider.logger.error(f"下载异常: {request.url} - {exception}")
        return None
