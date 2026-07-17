# -*- coding: utf-8 -*-
"""Scrapy Pipelines —— LLM 解析 → 校验 → MySQL 存储"""

import json
import time
import logging
from datetime import datetime

from kangyang.items import RawPageItem, ParsedItem

logger = logging.getLogger(__name__)


class LLMParsingPipeline:
    """
    Pipeline 1: LLM 语义解析
    将 RawPageItem 中的 HTML 提交给 LLM 解析层，生成 ParsedItem
    """

    def __init__(self):
        self.parser = None  # 延迟初始化

    @classmethod
    def from_crawler(cls, crawler):
        pipe = cls()
        pipe.crawler = crawler
        return pipe

    def _get_parser(self):
        if self.parser is None:
            import os
            from kangyang.llm.parser import LLMParser
            use_mock = os.environ.get("LLM_MOCK", "0") == "1"
            self.parser = LLMParser(use_mock=use_mock)
        return self.parser

    def process_item(self, item, spider):
        if not isinstance(item, RawPageItem):
            return item

        parser = self._get_parser()
        spider.logger.info(f"LLM 开始解析: {item['url']}")

        result = parser.parse(
            html=item["html"],
            schema_name=item.get("schema_name", "policy.json"),
        )

        parsed = ParsedItem()
        parsed["task_name"] = item["task_name"]
        parsed["url"] = item["url"]
        parsed["parsed_data"] = result.get("data", {})
        parsed["parse_status"] = result.get("status", "failed")
        parsed["retry_count"] = result.get("retry_count", 0)
        parsed["error_message"] = result.get("error", "")
        parsed["fetched_at"] = item.get("fetched_at", "")
        parsed["parsed_at"] = datetime.now().isoformat()

        if result["status"] == "success":
            spider.logger.info(f"LLM 解析成功: {item['url']}")
        else:
            spider.logger.warning(
                f"LLM 解析失败: {item['url']} - {result.get('error')}"
            )

        return parsed


class ValidationPipeline:
    """
    Pipeline 2: 字段校验
    检查 ParsedItem 中 parsed_data 的完整性和类型正确性
    """

    @classmethod
    def from_crawler(cls, crawler):
        return cls()

    def process_item(self, item, spider):
        if not isinstance(item, ParsedItem):
            return item

        from kangyang.validators.field_validator import FieldValidator

        validator = FieldValidator()
        schema_name = item.get("task_name", "policy.json")
        # 从 parsed_data 中能推断 schema，这里用默认
        valid, errors = validator.validate(
            data=item["parsed_data"],
            schema_name="policy.json",
        )

        # 记录校验结果
        if not valid:
            spider.logger.warning(
                f"校验不通过: {item['url']} — {'; '.join(errors)}"
            )
            # 标记为失败
            item["parse_status"] = "validation_failed"
            item["error_message"] = "; ".join(errors)

        return item


class MySQLPipeline:
    """
    Pipeline 3: MySQL 数据存储
    将校验通过的 ParsedItem 写入数据库
    """

    def __init__(self):
        self.store = None

    @classmethod
    def from_crawler(cls, crawler):
        pipe = cls()
        pipe.crawler = crawler
        return pipe

    def _get_store(self):
        if self.store is None:
            from kangyang.db.store import DataStore
            self.store = DataStore()
        return self.store

    def process_item(self, item, spider):
        if not isinstance(item, ParsedItem):
            return item

        # 只存储成功解析的数据
        if item["parse_status"] != "success":
            spider.logger.info(f"跳过存储（状态={item['parse_status']}）: {item['url']}")
            return item

        store = self._get_store()
        success = store.save(item["parsed_data"], item["url"])

        if success:
            spider.logger.info(f"数据已入库: {item['url']}")
        else:
            spider.logger.error(f"数据入库失败: {item['url']}")

        return item
