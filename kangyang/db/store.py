# -*- coding: utf-8 -*-
"""数据存储层 —— 将校验通过的 ParsedItem 写入 MySQL"""

import json
import logging
from datetime import datetime

from kangyang.db.connection import get_session
from kangyang.db.models import PolicyRecord

logger = logging.getLogger(__name__)


class DataStore:
    """
    数据存储管理器

    用法:
        store = DataStore()
        store.save(data=parsed_dict, source_url="https://...")
    """

    def __init__(self):
        pass

    def save(self, data: dict, source_url: str) -> bool:
        """
        将解析数据写入数据库

        Args:
            data: LLM 解析后的结构化 dict
            source_url: 数据来源 URL

        Returns:
            True 写入成功，False 写入失败
        """
        session = get_session()
        try:
            # 检查是否已存在（按 source_url 去重）
            existing = (
                session.query(PolicyRecord)
                .filter(PolicyRecord.source_url == source_url)
                .first()
            )
            if existing:
                # 已存在则更新
                self._update_record(existing, data)
                logger.info(f"更新已有记录: {source_url}")
            else:
                # 新建记录
                record = self._build_record(data, source_url)
                session.add(record)
                logger.info(f"新建记录: {source_url}")

            session.commit()
            return True

        except Exception as e:
            session.rollback()
            logger.error(f"数据入库失败: {e}")
            return False

        finally:
            session.close()

    def _build_record(self, data: dict, source_url: str) -> PolicyRecord:
        """构建 PolicyRecord 实例"""
        return PolicyRecord(
            title=data.get("title", ""),
            publish_date=data.get("publish_date", ""),
            doc_number=data.get("doc_number", ""),
            issuing_authority=data.get("issuing_authority", ""),
            category=data.get("category", ""),
            summary=data.get("summary", ""),
            keywords=data.get("keywords", []),
            source_url=source_url or data.get("source_url", ""),
            crawl_time=datetime.now(),
        )

    @staticmethod
    def _update_record(record: PolicyRecord, data: dict):
        """更新已有记录的各字段"""
        for field in [
            "title", "publish_date", "doc_number",
            "issuing_authority", "category", "summary", "keywords",
        ]:
            if field in data and data[field]:
                setattr(record, field, data[field])
        record.updated_at = datetime.now()
