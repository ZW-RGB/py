# -*- coding: utf-8 -*-
"""数据库模型定义 —— SQLAlchemy ORM"""

import json
from datetime import datetime
from sqlalchemy import (
    Column, BigInteger, String, Text, DateTime, JSON, Index,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class PolicyRecord(Base):
    """
    康养政策数据表

    表结构与 Schema (policy.json) 一一对应，
    额外包含采集元数据列（url、crawl_time 等）
    """
    __tablename__ = "kangyang_policies"

    # ---- 主键 ----
    id = Column(BigInteger, primary_key=True, autoincrement=True, comment="自增主键")

    # ---- 业务字段（与 Schema 对应） ----
    title = Column(String(500), nullable=False, comment="政策标题")
    publish_date = Column(String(20), nullable=True, comment="发布日期")
    doc_number = Column(String(200), nullable=True, comment="文号")
    issuing_authority = Column(String(300), nullable=True, comment="发布机构")
    category = Column(String(200), nullable=True, comment="政策类别")
    summary = Column(Text, nullable=False, comment="核心摘要")
    keywords = Column(JSON, nullable=True, comment="关键主题词列表")
    source_url = Column(String(1000), nullable=False, comment="原文链接")

    # ---- 采集元数据 ----
    crawl_time = Column(DateTime, default=datetime.now, comment="采集时间")
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间"
    )

    # ---- 索引 ----
    __table_args__ = (
        Index("idx_source_url", "source_url"),
        Index("idx_publish_date", "publish_date"),
        Index("idx_category", "category"),
        {"comment": "康养政策采集数据表"},
    )

    def __repr__(self):
        return f"<PolicyRecord(id={self.id}, title='{self.title[:30]}...')>"


# 通用动态表模型工厂（扩展用）
def create_dynamic_model(table_name: str, schema_name: str = None):
    """
    根据 Schema 动态创建表模型（高级用法）

    适用于非固定结构的采集任务，运行时动态建表。
    """
    from kangyang.llm.schema import schema_manager

    if schema_name is None:
        raise ValueError("必须提供 schema_name")

    fields = schema_manager.get_fields(schema_name)
    columns = {
        "__tablename__": table_name,
        "id": Column(BigInteger, primary_key=True, autoincrement=True),
        "crawl_time": Column(DateTime, default=datetime.now),
    }

    for f in fields:
        col_type = _field_type_to_column(f["type"])
        col = Column(
            col_type,
            nullable=not f.get("required", False),
            comment=f.get("description", ""),
        )
        columns[f["name"]] = col

    columns["__table_args__"] = ({"comment": f"动态表: {table_name}"},)
    return type(table_name.capitalize(), (Base,), columns)


def _field_type_to_column(field_type: str):
    """Schema 字段类型 → SQLAlchemy Column 类型"""
    mapping = {
        "string": String(1000),
        "int": BigInteger,
        "float": String(50),   # 浮点数存为字符串避免精度问题
        "date": String(20),
        "bool": String(10),
        "list": JSON,
        "dict": JSON,
        "text": Text,
    }
    return mapping.get(field_type, Text)
