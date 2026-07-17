# -*- coding: utf-8 -*-
"""Scrapy Item 定义"""

import scrapy


class RawPageItem(scrapy.Item):
    """待 LLM 解析的原始页面数据"""
    task_name = scrapy.Field()
    url = scrapy.Field()
    html = scrapy.Field()
    schema_name = scrapy.Field()
    fetched_at = scrapy.Field()


class ParsedItem(scrapy.Item):
    """LLM 解析后的结构化数据"""
    task_name = scrapy.Field()
    url = scrapy.Field()
    parsed_data = scrapy.Field()
    parse_status = scrapy.Field()
    retry_count = scrapy.Field()
    error_message = scrapy.Field()
    fetched_at = scrapy.Field()
    parsed_at = scrapy.Field()
