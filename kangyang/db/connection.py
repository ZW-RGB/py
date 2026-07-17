# -*- coding: utf-8 -*-
"""数据库连接管理"""

import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import QueuePool

from kangyang.llm.config_loader import get_mysql_config

logger = logging.getLogger(__name__)

_engine = None
_SessionLocal = None


def _build_dsn(config: dict) -> str:
    """构建 MySQL 连接字符串"""
    return (
        f"mysql+pymysql://{config['user']}:{config['password']}"
        f"@{config['host']}:{config['port']}/{config['database']}"
        f"?charset={config.get('charset', 'utf8mb4')}"
    )


def get_engine():
    """获取或创建数据库引擎（单例）"""
    global _engine
    if _engine is None:
        config = get_mysql_config()
        dsn = _build_dsn(config)
        _engine = create_engine(
            dsn,
            poolclass=QueuePool,
            pool_size=config.get("pool_size", 5),
            pool_recycle=config.get("pool_recycle", 3600),
            echo=False,
        )
        logger.info(f"数据库引擎已初始化: {config['host']}:{config['port']}/{config['database']}")
    return _engine


def get_session() -> Session:
    """获取数据库会话"""
    global _SessionLocal
    if _SessionLocal is None:
        engine = get_engine()
        _SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return _SessionLocal()


def init_db():
    """初始化数据库表结构"""
    from kangyang.db.models import Base
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    logger.info("数据库表结构已初始化")
