# -*- coding: utf-8 -*-
"""配置加载器 —— 读取 config.yaml"""

import os
import yaml

_CONFIG_CACHE = None


def _get_project_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_config():
    """加载全局配置（带缓存）"""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    config_path = os.path.join(_get_project_root(), "config", "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        _CONFIG_CACHE = yaml.safe_load(f)
    return _CONFIG_CACHE

def get_config_dir():
    """获取 config 目录路径"""
    return os.path.join(_get_project_root(), "config")


def get_llm_config():
    """获取 LLM 配置段"""
    return load_config().get("llm", {})


def get_mysql_config():
    """获取 MySQL 配置段"""
    return load_config().get("mysql", {})


def get_task_config():
    """获取任务配置段"""
    return load_config().get("tasks", {})


def get_validation_config():
    """获取校验配置段"""
    return load_config().get("validation", {})
