# -*- coding: utf-8 -*-
from kangyang.config.config_loader import (
    get_endpoints,
    get_routes,
    reload_config as _reload_config,
    merge_discovered_routes as _merge_discovered_routes,
    save_endpoints,
    get_config_status,
    reset_routes,
    lazy as config_lazy,
)


def reload_config(*args, **kwargs):
    """热重载配置，并通知 intent_parser 重建页面路由索引"""
    result = _reload_config(*args, **kwargs)
    _rebuild_parser_index()
    return result


def merge_discovered_routes(*args, **kwargs):
    """合并发现的路由，并通知 intent_parser 重建页面路由索引"""
    result = _merge_discovered_routes(*args, **kwargs)
    _rebuild_parser_index()
    return result


def _rebuild_parser_index():
    """通知 intent_parser 重建页面路由反向索引"""
    try:
        from kangyang.intent_parser import _rebuild_page_route_index
        _rebuild_page_route_index()
    except ImportError:
        pass
