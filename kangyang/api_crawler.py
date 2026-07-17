# -*- coding: utf-8 -*-
"""
API 模式爬虫 —— 直接调后端接口采集数据

工作流程：
  登录 → 获取菜单/路由表 → 发现所有数据表 → 逐个拉取分页数据 → LLM 语义增强 → 存储
"""
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from kangyang.api_client import RuoYiApiClient
from kangyang.llm.parser import LLMParser
from kangyang.config import get_endpoints

logger = logging.getLogger(__name__)


# ── API 端点：从 YAML 配置文件加载，修改 YAML 无需动代码 ──
# 热更新: 调用 kangyang.config.reload_config() 或 API /api/config/reload
KNOWN_ENDPOINTS = get_endpoints()


@dataclass
class CrawlResult:
    endpoint: str
    module: str
    table_name: str
    total_count: int
    collected_count: int
    success: bool
    error_msg: str = ""
    sample_record: dict = field(default_factory=dict)
    columns: list = field(default_factory=list)
    records: list = field(default_factory=list)  # 全部记录


class ApiCrawler:
    """API 直调爬虫 —— 不碰 HTML，直接拿 JSON"""

    def __init__(self, base_url, username, password, use_llm=False, schema_name="institution.json"):
        self.client = RuoYiApiClient(base_url, username, password)
        self.base_url = base_url
        self.use_llm = use_llm
        self.schema_name = schema_name
        self.parser = LLMParser(use_mock=not use_llm) if use_llm else LLMParser(use_mock=True)
        self.results: list[CrawlResult] = []

    def run(self, endpoints=None):
        """主流程：登录 → 遍历所有表 → 拉取数据 → LLM 解析"""
        if endpoints is None:
            endpoints = KNOWN_ENDPOINTS

        # 1. 登录
        if not self.client.login():
            logger.error("登录失败: %s", getattr(self.client, "last_error", "未知原因"))
            return self.results

        # 2. 拉取每个表的 list 数据
        for ep in endpoints:
            logger.info(f"正在拉取 [{ep['name']}]: {ep['path']}")
            result = self._crawl_table(ep)
            self.results.append(result)

        # 3. LLM 语义增强（可选）
        if self.use_llm and self.parser:
            self._llm_enrich()

        return self.results

    def _crawl_table(self, endpoint_info):
        """拉取单个表的分页数据"""
        path = endpoint_info["path"]
        rows = self.client.get_list(path)

        result = CrawlResult(
            endpoint=path,
            module=endpoint_info["module"],
            table_name=endpoint_info["name"],
            total_count=len(rows),
            collected_count=len(rows),
            success=len(rows) > 0,
        )

        if rows:
            # 提取列名
            result.columns = list(rows[0].keys()) if rows else []
            result.sample_record = rows[0]
            result.records = rows

        logger.info(f"  [{result.table_name}] 共 {result.total_count} 条记录")
        return result

    def _llm_enrich(self):
        """将获取的数据交给 LLM 做语义解析和字段提取"""
        for r in self.results:
            if not r.success or r.total_count == 0:
                continue
            # TODO: 将 rows 数据交给 LLM 按 Schema 提取并结构化
            pass

    def to_dict(self):
        """导出为可序列化的字典"""
        return {
            "base_url": self.base_url,
            "total_tables": len(self.results),
            "total_records": sum(r.collected_count for r in self.results),
            "tables": [asdict(r) for r in self.results],
        }
