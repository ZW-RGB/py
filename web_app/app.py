# -*- coding: utf-8 -*-
import os
os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')  # 禁止生成 .pyc 缓存，防止旧代码残留
"""
康养数据 Web 采集控制台 —— Flask 后端

功能：
  - 选择数据表 → 一键爬取
  - 实时进度展示
  - 数据预览（分页表格）
  - 多格式导出（CSV / JSON / Excel / TXT）
"""
import sys
import json
import csv
import io
import threading
import time
import hashlib
import logging
import re as _re
from datetime import datetime
from urllib.parse import urljoin, urlparse, urlunparse
from flask import Flask, render_template, request, jsonify, send_file, session, Response
import requests as _requests

logger = logging.getLogger(__name__)

# 把父目录加入 path，以导入 kangyang 模块
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from kangyang.api_client import RuoYiApiClient
from kangyang.api_crawler import KNOWN_ENDPOINTS
from kangyang.intent_parser import parse_intent, smart_parse, execute_custom_crawl, _apply_filters, _apply_limit
from kangyang.mysql_writer import write_rows
from kangyang.page_scraper import (
    KNOWN_ROUTES, scrape_single_page, scrape_with_screenshot, scrape_batch, scrape_paginated,
    PageScraper
)
from kangyang.query_feedback import record_query, get_endpoint_stats, clear_feedback
from kangyang.config import reload_config, get_config_status, merge_discovered_routes, reset_routes
from kangyang.route_discovery import discover_routes, discover_and_merge
from kangyang.visual_collector import (
    create_snapshot, find_element_by_position, get_region_elements,
    get_sibling_elements, preview_extraction, execute_collection,
    build_selector_for_element, cache_snapshot, get_cached_snapshot, clear_cache,
    ExtractionRule, PageSnapshot, SelectableElement, CollectionTask,
)
from kangyang.task_manager import (
    create_task, get_task, list_tasks, update_task, delete_task,
    mark_task_run, duplicate_task, export_task as export_task_json, import_task,
)

# ── Playwright 兼容性补丁 ──────────────────────────
# Playwright 1.48.0 的 PlaywrightContextManager.__init__ 中
# self._playwright / self._loop 仅是类型注解，未实际初始化属性。
# 在 Flask WSGI 上下文中初始化失败时会触发 AttributeError。
# 此补丁在应用启动时修正 __init__，确保属性始终存在。
# 同时包装 __enter__ 提供清晰的错误信息，避免混惑的
# "NoneType has no attribute 'stop'" 错误。
def _patch_playwright_context_manager():
    from playwright.sync_api._context_manager import PlaywrightContextManager
    if getattr(PlaywrightContextManager.__init__, "_kwb_patched", False):
        return

    # 1. 修复 __init__：显式初始化所有属性
    _orig_init = PlaywrightContextManager.__init__
    def _patched_init(self):
        self._playwright = None
        self._loop = None
        self._own_loop = False
        self._watcher = None
        self._exit_was_called = False
        self._connection = None
    PlaywrightContextManager.__init__ = _patched_init
    PlaywrightContextManager.__init__._kwb_patched = True

    # 2. 包装 __enter__：捕获启动失败，给出清晰的错误信息
    _orig_enter = PlaywrightContextManager.__enter__
    def _patched_enter(self):
        try:
            return _orig_enter(self)
        except AttributeError as e:
            if "'NoneType' object has no attribute" in str(e) or \
               "no attribute 'stop'" in str(e):
                raise RuntimeError(
                    "Playwright Chromium 启动失败。请确保浏览器已安装：\n"
                    "  cd kangyang-crawler && venv/Scripts/python.exe -m playwright install chromium"
                ) from e
            raise
        except Exception as e:
            raise RuntimeError(
                f"Playwright 初始化异常: {e}\n"
                "若反复出现，请手动运行: venv/Scripts/python.exe -m playwright install chromium"
            ) from e
    PlaywrightContextManager.__enter__ = _patched_enter

    logger.info("[Patch] PlaywrightContextManager __init__ + __enter__ 已补丁")

_patch_playwright_context_manager()
# ── 补丁结束 ───────────────────────────────────────

app = Flask(__name__)
app.secret_key = "kangyang-crawler-web-2026"

# ── 输出目录 ──
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")
scrape_results_dir = os.path.join(OUTPUT_DIR, "scrape_results")
os.makedirs(scrape_results_dir, exist_ok=True)

# ── 全局爬取状态 ──
crawl_state = {
    "running": False,
    "progress": 0,          # 0-100
    "current_table": "",
    "total_tables": 0,
    "completed_tables": 0,
    "results": [],          # [{"table_name": ..., "rows": [...], "columns": [...], "count": ...}, ...]
    "error": "",
    "start_time": None,
}

# ── 全局：端点发现（标准模式「加载全部接口」）状态 ──
endpoint_discover_state = {
    "running": False,
    "done": False,
    "progress": 0,
    "total": 0,
    "current": "",
    "found": 0,
    "endpoints": [],
    "error": "",
    "stats": {},
}


def _endpoint_from_path(path):
    """从 API 路径推导最小端点描述，供「加载全部接口」发现的动态端点使用。

    /dev-api/{module}/{entity}/list -> {module, name=entity, path}
    """
    parts = [p for p in path.split("/") if p]
    if parts and parts[0] == "dev-api":
        parts = parts[1:]
    if len(parts) >= 2:
        module, entity = parts[0], parts[1]
    elif parts:
        module = entity = parts[0]
    else:
        module = entity = path
    return {"module": module, "name": entity, "path": path}


def run_crawl(host, username, password, selected_paths):
    """后台线程：执行爬取"""
    global crawl_state

    # 支持「加载全部接口」发现的动态端点：勾选的 path 若在 KNOWN_ENDPOINTS 取详情，
    # 否则从路径动态构造端点描述，避免新发现端口被静态表过滤掉而爬不到。
    known = {ep["path"]: ep for ep in KNOWN_ENDPOINTS}
    endpoints = [known.get(p) or _endpoint_from_path(p) for p in selected_paths]
    _seen, _uniq = set(), []
    for ep in endpoints:
        if ep["path"] not in _seen:
            _seen.add(ep["path"]); _uniq.append(ep)
    endpoints = _uniq

    crawl_state["running"] = True
    crawl_state["progress"] = 0
    crawl_state["total_tables"] = len(endpoints)
    crawl_state["completed_tables"] = 0
    crawl_state["results"] = []
    crawl_state["error"] = ""
    crawl_state["start_time"] = datetime.now().isoformat()

    try:
        client = RuoYiApiClient(host, username, password)
        if not client.login():
            err_detail = getattr(client, "last_error", "")
            crawl_state["error"] = f"登录失败: {err_detail}" if err_detail else "登录失败，请检查账号密码"
            crawl_state["running"] = False
            return

        for i, ep in enumerate(endpoints):
            crawl_state["current_table"] = ep["name"]
            path = ep["path"]

            try:
                rows = client.get_list(path)
                columns = list(rows[0].keys()) if rows else []
                crawl_state["results"].append({
                    "table_name": ep["name"],
                    "module": ep["module"],
                    "path": path,
                    "rows": rows,
                    "columns": columns,
                    "count": len(rows),
                })
            except Exception as e:
                crawl_state["results"].append({
                    "table_name": ep["name"],
                    "module": ep["module"],
                    "path": path,
                    "rows": [],
                    "columns": [],
                    "count": 0,
                    "error": str(e),
                })

            crawl_state["completed_tables"] = i + 1
            crawl_state["progress"] = int((i + 1) / len(endpoints) * 100)

        crawl_state["current_table"] = "完成"
    except Exception as e:
        crawl_state["error"] = str(e)
    finally:
        crawl_state["running"] = False


# ═══════════════ 页面路由 ═══════════════

@app.route("/")
def index():
    """主页面"""
    from flask import make_response
    resp = make_response(render_template("index.html", endpoints=KNOWN_ENDPOINTS, routes=KNOWN_ROUTES))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ═══════════════ API 路由 ═══════════════

@app.route("/api/tables")
def api_tables():
    """返回所有可爬取的数据表列表"""
    return jsonify(KNOWN_ENDPOINTS)


@app.route("/api/endpoints/discover", methods=["POST"])
def api_discover_endpoints():
    """标准模式「加载全部接口」：登录后端 → 拉菜单 → 探测所有真实 list 接口。

    后台线程执行（探测约 20~30s），前端轮询 /api/endpoints/discover/status 拿进度。
    完成后把真实存在的接口合并进 endpoints.yaml 并刷新内存表（持久化）。
    """
    global endpoint_discover_state
    if endpoint_discover_state["running"]:
        return jsonify({"ok": False, "message": "已有发现任务在运行，请稍候"})
    data = request.get_json(silent=True) or {}
    host = (data.get("host") or "").strip()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not host or not username:
        return jsonify({"ok": False, "message": "请先填写平台地址和账号"})

    endpoint_discover_state = {
        "running": True, "done": False, "progress": 0, "total": 0,
        "current": "", "found": 0, "endpoints": [], "error": "", "stats": {},
    }

    def _run():
        global endpoint_discover_state
        try:
            from kangyang.endpoint_discovery import discover_all_endpoints
            def cb(done, total, perm, found_so_far):
                endpoint_discover_state["progress"] = done
                endpoint_discover_state["total"] = total
                endpoint_discover_state["current"] = perm
                endpoint_discover_state["found"] = found_so_far
            found, stats = discover_all_endpoints(host, username, password, progress_cb=cb)
            endpoint_discover_state["endpoints"] = found
            endpoint_discover_state["stats"] = stats
            endpoint_discover_state["found"] = len(found)
            # 持久化合并进 endpoints.yaml，并刷新本进程 KNOWN_ENDPOINTS，
            # 使下次刷新页面/重启后这些端口自动出现在标准模式列表。
            try:
                from kangyang.config.config_loader import (
                    _config_loader, reload_config, get_endpoints,
                )
                _config_loader.save_endpoints(found)
                reload_config()
                import kangyang.api_crawler as _ac
                _ac.KNOWN_ENDPOINTS = get_endpoints()
                globals()["KNOWN_ENDPOINTS"] = get_endpoints()
                logger.info("已合并 %d 个接口进 endpoints.yaml 并刷新内存表", len(found))
            except Exception as e:
                logger.warning("持久化/刷新端点失败（不影响本次结果）: %s", e)
        except Exception as e:
            endpoint_discover_state["error"] = str(e)
            logger.exception("发现端点异常")
        finally:
            endpoint_discover_state["running"] = False
            endpoint_discover_state["done"] = True

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "已开始从后端发现全部数据接口"})


@app.route("/api/endpoints/discover/status", methods=["GET"])
def api_discover_endpoints_status():
    """轮询端点发现进度。"""
    return jsonify(endpoint_discover_state)


@app.route("/api/crawl", methods=["POST"])
def api_crawl():
    """启动爬取任务"""
    global crawl_state

    if crawl_state["running"]:
        return jsonify({"ok": False, "message": "已有爬取任务正在运行"}), 409

    data = request.get_json()
    host = data.get("host", "").strip()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    selected_paths = data.get("tables", [])

    if not host or not username:
        return jsonify({"ok": False, "message": "请填写平台地址和账号"}), 400
    if not selected_paths:
        return jsonify({"ok": False, "message": "请至少选择一张数据表"}), 400

    thread = threading.Thread(target=run_crawl, args=(host, username, password, selected_paths), daemon=True)
    thread.start()

    return jsonify({"ok": True, "message": "爬取任务已启动"})


@app.route("/api/crawl_status")
def api_crawl_status():
    """查询爬取进度"""
    return jsonify({
        "running": crawl_state["running"],
        "progress": crawl_state["progress"],
        "current_table": crawl_state["current_table"],
        "total_tables": crawl_state["total_tables"],
        "completed_tables": crawl_state["completed_tables"],
        "error": crawl_state["error"],
        "start_time": crawl_state["start_time"],
        # 只返回摘要，不返回完整数据（数据太大）
        "summary": [
            {"table_name": r["table_name"], "count": r["count"], "columns": r["columns"]}
            for r in crawl_state["results"]
        ],
    })


@app.route("/api/preview/<int:table_index>")
def api_preview(table_index):
    """预览某张表的前100条数据"""
    results = crawl_state["results"]
    if table_index < 0 or table_index >= len(results):
        return jsonify({"ok": False, "message": "无效的表索引"}), 404

    r = results[table_index]
    rows = r.get("rows", [])
    return jsonify({
        "ok": True,
        "table_name": r["table_name"],
        "columns": r["columns"],
        "count": r["count"],
        "rows": rows[:100],  # 只返回前100条
    })


@app.route("/api/export", methods=["POST"])
def api_export():
    """导出数据（按格式下载文件）"""
    data = request.get_json()
    fmt = data.get("format", "csv").lower()
    table_index = data.get("table_index")  # None 表示导出全部
    encode_utf8_sig = data.get("utf8_bom", True)  # Excel 打开 CSV 不乱码

    results = crawl_state["results"]

    if table_index is not None:
        if table_index < 0 or table_index >= len(results):
            return jsonify({"ok": False, "message": "无效的表索引"}), 404
        to_export = [results[table_index]]
        name_hint = results[table_index]["table_name"]
    else:
        to_export = results
        name_hint = "all"

    if fmt == "csv":
        return _export_csv(to_export, name_hint, encode_utf8_sig)
    elif fmt == "json":
        return _export_json(to_export, name_hint)
    elif fmt == "xlsx":
        return _export_xlsx(to_export, name_hint)
    elif fmt == "txt":
        return _export_txt(to_export, name_hint)
    else:
        return jsonify({"ok": False, "message": f"不支持的格式: {fmt}"}), 400


# ═══════════════ 智能意图解析 API ═══════════════

# 自定义爬取状态（独立的，不干扰标准模式）
custom_crawl_state = {
    "running": False,
    "progress": 0,
    "status_text": "",
    "results": [],
    "columns": [],
    "intent_info": {},
    "error": "",
}


@app.route("/api/parse_intent", methods=["POST"])
def api_parse_intent():
    """
    解析用户的自然语言需求 → 爬取规则

    请求: {"user_query": "获取长者档案页面的所有长者姓名以及联系方式"}
    响应: {
        "ok": true,
        "intent": {
            "target_api": "/dev-api/xxx/list",
            "api_name": "长者档案",
            "fields": ["姓名", "联系方式"],
            "field_descriptions": {...},
            "description": "...",
            "confidence": 0.92,
            "reasoning": "..."
        }
    }
    """
    data = request.get_json()
    user_query = data.get("user_query", "").strip()

    if not user_query:
        return jsonify({"ok": False, "message": "请输入您的数据需求描述"}), 400

    try:
        intent = smart_parse(user_query)
        if "error" in intent:
            return jsonify({"ok": False, "message": f"意图解析失败: {intent['error']}"}), 500

        # 透传原始查询，供灵活层兜底（无高置信匹配时按实体名自动拼接口探测）
        intent["_query"] = user_query
        return jsonify({"ok": True, "intent": intent})
    except Exception as e:
        return jsonify({"ok": False, "message": f"解析异常: {str(e)}"}), 500


# ── v5: 用户反馈 API ──

@app.route("/api/feedback", methods=["POST"])
def api_submit_feedback():
    """
    提交用户反馈（确认/修正解析结果）

    请求: {
        "user_query": "获取老人的心率数据",
        "intent": {解析结果},
        "action": "confirm" | "correct",
        "corrected_endpoint": "/dev-api/xxx/list",  (仅 correct 时)
        "corrected_api_name": "心率监测",            (仅 correct 时)
        "corrected_fields": ["心率", "时间"]          (可选)
    }
    """
    data = request.get_json()
    user_query = data.get("user_query", "").strip()
    intent = data.get("intent", {})
    action = data.get("action", "confirm")
    corrected_endpoint = data.get("corrected_endpoint", "")
    corrected_fields = data.get("corrected_fields", [])

    if not user_query:
        return jsonify({"ok": False, "message": "缺少查询内容"}), 400

    try:
        if action == "correct":
            # 用户修正了端点
            corrected_intent = dict(intent)
            if corrected_endpoint:
                corrected_intent["target_api"] = corrected_endpoint
                corrected_intent["api_name"] = data.get("corrected_api_name", "")
            if corrected_fields:
                corrected_intent["fields"] = corrected_fields

            record_query(
                user_query, intent,
                user_confirmed=False,
                corrected_endpoint=corrected_endpoint,
                corrected_fields=corrected_fields,
            )
            return jsonify({"ok": True, "message": "已记录修正，下次相似查询将优先匹配此端点"})
        else:
            # 用户确认结果正确
            record_query(user_query, intent, user_confirmed=True)
            return jsonify({"ok": True, "message": "已确认解析结果正确"})
    except Exception as e:
        return jsonify({"ok": False, "message": f"反馈记录失败: {str(e)}"}), 500


@app.route("/api/feedback/stats", methods=["GET"])
def api_feedback_stats():
    """获取反馈学习统计"""
    try:
        stats = get_endpoint_stats()
        return jsonify({"ok": True, "stats": stats})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/feedback/clear", methods=["POST"])
def api_clear_feedback():
    """清空反馈历史"""
    try:
        clear_feedback()
        return jsonify({"ok": True, "message": "反馈历史已清空"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


# ═══════════════ 配置管理 API ═══════════════

@app.route("/api/config/status", methods=["GET"])
def api_config_status():
    """查看当前配置状态（YAML vs 默认、端点/路由数量、来源等）"""
    try:
        status = get_config_status()
        return jsonify({"ok": True, "config": status})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/config/reload", methods=["POST"])
def api_config_reload():
    """热重载配置：重新从 YAML 文件加载端点和路由"""
    try:
        reload_config()
        status = get_config_status()
        return jsonify({
            "ok": True,
            "message": "配置已重载",
            "config": status,
        })
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/config/endpoints", methods=["GET"])
def api_list_endpoints():
    """列出所有 API 端点"""
    try:
        from kangyang.config import get_endpoints as ge
        eps = ge()
        return jsonify({
            "ok": True,
            "total": len(eps),
            "endpoints": eps,
        })
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/config/routes", methods=["GET"])
def api_list_routes():
    """列出所有前端路由"""
    try:
        from kangyang.config import get_routes as gr
        routes = gr()
        return jsonify({
            "ok": True,
            "total": len(routes),
            "routes": routes,
        })
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


# ═══════════════ 路由自动发现 API ═══════════════

@app.route("/api/discover/routes", methods=["POST"])
def api_discover_routes():
    """
    自动发现侧边栏路由（需要平台凭据）

    请求体:
      { host, username, password, strategy? ("append"|"replace"), headless? (true|false) }

    响应:
      { ok: true, discovery: { routes: [...], source: "index_attr"|..., total_found: N } }
    """
    data = request.get_json()
    host = data.get("host", "").strip()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    strategy = data.get("strategy", "append")
    headless = data.get("headless", True)

    if not host or not username or not password:
        return jsonify({
            "ok": False,
            "message": "请提供 host, username, password 参数"
        }), 400

    try:
        result = discover_and_merge(
            host=host,
            username=username,
            password=password,
            strategy=strategy,
            headless=headless,
        )
        return jsonify({"ok": True, "discovery": result})
    except Exception as e:
        return jsonify({"ok": False, "message": f"路由发现失败: {str(e)}"}), 500


@app.route("/api/discover/routes/scan", methods=["POST"])
def api_discover_routes_scan_only():
    """
    仅扫描路由，不合并到全局配置（预览模式）

    请求体:
      { host, username, password, headless? }

    响应:
      { ok: true, discovery: { routes: [...], source: str, total_found: N, errors: [...] } }
    """
    data = request.get_json()
    host = data.get("host", "").strip()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    headless = data.get("headless", True)

    if not host or not username or not password:
        return jsonify({
            "ok": False,
            "message": "请提供 host, username, password 参数"
        }), 400

    try:
        result = discover_routes(
            host=host,
            username=username,
            password=password,
            headless=headless,
        )
        return jsonify({
            "ok": True,
            "discovery": {
                "routes": result.routes,
                "source": result.source,
                "total_found": result.total_found,
                "errors": result.errors,
            }
        })
    except Exception as e:
        return jsonify({"ok": False, "message": f"路由发现失败: {str(e)}"}), 500


@app.route("/api/config/routes/reset", methods=["POST"])
def api_routes_reset():
    """重置路由为 YAML 文件配置或默认值"""
    try:
        reset_routes()
        status = get_config_status()
        return jsonify({
            "ok": True,
            "message": "路由已重置",
            "config": status,
        })
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/crawl_custom", methods=["POST"])
def api_crawl_custom():
    """根据已确认的意图执行自定义爬取（支持 API 和页面采集两种模式）"""
    global custom_crawl_state

    if custom_crawl_state["running"]:
        return jsonify({"ok": False, "message": "已有自定义爬取任务正在运行"}), 409

    data = request.get_json()
    host = data.get("host", "").strip()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    intent = data.get("intent", {})

    if not host or not username:
        return jsonify({"ok": False, "message": "请填写平台地址和账号"}), 400

    # ── 页面采集模式 ──
    if intent.get("source_type") == "page":
        return _execute_page_crawl(host, username, password, intent)

    # ── API 采集模式（原有逻辑）──
    # 允许「无 target_api 但携带原始 query」时进入灵活层兜底（自动拼接口探测），
    # 只有 intent 为空且既无 target_api 也无 query 时才提示先完成意图解析。
    if not intent:
        return jsonify({"ok": False, "message": "请先完成意图解析"}), 400
    if not intent.get("target_api") and not (intent.get("_query") or data.get("user_query")):
        return jsonify({"ok": False, "message": "请先完成意图解析"}), 400

    custom_crawl_state["running"] = True
    custom_crawl_state["progress"] = 10
    custom_crawl_state["status_text"] = "正在连接平台..."
    custom_crawl_state["intent_info"] = intent

    try:
        custom_crawl_state["status_text"] = "正在调用 API 获取数据..."
        custom_crawl_state["progress"] = 30

        query = intent.get("_query") or data.get("user_query", "")
        rows = execute_custom_crawl(host, username, password, intent, query=query)

        custom_crawl_state["progress"] = 90
        custom_crawl_state["status_text"] = "正在整理数据..."

        if isinstance(rows, list) and len(rows) > 0:
            if "error" in rows[0]:
                custom_crawl_state["error"] = rows[0].get("error", "未知错误")
                custom_crawl_state["running"] = False
                return jsonify({"ok": False, "message": custom_crawl_state["error"]}), 500

            # 提取缺失字段信息后过滤内部字段
            missing_fields = rows[0].get("_missing_fields", [])
            filter_stats = rows[0].get("_filter_stats", {"applied": [], "filtered_out": 0, "not_applied": []})
            for row in rows:
                row.pop("_missing_fields", None)
                row.pop("_filter_stats", None)
                row.pop("_limit_applied", None)
            columns = list(rows[0].keys())
        else:
            missing_fields = []
            columns = []

        custom_crawl_state["results"] = rows
        custom_crawl_state["columns"] = columns
        custom_crawl_state["progress"] = 100
        custom_crawl_state["status_text"] = "完成"

        return jsonify({
            "ok": True,
            "count": len(rows),
            "columns": columns,
            "rows": rows[:200],
            "total_rows": len(rows),
            "intent": intent,
            "missing_fields": missing_fields,
            "filter_stats": filter_stats,
        })

    except Exception as e:
        custom_crawl_state["error"] = str(e)
        return jsonify({"ok": False, "message": f"爬取异常: {str(e)}"}), 500
    finally:
        custom_crawl_state["running"] = False


def _execute_page_crawl(host, username, password, intent):
    """执行页面采集（智能模式）"""
    global custom_crawl_state

    url_or_route = intent.get("url_or_route", "")
    user_fields = intent.get("fields", [])
    is_paginated = intent.get("is_paginated", True)

    if not url_or_route:
        return jsonify({"ok": False, "message": "未找到要采集的页面地址"}), 400

    custom_crawl_state["running"] = True
    custom_crawl_state["progress"] = 10
    custom_crawl_state["status_text"] = "正在启动浏览器..."
    custom_crawl_state["intent_info"] = intent

    try:
        custom_crawl_state["progress"] = 20
        custom_crawl_state["status_text"] = "正在登录平台..."

        if is_paginated:
            # 分页采集（自动翻页，合并所有表格）
            custom_crawl_state["status_text"] = "正在分页采集页面数据..."
            custom_crawl_state["progress"] = 30

            result = scrape_paginated(
                host=host,
                username=username,
                password=password,
                route_or_url=url_or_route,
                page_start=1,
                page_end=None,     # 自动检测全部页
                page_size=None,
                headless=True,
                fields=user_fields if user_fields else None,
            )
        else:
            # 单页采集
            custom_crawl_state["status_text"] = "正在采集页面内容..."
            custom_crawl_state["progress"] = 30

            result = scrape_single_page(
                host, username, password, url_or_route,
                fields=user_fields if user_fields else None
            )

        custom_crawl_state["progress"] = 80
        custom_crawl_state["status_text"] = "正在整理表格数据..."

        # 提取表格数据为行格式（模拟 API 返回的数据结构）
        rows, columns, filter_stats = _extract_rows_from_page_result(
            result, user_fields, is_paginated, intent.get("filters", {}), intent.get("limit"))

        custom_crawl_state["results"] = rows
        custom_crawl_state["columns"] = columns
        custom_crawl_state["progress"] = 100
        custom_crawl_state["status_text"] = "完成"

        # 检查缺失字段
        missing_fields = [f for f in user_fields if f not in columns] if user_fields else []

        # 提取多格式数据（images, links, text, forms, sections）
        if is_paginated:
            all_data = {
                "images": result.get("merged_images", []),
                "links": result.get("merged_links", []),
                "raw_text": result.get("merged_text", ""),
                "forms": result.get("merged_forms", []),
                "sections": result.get("merged_structure", []),
                "tables": result.get("merged_tables", []),
                "metadata": {},
            }
        else:
            all_data = {
                "images": result.get("images", []),
                "links": result.get("links", []),
                "raw_text": result.get("raw_text", ""),
                "forms": result.get("forms", []),
                "sections": result.get("sections", []),
                "tables": result.get("tables", []),
                "metadata": result.get("metadata", {}),
            }

        return jsonify({
            "ok": True,
            "count": len(rows),
            "columns": columns,
            "rows": rows[:200],
            "total_rows": len(rows),
            "intent": intent,
            "missing_fields": missing_fields,
            "filter_stats": filter_stats,
            "source_type": "page",
            "page_info": {
                "title": result.get("title", ""),
                "url": result.get("url", ""),
                "timestamp": result.get("timestamp", ""),
                "total_pages": result.get("total_pages_scraped", 1),
                "tables_count": len(result.get("merged_tables", result.get("tables", []))),
            },
            "all_data": all_data,
        })

    except Exception as e:
        custom_crawl_state["error"] = str(e)
        logger.error(f"页面采集异常: {e}", exc_info=True)
        return jsonify({"ok": False, "message": f"页面采集异常: {str(e)}"}), 500
    finally:
        custom_crawl_state["running"] = False


def _extract_rows_from_page_result(result, user_fields, is_paginated, filters=None, limit=None):
    """
    从页面采集结果中提取表格数据为行格式

    参数:
        result: scrape_paginated() 或 scrape_single_page() 的返回值
        user_fields: 用户指定要的字段列表
        is_paginated: 是否分页采集
        filters: 查询解析出的过滤条件（时间/状态/关键词）
        limit: 行数限制 {"limit":N,"mode":...}（前N条 / 后N条 / 第N条）

    返回: (rows, columns, filter_stats)
    """
    rows = []
    columns = []

    if is_paginated:
        tables = result.get("merged_tables", [])
    else:
        tables = result.get("tables", [])

    if tables:
        # 智能选择主表：综合「字段命中率 + 行数」打分，避免取到筛选区/统计卡等小表
        # 而非简单取第一个出现的表（merged_tables 按 DOM 出现顺序拼接，未必是主数据表）
        best_table = None
        best_score = -1.0
        for _t in tables:
            _headers = _t.get("headers", [])
            _rows = _t.get("rows", [])
            hit_rate = 0.0
            if user_fields and _headers:
                from difflib import SequenceMatcher
                _hit = 0
                for _f in user_fields:
                    if _f in _headers or any(SequenceMatcher(None, _f, _h).ratio() >= 0.72 for _h in _headers):
                        _hit += 1
                hit_rate = _hit / len(user_fields)
            # 行数归一（30 行以上视为满分行数），过滤区/统计卡通常行数极少
            row_score = min(1.0, len(_rows) / 30.0)
            score = (hit_rate * 0.7 + row_score * 0.3) if user_fields else row_score
            if score > best_score:
                best_score = score
                best_table = _t
        main_table = best_table or tables[0]
        headers = main_table.get("headers", [])
        table_rows = main_table.get("rows", [])

        if user_fields:
            # 字段过滤：只保留用户指定的列
            # 先做字段名匹配（中文→表格列名）
            selected_indices = []
            for f in user_fields:
                # 精确匹配
                if f in headers:
                    selected_indices.append(headers.index(f))
                else:
                    # 模糊匹配
                    best_idx = None
                    best_score = 0
                    from difflib import SequenceMatcher
                    for i, h in enumerate(headers):
                        score = SequenceMatcher(None, f, h).ratio()
                        if score > best_score and score >= 0.72:
                            best_score = score
                            best_idx = i
                    if best_idx is not None:
                        selected_indices.append(best_idx)

            if selected_indices:
                columns = [headers[i] for i in selected_indices]
                for row in table_rows:
                    item = {}
                    for col_idx, col_name in zip(selected_indices, columns):
                        item[col_name] = row[col_idx] if col_idx < len(row) else ""
                    rows.append(item)
            else:
                # 没有匹配到任何字段，返回全部
                columns = headers
                for row in table_rows:
                    item = {}
                    for i, h in enumerate(headers):
                        item[h] = row[i] if i < len(row) else ""
                    rows.append(item)
        else:
            # 全部字段
            columns = headers
            for row in table_rows:
                item = {}
                for i, h in enumerate(headers):
                    item[h] = row[i] if i < len(row) else ""
                rows.append(item)
    else:
        # 没有表格数据，返回空
        columns = []
        rows = []

    # 过滤掉过长的列（只保留前20列）
    if len(columns) > 20:
        columns = columns[:20]
        rows = [{h: row[i] if i < len(row) else "" for i, h in enumerate(columns)} for row in rows]

    # 应用查询中的过滤条件（时间/状态/关键词）
    filter_stats = {"applied": [], "filtered_out": 0, "not_applied": []}
    if filters and rows:
        rows, filter_stats = _apply_filters(rows, filters)

    # 应用行数限制（前N条 / 后N条 / 第N条）
    if limit and rows:
        rows = _apply_limit(rows, limit)

    return rows, columns, filter_stats


@app.route("/api/crawl_custom_status")
def api_crawl_custom_status():
    """查询自定义爬取进度"""
    return jsonify({
        "running": custom_crawl_state["running"],
        "progress": custom_crawl_state["progress"],
        "status_text": custom_crawl_state["status_text"],
        "error": custom_crawl_state["error"],
    })


@app.route("/api/crawl_custom_export", methods=["POST"])
def api_crawl_custom_export():
    """导出自定义爬取的结果"""
    data = request.get_json()
    fmt = data.get("format", "csv").lower()
    columns = custom_crawl_state["columns"]
    rows = custom_crawl_state["results"]
    intent_info = custom_crawl_state["intent_info"]

    name_hint = intent_info.get("api_name", "custom")
    wrapper = [{
        "table_name": intent_info.get("api_name", "自定义提取"),
        "module": "custom",
        "columns": columns,
        "rows": rows,
        "count": len(rows),
    }]

    if fmt == "csv":
        return _export_csv(wrapper, name_hint, True)
    elif fmt == "json":
        return _export_json(wrapper, name_hint)
    elif fmt == "xlsx":
        return _export_xlsx(wrapper, name_hint)
    elif fmt == "txt":
        return _export_txt(wrapper, name_hint)
    else:
        return jsonify({"ok": False, "message": f"不支持的格式: {fmt}"}), 400


@app.route("/api/crawl_custom_mysql", methods=["POST"])
def api_crawl_custom_mysql():
    """
    把已爬取（智能模式）的结果写入 MySQL 数据库。
    读取内存中的 custom_crawl_state["results"]（全量），无需重新爬取。
    请求体: { mysql: {host, port, user, password, database},
              table: "表名", mode: "append"|"replace" }
    """
    data = request.get_json() or {}
    mysql_cfg = data.get("mysql", {}) or {}
    table = (data.get("table") or "").strip() or "crawled_data"
    mode = data.get("mode", "append")
    if mode not in ("append", "replace"):
        mode = "append"

    rows = custom_crawl_state.get("results") or []
    columns = custom_crawl_state.get("columns") or []

    if not rows:
        return jsonify({
            "ok": False,
            "message": "当前没有可写入的数据，请先执行一次爬取。",
            "inserted": 0,
        }), 400

    # 过滤内部字段（以 _ 开头）
    clean_rows = []
    for r in rows:
        if isinstance(r, dict):
            clean_rows.append({k: v for k, v in r.items() if not str(k).startswith("_")})
        else:
            clean_rows.append(r)
    clean_columns = [c for c in columns if not str(c).startswith("_")]

    result = write_rows(
        rows=clean_rows,
        columns=clean_columns,
        config=mysql_cfg,
        table_name=table,
        mode=mode,
    )

    if result.get("ok"):
        return jsonify({
            "ok": True,
            "message": f"已写入 MySQL 表 `{result['table']}`，共 {result['inserted']} 行。",
            "inserted": result["inserted"],
            "table": result["table"],
            "columns": result.get("columns", []),
        })
    else:
        return jsonify({
            "ok": False,
            "message": f"写入 MySQL 失败: {result.get('error')}",
            "inserted": 0,
        }), 500


@app.route("/api/crawl/standard_mysql", methods=["POST"])
def api_crawl_standard_mysql():
    """
    把标准模式已爬取的结果（多张表）写入 MySQL 数据库。
    标准模式是「一端点一表」结构，这里按每张表分别写入独立的 MySQL 表
    （表名 = 前缀 + "_" + 原表名，前缀来自请求体的 table 字段，可空）。
    读取内存中的 crawl_state["results"]（全量），无需重新爬取。
    请求体: { mysql: {host, port, user, password, database},
              table: "表名前缀(可空)", mode: "append"|"replace" }
    """
    data = request.get_json() or {}
    mysql_cfg = data.get("mysql", {}) or {}
    table_prefix = (data.get("table") or "").strip()
    mode = data.get("mode", "append")
    if mode not in ("append", "replace"):
        mode = "append"

    if not (mysql_cfg.get("host") and mysql_cfg.get("database") and mysql_cfg.get("user")):
        return jsonify({
            "ok": False,
            "message": "MySQL 连接信息不完整（主机 / 数据库 / 用户名必填）。",
            "inserted": 0,
        }), 500

    results = crawl_state.get("results") or []
    if not results:
        return jsonify({
            "ok": False,
            "message": "当前标准模式没有爬取到任何数据，请先执行爬取。",
            "inserted": 0,
        }), 400

    tables = []
    for idx, t in enumerate(results):
        name = t.get("table_name") or f"table_{idx}"
        safe_name = f"{table_prefix}_{name}" if table_prefix else name
        # 列 + 内部字段过滤（以 _ 开头）
        columns = [c for c in t.get("columns", []) if not str(c).startswith("_")]
        raw_rows = t.get("rows", []) or []
        rows = []
        for r in raw_rows:
            if isinstance(r, dict):
                rows.append({k: v for k, v in r.items() if not str(k).startswith("_")})
            else:
                rows.append(r)
        if not rows:
            continue
        res = write_rows(
            rows=rows,
            columns=columns,
            config=mysql_cfg,
            table_name=safe_name,
            mode=mode,
        )
        tables.append({
            "table": res.get("table", safe_name),
            "requested": safe_name,
            "inserted": res.get("inserted", 0),
            "ok": res.get("ok", False),
            "error": res.get("error"),
        })

    if not tables:
        return jsonify({
            "ok": False,
            "message": "没有可写入的表格或行数据。",
            "inserted": 0,
        }), 400

    ok_tables = [x for x in tables if x["ok"]]
    total = sum(x["inserted"] for x in tables)
    detail = "; ".join(f"`{x['table']}` {x['inserted']}行" for x in tables)
    msg = f"已写入 {len(ok_tables)}/{len(tables)} 张表，共 {total} 行：{detail}"
    failed = [x for x in tables if not x["ok"]]
    if failed:
        msg += "；失败：" + "; ".join(f"`{x['requested']}`（{x['error']}）" for x in failed)

    return jsonify({
        "ok": len(ok_tables) > 0,
        "message": msg,
        "inserted": total,
        "tables": tables,
    }), (200 if ok_tables else 500)


@app.route("/api/collector/mysql", methods=["POST"])
def api_collector_mysql():
    """
    把可视化采集（标注模式）的预览结果写入 MySQL 数据库。
    读取 visual_collector_state["last_preview"]（预览时缓存），无需重新提取。
    请求体: { mysql: {host, port, user, password, database},
              table: "表名", mode: "append"|"replace" }
    """
    data = request.get_json() or {}
    mysql_cfg = data.get("mysql", {}) or {}
    table = (data.get("table") or "").strip() or "collector_data"
    mode = data.get("mode", "append")
    if mode not in ("append", "replace"):
        mode = "append"

    preview = visual_collector_state.get("last_preview")
    if not preview or not preview.get("rows"):
        return jsonify({
            "ok": False,
            "message": "当前没有可写入的数据，请先在「标注模式」预览一次提取结果。",
            "inserted": 0,
        }), 400

    rows = preview.get("rows") or []
    columns = preview.get("columns") or []

    # 过滤内部字段（以 _ 开头）
    clean_rows = []
    for r in rows:
        if isinstance(r, dict):
            clean_rows.append({k: v for k, v in r.items() if not str(k).startswith("_")})
        else:
            clean_rows.append(r)
    clean_columns = [c for c in columns if not str(c).startswith("_")]

    result = write_rows(
        rows=clean_rows,
        columns=clean_columns,
        config=mysql_cfg,
        table_name=table,
        mode=mode,
    )

    if result.get("ok"):
        return jsonify({
            "ok": True,
            "message": f"已写入 MySQL 表 `{result['table']}`，共 {result['inserted']} 行。",
            "inserted": result["inserted"],
            "table": result["table"],
            "columns": result.get("columns", []),
        })
    else:
        return jsonify({
            "ok": False,
            "message": f"写入 MySQL 失败: {result.get('error')}",
            "inserted": 0,
        }), 500


@app.route("/api/page/crawl_mysql", methods=["POST"])
def api_page_crawl_mysql():
    """
    把页面采集（含分页采集）的表格结果写入 MySQL 数据库。
    从 page_scrape_state 中提取所有页面的表格，扁平化为统一行格式（联合列名）。
    请求体: { mysql: {host, port, user, password, database},
              table: "表名", mode: "append"|"replace" }
    """
    data = request.get_json() or {}
    mysql_cfg = data.get("mysql", {}) or {}
    table = (data.get("table") or "").strip() or "page_scrape_data"
    mode = data.get("mode", "append")
    if mode not in ("append", "replace"):
        mode = "append"

    results = page_scrape_state.get("results") or []
    paginated = page_scrape_state.get("paginated_data")

    # 收集表格：分页采集优先用 merged_tables（已是合并后的主表）；
    # 单页采集用各页 tables，再挑「行数最多」的主表，避免把筛选区/统计卡等
    # 小表与真正的数据表拼成一个稀疏大宽表写进数据库。
    tables = []
    if paginated and paginated.get("merged_tables"):
        tables = list(paginated.get("merged_tables") or [])
    else:
        for page in results:
            for t in page.get("tables", []):
                tables.append(t)

    if not tables:
        return jsonify({
            "ok": False,
            "message": "当前页面采集结果中没有可用的表格数据，无法写入 MySQL。",
            "inserted": 0,
        }), 400

    # 多表时只写主表（行数最多），其余表忽略但提示用户
    main_table = tables[0]
    if len(tables) > 1:
        main_table = max(tables, key=lambda t: len(t.get("rows", []) or []))

    headers = [c for c in (main_table.get("headers", []) or []) if not str(c).startswith("_")]
    raw_rows = main_table.get("rows", []) or []

    if not headers or not raw_rows:
        return jsonify({
            "ok": False,
            "message": "页面采集的主表中没有可写入的行列数据。",
            "inserted": 0,
        }), 400

    rows = []
    for row in raw_rows:
        item = {}
        for i, h in enumerate(headers):
            item[h] = row[i] if i < len(row) else ""
        rows.append(item)

    total_tables = len(tables)
    result = write_rows(
        rows=rows,
        columns=headers,
        config=mysql_cfg,
        table_name=table,
        mode=mode,
    )

    if result.get("ok"):
        msg = f"已写入 MySQL 表 `{result['table']}`，共 {result['inserted']} 行。"
        if total_tables > 1:
            msg += f"（本次共识别 {total_tables} 个表格，已写入行数最多的主表；如需其他表格请分开采集）"
        return jsonify({
            "ok": True,
            "message": msg,
            "inserted": result["inserted"],
            "table": result["table"],
            "columns": result.get("columns", []),
        })
    else:
        return jsonify({
            "ok": False,
            "message": f"写入 MySQL 失败: {result.get('error')}",
            "inserted": 0,
        }), 500


# ═══════════════ 页面完整采集 API ═══════════════

# 页面采集状态
page_scrape_state = {
    "running": False,
    "progress": 0,
    "status_text": "",
    "current_route": "",
    "results": [],         # [PageContent dict, ...]
    "paginated_data": None,  # 分页采集的合并结果
    "error": "",
}

# 设备配置状态（session 级别，也可在这里缓存）
page_config = {
    "host": "",
    "username": "",
    "password": "",
}


@app.route("/api/page/routes")
def api_page_routes():
    """返回已知的页面路由列表（对应平台菜单）"""
    return jsonify(KNOWN_ROUTES)


@app.route("/api/page/scrape", methods=["POST"])
def api_page_scrape():
    """
    爬取指定页面的完整内容（文本/图片/链接/表格/表单/结构）

    请求: {
        "host": "http://192.168.18.143:1024",
        "username": "admin",
        "password": "admin123",
        "target": "/elderly/checkin",    // Vue 路由 或 完整 URL
        "with_screenshot": false,         // 是否带截图（base64，体积大）
        "full_page_screenshot": false     // 是否整页截图
    }
    响应: {
        "ok": true,
        "result": { ... PageContent dict ... }
    }
    """
    global page_scrape_state

    if page_scrape_state["running"]:
        return jsonify({"ok": False, "message": "已有页面采集任务正在运行"}), 409

    data = request.get_json()
    host = data.get("host", "").strip()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    target = data.get("target", "").strip()
    with_screenshot = data.get("with_screenshot", False)
    full_page_screenshot = data.get("full_page_screenshot", False)
    fields = data.get("fields", None)  # 可选：只保留指定字段列

    if not host or not username:
        return jsonify({"ok": False, "message": "请填写平台地址和账号"}), 400
    if not target:
        return jsonify({"ok": False, "message": "请指定要爬取的页面路由或 URL"}), 400

    page_scrape_state["running"] = True
    page_scrape_state["progress"] = 10
    page_scrape_state["status_text"] = "正在启动浏览器..."
    page_scrape_state["current_route"] = target

    try:
        page_scrape_state["progress"] = 20
        page_scrape_state["status_text"] = "正在登录平台..."

        if with_screenshot:
            result = scrape_with_screenshot(host, username, password, target,
                                            full_page=full_page_screenshot)
        else:
            result = scrape_single_page(host, username, password, target, fields=fields)

        page_scrape_state["progress"] = 100
        page_scrape_state["status_text"] = "完成"

        if "error" in result and not result.get("url"):
            page_scrape_state["error"] = result["error"]
            page_scrape_state["running"] = False
            return jsonify({"ok": False, "message": result["error"]}), 500

        page_scrape_state["results"] = [result]

        return jsonify({"ok": True, "result": result})

    except Exception as e:
        page_scrape_state["error"] = str(e)
        page_scrape_state["running"] = False
        return jsonify({"ok": False, "message": f"页面采集异常: {str(e)}"}), 500
    finally:
        page_scrape_state["running"] = False


@app.route("/api/page/scrape_batch", methods=["POST"])
def api_page_scrape_batch():
    """
    批量爬取多个页面

    请求: {
        "host": "...", "username": "...", "password": "...",
        "routes": ["/elderly/checkin", "http://...", "/health/heartrate"],
        // 或 "routes": "all" 表示全部
    }
    """
    global page_scrape_state

    if page_scrape_state["running"]:
        return jsonify({"ok": False, "message": "已有页面采集任务正在运行"}), 409

    data = request.get_json()
    host = data.get("host", "").strip()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    routes = data.get("routes", None)

    if not host or not username:
        return jsonify({"ok": False, "message": "请填写平台地址和账号"}), 400

    if routes == "all" or routes is None:
        routes = [r["route"] for r in KNOWN_ROUTES]
    if not isinstance(routes, list) or len(routes) == 0:
        return jsonify({"ok": False, "message": "请提供有效的网址列表"}), 400

    total = len(routes)

    # 后台线程执行批量爬取（逐步更新进度）
    def _batch_run():
        global page_scrape_state
        page_scrape_state["running"] = True
        page_scrape_state["progress"] = 0
        page_scrape_state["status_text"] = f"批量采集 0/{total}..."
        page_scrape_state["results"] = []
        page_scrape_state["error"] = ""

        scraper = None
        try:
            # 登录一次，然后逐个采集
            scraper = PageScraper(headless=True)
            if not scraper.login(host, username, password):
                page_scrape_state["error"] = "登录失败"
                return
            for idx, route in enumerate(routes):
                try:
                    page_scrape_state["status_text"] = f"采集 {idx+1}/{total}: {route}"
                    page_scrape_state["current_route"] = route
                    if route.startswith("http://") or route.startswith("https://"):
                        content = scraper.scrape_page(route)
                    else:
                        content = scraper.scrape_by_route(route)
                    page_scrape_state["results"].append(content.to_dict(include_screenshot=False))
                except Exception as e:
                    logger.error(f"采集 {route} 失败: {e}")
                    page_scrape_state["results"].append({"error": str(e), "url": route, "route": route})
                page_scrape_state["progress"] = int((idx + 1) / total * 100)
            page_scrape_state["status_text"] = f"批量采集完成，共 {len(page_scrape_state['results'])} 个页面"
        except Exception as e:
            page_scrape_state["error"] = str(e)
            page_scrape_state["status_text"] = f"出错: {e}"
        finally:
            if scraper:
                try:
                    scraper.close()
                except Exception:
                    pass
            page_scrape_state["running"] = False

    thread = threading.Thread(target=_batch_run, daemon=True)
    thread.start()

    return jsonify({"ok": True, "message": f"批量采集已启动，共 {total} 个页面"})


@app.route("/api/page/scrape_status")
def api_page_scrape_status():
    """查询页面采集进度"""
    return jsonify({
        "running": page_scrape_state["running"],
        "progress": page_scrape_state["progress"],
        "status_text": page_scrape_state["status_text"],
        "current_route": page_scrape_state["current_route"],
        "results_count": len(page_scrape_state["results"]),
        "error": page_scrape_state["error"],
        # 返回结果的摘要（不含完整数据）
        "summary": [
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "error": r.get("error", ""),
                "images": len(r.get("images", [])),
                "links": len(r.get("links", [])),
                "tables": len(r.get("tables", [])),
                "text_length": r.get("metadata", {}).get("text_length", 0),
            }
            for r in page_scrape_state["results"]
        ] if page_scrape_state["results"] else [],
    })


@app.route("/api/page/scrape_result/<int:index>")
def api_page_scrape_result(index):
    """获取单个页面的完整采集结果"""
    results = page_scrape_state["results"]
    if index < 0 or index >= len(results):
        return jsonify({"ok": False, "message": "无效的索引"}), 404
    return jsonify({"ok": True, "result": results[index]})


@app.route("/api/page/scrape_export", methods=["POST"])
def api_page_scrape_export():
    """
    导出页面采集结果

    参数:
        format: csv/json/xlsx/txt
        page_index: 页面索引（默认 0）
        category: 分类过滤 —— tables/text/images/links/forms/structure/overview
                  overview 或不传 = 导出全部
    """
    data = request.get_json()
    fmt = data.get("format", "json").lower()
    page_index = data.get("page_index", 0)
    category = data.get("category", "overview").lower()

    results = page_scrape_state["results"]

    if not results:
        return jsonify({"ok": False, "message": "没有可导出的数据"}), 400

    if page_index is not None:
        if page_index < 0 or page_index >= len(results):
            return jsonify({"ok": False, "message": "无效的页面索引"}), 404
        to_export = [results[page_index]]
    else:
        to_export = results

    # 分类名称映射
    cat_names = {
        "tables": "表格", "text": "文本", "images": "图片",
        "links": "链接", "forms": "表单", "structure": "结构",
        "overview": "全部",
    }
    cat_label = cat_names.get(category, category)
    name_hint = f"{category}_{page_index}" if page_index is not None else f"{category}_all"

    if fmt == "json":
        return _export_category_json(to_export, category, name_hint, cat_label)
    elif fmt == "csv":
        return _export_category_csv(to_export, category, name_hint, cat_label)
    elif fmt == "xlsx":
        return _export_category_xlsx(to_export, category, name_hint, cat_label)
    elif fmt == "txt":
        return _export_category_txt(to_export, category, name_hint, cat_label)
    else:
        return jsonify({"ok": False, "message": f"不支持的格式: {fmt}"}), 400


@app.route("/api/page/scrape_paginated", methods=["POST"])
def api_page_scrape_paginated():
    """
    分页爬取 —— 支持指定页码范围和每页条数

    参数:
        host: 平台地址
        username, password: 登录凭证
        route_or_url: 页面路由或完整 URL
        page_start: 起始页码 (默认 1)
        page_end: 结束页码 (默认 None = 自动全部)
        page_size: 每页条数 (默认 None = 保持默认, 可选 10/20/30/50)
    """
    global page_scrape_state

    if page_scrape_state["running"]:
        return jsonify({"ok": False, "message": "正在采集，请等待完成"}), 409

    data = request.get_json()
    host = data.get("host", "").rstrip("/")
    username = data.get("username", "admin")
    password = data.get("password", "admin123")
    route_or_url = data.get("route_or_url", "")
    page_start = int(data.get("page_start", 1))
    page_end = data.get("page_end")
    if page_end is not None:
        page_end = int(page_end)
    page_size = data.get("page_size")
    if page_size is not None:
        page_size = int(page_size)
    fields = data.get("fields", None)  # 可选：只保留指定字段列

    if not host or not route_or_url:
        return jsonify({"ok": False, "message": "平台地址和页面路由不能为空"}), 400

    # 后台线程执行
    def _do_paginated_scrape():
        global page_scrape_state
        page_scrape_state["running"] = True
        page_scrape_state["progress"] = 5
        page_scrape_state["status_text"] = "正在启动浏览器..."
        page_scrape_state["current_route"] = route_or_url
        page_scrape_state["results"] = []
        page_scrape_state["paginated_data"] = None
        page_scrape_state["error"] = ""

        try:
            page_scrape_state["progress"] = 20
            page_scrape_state["status_text"] = "正在登录并爬取..."

            result = scrape_paginated(
                host=host,
                username=username,
                password=password,
                route_or_url=route_or_url,
                page_start=page_start,
                page_end=page_end,
                page_size=page_size,
                headless=True,
                fields=fields,
            )

            if result.get("error"):
                page_scrape_state["error"] = result["error"]
                page_scrape_state["running"] = False
                page_scrape_state["progress"] = 100
                page_scrape_state["status_text"] = "出错"
                return

            page_scrape_state["paginated_data"] = result
            page_scrape_state["results"] = result.get("pages", [])
            page_scrape_state["progress"] = 100
            page_scrape_state["status_text"] = (
                f"分页采集完成：共 {result.get('total_pages_scraped', 0)} 页，"
                f"{len(result.get('merged_tables', []))} 个表格"
            )

            # 自动保存到磁盘
            _save_paginated_result(host, route_or_url, result)

        except Exception as e:
            page_scrape_state["error"] = str(e)
            page_scrape_state["status_text"] = f"出错: {e}"
        finally:
            page_scrape_state["running"] = False

    t = threading.Thread(target=_do_paginated_scrape, daemon=True)
    t.start()

    page_scrape_state["paginated_data"] = None
    return jsonify({"ok": True, "message": "分页采集已启动"})


@app.route("/api/page/scrape_paginated_status")
def api_page_scrape_paginated_status():
    """获取分页爬取状态（含合并数据摘要）"""
    pd = page_scrape_state.get("paginated_data")
    summary = {}
    if pd:
        summary = {
            "total_pages_scraped": pd.get("total_pages_scraped", 0),
            "pagination_info": pd.get("pagination_info", {}),
            "merged_tables_count": len(pd.get("merged_tables", [])),
            "merged_links_count": len(pd.get("merged_links", [])),
            "merged_images_count": len(pd.get("merged_images", [])),
            "merged_forms_count": len(pd.get("merged_forms", [])),
            "merged_structure_count": len(pd.get("merged_structure", [])),
            "merged_text_length": len(pd.get("merged_text", "")),
            "title": pd.get("title", ""),
            "url": pd.get("url", ""),
        }
    return jsonify({
        "running": page_scrape_state["running"],
        "progress": page_scrape_state["progress"],
        "status_text": page_scrape_state["status_text"],
        "current_route": page_scrape_state["current_route"],
        "error": page_scrape_state["error"],
        "has_paginated_data": pd is not None,
        "summary": summary,
    })


@app.route("/api/page/available_fields")
def api_page_available_fields():
    """
    获取当前采集结果中所有表格的字段名（去重），供字段筛选使用

    返回: {"ok": true, "fields": ["姓名", "年龄", "血氧", ...]}
    """
    # 优先用分页数据，其次用单页数据
    pd = page_scrape_state.get("paginated_data")
    results = page_scrape_state.get("results", [])

    all_headers = set()
    if pd:
        for t in pd.get("merged_tables", []):
            for h in t.get("headers", []):
                h = h.strip()
                if h:
                    all_headers.add(h)
    if not all_headers and results:
        for r in results:
            for t in r.get("tables", []):
                for h in t.get("headers", []):
                    h = h.strip()
                    if h:
                        all_headers.add(h)

    return jsonify({
        "ok": True,
        "fields": sorted(all_headers),
        "count": len(all_headers),
    })


@app.route("/api/page/scrape_paginated_data")
def api_page_scrape_paginated_data():
    """
    获取分页爬取结果的分类数据（用于前端预览）

    参数:
        category: overview/text/tables/images/links/forms/structure — 默认 overview
    """
    pd = page_scrape_state.get("paginated_data")
    if not pd:
        return jsonify({"ok": False, "message": "没有分页采集数据"}), 400

    category = request.args.get("category", "overview").lower()

    if category == "overview":
        result = {
            "title": pd.get("title", ""),
            "url": pd.get("url", ""),
            "total_pages_scraped": pd.get("total_pages_scraped", 0),
            "pagination_info": pd.get("pagination_info", {}),
            "images_count": len(pd.get("merged_images", [])),
            "links_count": len(pd.get("merged_links", [])),
            "tables_count": len(pd.get("merged_tables", [])),
            "forms_count": len(pd.get("merged_forms", [])),
            "structure_count": len(pd.get("merged_structure", [])),
            "text_length": len(pd.get("merged_text", "")),
        }
    elif category == "text":
        result = {"text": pd.get("merged_text", "")}
    elif category == "images":
        result = {"images": pd.get("merged_images", [])}
    elif category == "links":
        result = {"links": pd.get("merged_links", [])}
    elif category == "tables":
        result = {"tables": pd.get("merged_tables", [])}
    elif category == "forms":
        result = {"forms": pd.get("merged_forms", [])}
    elif category == "structure":
        result = {"sections": pd.get("merged_structure", [])}
    else:
        return jsonify({"ok": False, "message": f"未知分类: {category}"}), 400

    return jsonify({"ok": True, "category": category, "data": result})


@app.route("/api/page/scrape_paginated_export", methods=["POST"])
def api_page_scrape_paginated_export():
    """
    导出分页爬取结果

    参数:
        format: csv/json/xlsx/txt
        source: "merged" (合并数据) / "pages" (每页单独数据) — 默认 "merged"
        category: tables/text/images/links/forms/structure/overview
    """
    pd = page_scrape_state.get("paginated_data")
    if not pd:
        return jsonify({"ok": False, "message": "没有分页采集数据"}), 400

    data = request.get_json()
    fmt = data.get("format", "csv").lower()
    source = data.get("source", "merged").lower()
    category = data.get("category", "overview").lower()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if source == "pages":
        to_export = pd.get("pages", [])
        name_hint = f"pages_{timestamp}"
    else:
        if category == "tables":
            to_export = pd.get("merged_tables", [])
        elif category == "links":
            to_export = pd.get("merged_links", [])
        elif category == "images":
            to_export = pd.get("merged_images", [])
        elif category == "forms":
            to_export = pd.get("merged_forms", [])
        elif category == "structure":
            to_export = pd.get("merged_structure", [])
        elif category == "text":
            to_export = pd.get("merged_text", "")
        else:
            # overview: 导出合并后的全部数据
            merged_tables = pd.get("merged_tables", [])
            merged_links = pd.get("merged_links", [])
            merged_images = pd.get("merged_images", [])
            merged_forms = pd.get("merged_forms", [])
            merged_text = pd.get("merged_text", "")
            to_export = {
                "title": pd.get("title", ""),
                "url": pd.get("url", ""),
                "total_pages_scraped": pd.get("total_pages_scraped", 0),
                "pagination_info": pd.get("pagination_info", {}),
                "merged_tables": merged_tables,
                "merged_links": merged_links,
                "merged_images": merged_images,
                "merged_forms": merged_forms,
                "merged_text": merged_text,
            }
        name_hint = f"merged_{category}_{timestamp}"

    if fmt == "json":
        output = json.dumps(to_export, ensure_ascii=False, indent=2)
        buf = io.BytesIO(output.encode("utf-8"))
        buf.seek(0)
        return send_file(buf, mimetype="application/json", as_attachment=True,
                         download_name=f"page_scrape_{name_hint}.json")
    elif fmt == "csv":
        if category == "tables" and isinstance(to_export, list):
            return _export_merged_tables_csv(to_export, name_hint)
        elif category == "overview":
            # overview 导出：合并所有表格为 CSV
            merged = pd.get("merged_tables", [])
            return _export_merged_tables_csv(merged, name_hint)
        return _generic_csv(to_export, name_hint)
    elif fmt == "xlsx":
        if category == "overview":
            # overview 导出：合并所有表格为 Excel
            merged = pd.get("merged_tables", [])
            return _generic_xlsx(merged, name_hint)
        return _generic_xlsx(to_export, name_hint)
    elif fmt == "txt":
        if category == "overview":
            # overview 导出 TXT：包含表格和文本
            return _export_overview_txt(pd, name_hint, timestamp)
        return _generic_txt(to_export, name_hint)
    else:
        return jsonify({"ok": False, "message": f"不支持的格式: {fmt}"}), 400


def _save_paginated_result(host, route_or_url, result):
    """自动保存分页结果到磁盘"""
    try:
        os.makedirs(scrape_results_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = route_or_url.replace("/", "_").replace(":", "_").strip("_") or "index"
        base_name = f"{timestamp}_{safe_name}_paginated"

        json_path = os.path.join(scrape_results_dir, f"{base_name}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        logger.info("分页结果已保存: %s", json_path)

        txt_path = os.path.join(scrape_results_dir, f"{base_name}.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"分页爬取结果\n")
            f.write(f"页面: {host}{route_or_url}\n")
            f.write(f"时间: {datetime.now().isoformat()}\n")
            f.write(f"共 {result.get('total_pages_scraped', 0)} 页\n")
            f.write(f"分页信息: {json.dumps(result.get('pagination_info', {}), ensure_ascii=False)}\n")
            f.write(f"合并表格: {len(result.get('merged_tables', []))} 个\n")
            f.write(f"合并链接: {len(result.get('merged_links', []))} 个\n")
            f.write(f"合并图片: {len(result.get('merged_images', []))} 个\n")
            f.write("\n" + "=" * 60 + "\n")
            f.write(result.get("merged_text", ""))
        logger.info("TXT 可读版已保存: %s", txt_path)
    except Exception as e:
        logger.error("保存分页结果失败: %s", e)


def _export_merged_tables_csv(tables, name_hint):
    """导出合并表格数据为 CSV（对齐列数，防止窜列）"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = io.StringIO()
    writer = csv.writer(output)

    if not tables:
        writer.writerow(["无数据"])
    else:
        # 先计算全局最大列数，用于对齐所有行
        max_cols = 0
        for t in tables:
            hc = len(t.get("headers", []))
            for row in t.get("rows", []):
                hc = max(hc, len(row))
            max_cols = max(max_cols, hc)

        for i, t in enumerate(tables):
            if i > 0:
                # 空行也填充到相同列数
                writer.writerow([""] * max(1, max_cols))

            caption = t.get("caption", f"表格 {i+1}")
            # 标题行：标题放第一列，其余填充空字符串
            caption_row = [caption] + [""] * (max(1, max_cols) - 1)
            writer.writerow(caption_row)

            headers = t.get("headers", [])
            if headers:
                # 补齐到 max_cols
                padded_headers = list(headers) + [""] * (max_cols - len(headers))
                writer.writerow(padded_headers)

            for row in t.get("rows", []):
                padded_row = list(row) + [""] * (max_cols - len(row))
                writer.writerow(padded_row)
    output.seek(0)
    buf = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    buf.seek(0)
    return send_file(buf, mimetype="text/csv", as_attachment=True,
                     download_name=f"page_scrape_{name_hint}_{timestamp}.csv")


def _generic_csv(data, name_hint):
    """通用 CSV 导出"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = io.StringIO()
    writer = csv.writer(output)
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                writer.writerow(item.keys())
                writer.writerow([str(v) for v in item.values()])
                break
            else:
                writer.writerow([str(item)])
        for item in data:
            if isinstance(item, dict):
                writer.writerow([str(v) for v in item.values()])
            else:
                writer.writerow([str(item)])
    elif isinstance(data, str):
        writer.writerow([data])
    output.seek(0)
    buf = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    buf.seek(0)
    return send_file(buf, mimetype="text/csv", as_attachment=True,
                     download_name=f"page_scrape_{name_hint}_{timestamp}.csv")


def _generic_xlsx(data, name_hint):
    """通用 Excel 导出"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    buf = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = "数据"
    if isinstance(data, list) and len(data) > 0:
        if isinstance(data[0], dict):
            keys = list(data[0].keys())
            ws.append(keys)
            for item in data:
                ws.append([str(item.get(k, "")) for k in keys])
        else:
            for item in data:
                ws.append([str(item)])
    elif isinstance(data, str):
        ws.append([data])
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"page_scrape_{name_hint}_{timestamp}.xlsx")


def _generic_txt(data, name_hint):
    """通用 TXT 导出"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if isinstance(data, str):
        text = data
    else:
        text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    buf = io.BytesIO(text.encode("utf-8"))
    buf.seek(0)
    return send_file(buf, mimetype="text/plain", as_attachment=True,
                     download_name=f"page_scrape_{name_hint}_{timestamp}.txt")


def _export_overview_txt(pd, name_hint, timestamp):
    """导出 overview 为可读 TXT"""
    lines = []
    lines.append("=" * 60)
    lines.append(f"分页采集概览")
    lines.append("=" * 60)
    lines.append(f"页面标题: {pd.get('title', '-')}")
    lines.append(f"页面URL: {pd.get('url', '-')}")
    lines.append(f"爬取页数: {pd.get('total_pages_scraped', 0)}")
    pi = pd.get("pagination_info", {})
    lines.append(f"总记录数: {pi.get('total_items', '-')}")
    lines.append(f"每页条数: {pi.get('page_size', '-')}")
    lines.append("")

    tables = pd.get("merged_tables", [])
    for i, t in enumerate(tables):
        lines.append(f"{'=' * 60}")
        lines.append(f"表格 {i+1}: {t.get('caption', '')}")
        lines.append(f"{'=' * 60}")
        headers = t.get("headers", [])
        if headers:
            lines.append(" | ".join(headers))
            lines.append("-" * 40)
        for row in t.get("rows", []):
            lines.append(" | ".join(str(c) for c in row))
        lines.append("")

    lines.append(f"{'=' * 60}")
    merged_text = pd.get("merged_text", "")
    if merged_text:
        lines.append("页面文本内容")
        lines.append(f"{'=' * 60}")
        lines.append(merged_text)

    buf = io.BytesIO("\n".join(lines).encode("utf-8"))
    buf.seek(0)
    return send_file(buf, mimetype="text/plain", as_attachment=True,
                     download_name=f"page_scrape_{name_hint}_{timestamp}.txt")


def _export_page_json(results, name_hint):
    """导出页面采集结果 -> JSON"""
    output = json.dumps(results, ensure_ascii=False, indent=2)
    buf = io.BytesIO(output.encode("utf-8"))
    buf.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf, mimetype="application/json", as_attachment=True,
        download_name=f"page_scrape_{name_hint}_{timestamp}.json"
    )


def _export_page_csv(results, name_hint):
    """导出页面采集概览 -> CSV（分类汇总）"""
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)

    for i, r in enumerate(results):
        writer.writerow([f"# 页面 {i+1}: {r.get('title', '')}"])
        writer.writerow([f"URL", r.get("url", "")])
        writer.writerow([])

        # 图片
        images = r.get("images", [])
        if images:
            writer.writerow(["── 图片（{} 张）──".format(len(images))])
            writer.writerow(["序号", "URL", "描述"])
            for j, img in enumerate(images):
                writer.writerow([j + 1, img.get("src", ""), img.get("alt", "")])
            writer.writerow([])

        # 链接
        links = r.get("links", [])
        if links:
            writer.writerow(["── 链接（{} 个）──".format(len(links))])
            writer.writerow(["序号", "URL", "文本", "类型"])
            for j, link in enumerate(links):
                writer.writerow([j + 1, link.get("href", ""), link.get("text", ""), link.get("link_type", "")])
            writer.writerow([])

        # 表格
        tables = r.get("tables", [])
        if tables:
            writer.writerow(["── 表格（{} 个）──".format(len(tables))])
            for t_idx, tbl in enumerate(tables):
                writer.writerow([f"表格 {t_idx+1}: {tbl.get('caption', '')} ({tbl.get('row_count', 0)} 行 x {tbl.get('col_count', 0)} 列)"])
                headers = tbl.get("headers", [])
                if headers:
                    writer.writerow(headers)
                for row in tbl.get("rows", []):
                    writer.writerow(row)
                writer.writerow([])

        # 纯文本
        raw_text = r.get("raw_text", "")
        if raw_text:
            writer.writerow(["── 页面文本内容（{} 字符）──".format(len(raw_text))])
            for line in raw_text.split("\n")[:100]:
                if line.strip():
                    writer.writerow([line.strip()])
        writer.writerow([])
        writer.writerow(["=" * 50])

    buf = io.BytesIO()
    buf.write(output.getvalue().encode("utf-8"))
    buf.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf, mimetype="text/csv", as_attachment=True,
        download_name=f"page_scrape_{name_hint}_{timestamp}.csv"
    )


def _export_page_xlsx(results, name_hint):
    """导出页面采集结果 -> Excel（多Sheet：概览/图片/链接/表格/文本）"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="2B579A", end_color="2B579A", fill_type="solid")
    title_font = Font(bold=True, size=12, color="1F4E79")

    for p_idx, page in enumerate(results):
        page_title = page.get("title", "Page")[:20] or f"页面{p_idx+1}"

        # ── Sheet 1: 概览 ──
        ws = wb.create_sheet(title=f"{page_title[:25]}-概览")
        ws.append(["页面标题", page.get("title", "")])
        ws.append(["URL", page.get("url", "")])
        ws.append(["采集时间", page.get("timestamp", "")])
        ws.append(["图片数量", len(page.get("images", []))])
        ws.append(["链接数量", len(page.get("links", []))])
        ws.append(["表格数量", len(page.get("tables", []))])
        ws.append(["文本长度", page.get("metadata", {}).get("text_length", 0)])
        ws.column_dimensions['A'].width = 15
        ws.column_dimensions['B'].width = 80

        # ── Sheet 2: 图片 ──
        images = page.get("images", [])
        if images:
            ws2 = wb.create_sheet(title=f"{page_title[:25]}-图片")
            ws2.append(["序号", "URL", "描述", "宽度", "高度"])
            for ci, col in enumerate(["序号", "URL", "描述", "宽度", "高度"], 1):
                ws2.cell(row=1, column=ci).font = header_font
                ws2.cell(row=1, column=ci).fill = header_fill
            for i, img in enumerate(images):
                ws2.append([i + 1, img.get("src", ""), img.get("alt", ""),
                           img.get("width", ""), img.get("height", "")])
            ws2.column_dimensions['B'].width = 80

        # ── Sheet 3: 链接 ──
        links = page.get("links", [])
        if links:
            ws3 = wb.create_sheet(title=f"{page_title[:25]}-链接")
            ws3.append(["序号", "URL", "文本", "类型"])
            for ci in range(1, 5):
                ws3.cell(row=1, column=ci).font = header_font
                ws3.cell(row=1, column=ci).fill = header_fill
            for i, link in enumerate(links):
                ws3.append([i + 1, link.get("href", ""), link.get("text", ""),
                           link.get("link_type", "")])
            ws3.column_dimensions['B'].width = 80

        # ── Sheet 4: 表格 ──
        tables = page.get("tables", [])
        if tables:
            ws4 = wb.create_sheet(title=f"{page_title[:25]}-表格")
            row_offset = 1
            for t_idx, tbl in enumerate(tables):
                cap = tbl.get("caption", f"表格{t_idx+1}")
                ws4.merge_cells(start_row=row_offset, start_column=1,
                               end_row=row_offset,
                               end_column=max(len(tbl.get("headers", [])), 1))
                ws4.cell(row=row_offset, column=1, value=cap).font = title_font
                row_offset += 1

                headers = tbl.get("headers", [])
                if headers:
                    for ci, h in enumerate(headers, 1):
                        c = ws4.cell(row=row_offset, column=ci, value=h)
                        c.font = header_font
                        c.fill = header_fill
                    row_offset += 1

                for row in tbl.get("rows", []):
                    for ci, val in enumerate(row, 1):
                        ws4.cell(row=row_offset, column=ci, value=str(val)[:500])
                    row_offset += 1
                row_offset += 1  # 空行
            ws4.column_dimensions['A'].width = 30

        # ── Sheet 5: 页面文本 ──
        raw_text = page.get("raw_text", "")
        if raw_text:
            ws5 = wb.create_sheet(title=f"{page_title[:25]}-文本")
            lines = raw_text.split("\n")
            for i, line in enumerate(lines[:500], 1):
                if line.strip():
                    ws5.cell(row=i, column=1, value=line.strip())
            ws5.column_dimensions['A'].width = 100

    # 删除默认 Sheet
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=f"page_scrape_{name_hint}_{timestamp}.xlsx"
    )


def _export_page_txt(results, name_hint):
    """导出页面采集结果 -> TXT"""
    output = io.StringIO()
    output.write("=" * 60 + "\n")
    output.write("  康养平台页面完整采集结果\n")
    output.write(f"  导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    output.write(f"  页面数量: {len(results)}\n")
    output.write("=" * 60 + "\n\n")

    for i, page in enumerate(results):
        output.write(f"\n{'─' * 60}\n")
        output.write(f"  页面 {i + 1}: {page.get('title', '无标题')}\n")
        output.write(f"  URL: {page.get('url', '')}\n")
        output.write(f"{'─' * 60}\n\n")

        if page.get("error"):
            output.write(f"  错误: {page['error']}\n")
            continue

        # 文本内容
        raw_text = page.get("raw_text", "")
        if raw_text:
            output.write("── 页面文本 ──\n\n")
            for line in raw_text.split("\n")[:200]:
                if line.strip():
                    output.write(f"  {line.strip()}\n")
            output.write("\n")

        # 图片
        images = page.get("images", [])
        if images:
            output.write(f"── 图片（{len(images)} 张）──\n")
            for j, img in enumerate(images):
                output.write(f"  {j+1}. {img.get('src', '')}\n")
                if img.get("alt"):
                    output.write(f"     描述: {img['alt']}\n")
            output.write("\n")

        # 链接
        links = page.get("links", [])
        if links:
            output.write(f"── 链接（{len(links)} 个）──\n")
            for j, link in enumerate(links):
                output.write(f"  {j+1}. [{link.get('link_type', '')}] {link.get('text', '')}\n")
                output.write(f"      {link.get('href', '')}\n")
            output.write("\n")

        # 表格
        tables = page.get("tables", [])
        if tables:
            output.write(f"── 表格（{len(tables)} 个）──\n")
            for t_idx, tbl in enumerate(tables):
                output.write(f"\n  表格 {t_idx+1}: {tbl.get('caption', '')} ({tbl.get('row_count', 0)} 行)\n")
                headers = tbl.get("headers", [])
                if headers:
                    output.write("  | " + " | ".join(headers) + " |\n")
                for row in tbl.get("rows", [])[:50]:
                    output.write("  | " + " | ".join([str(v)[:60] for v in row]) + " |\n")
                if tbl.get("row_count", 0) > 50:
                    output.write(f"  ... 还有 {tbl['row_count'] - 50} 行\n")
            output.write("\n")

        # 页面结构
        sections = page.get("sections", [])
        if sections:
            output.write(f"── 页面结构（{len(sections)} 个元素）──\n")
            for sec in sections[:100]:
                stype = sec.get("section_type", "")
                text = sec.get("text", "")
                level = sec.get("level", 0)
                if stype == "heading":
                    prefix = "#" * level
                    output.write(f"  {prefix} {text}\n")
                elif stype == "paragraph":
                    output.write(f"  {text[:200]}\n")
                elif stype == "list":
                    for child in sec.get("children", []):
                        output.write(f"  - {child}\n")
            output.write("\n")

        output.write("\n")

    buf = io.BytesIO()
    buf.write(output.getvalue().encode("utf-8"))
    buf.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf, mimetype="text/plain", as_attachment=True,
        download_name=f"page_scrape_{name_hint}_{timestamp}.txt"
    )


# ═══════════════ 导出实现 ═══════════════

def _export_csv(data_list, name_hint, utf8_bom):
    """导出 CSV —— 多张表合并到一个文件，每张表前加标题行"""
    output = io.StringIO()

    # UTF-8 BOM 让 Excel 能正确打开中文
    if utf8_bom:
        output.write("\ufeff")

    writer = csv.writer(output)

    for item in data_list:
        columns = item.get("columns", [])
        rows = item.get("rows", [])

        # 表名标题
        writer.writerow([f"# {item['table_name']}（{len(rows)} 条记录）"])
        writer.writerow(columns)

        for row in rows:
            writer.writerow([row.get(col, "") for col in columns])
        writer.writerow([])  # 空行分隔

    buf = io.BytesIO()
    buf.write(output.getvalue().encode("utf-8"))
    buf.seek(0)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf, mimetype="text/csv", as_attachment=True,
        download_name=f"kangyang_{name_hint}_{timestamp}.csv"
    )


def _export_json(data_list, name_hint):
    """导出 JSON"""
    export_data = {
        "export_time": datetime.now().isoformat(),
        "total_tables": len(data_list),
        "total_records": sum(len(r.get("rows", [])) for r in data_list),
        "tables": [
            {
                "table_name": r["table_name"],
                "module": r.get("module", ""),
                "count": r["count"],
                "columns": r["columns"],
                "rows": r.get("rows", []),
            }
            for r in data_list
        ],
    }
    output = json.dumps(export_data, ensure_ascii=False, indent=2)
    buf = io.BytesIO(output.encode("utf-8"))
    buf.seek(0)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf, mimetype="application/json", as_attachment=True,
        download_name=f"kangyang_{name_hint}_{timestamp}.json"
    )


def _export_xlsx(data_list, name_hint):
    """导出 Excel —— 每张表一个 Sheet"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    wb.remove(wb.active)  # 删除默认 Sheet

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="2B579A", end_color="2B579A", fill_type="solid")
    title_font = Font(bold=True, size=12, color="1F4E79")

    for item in data_list:
        sheet_name = item["table_name"][:31]  # Excel Sheet 名最多31字符
        ws = wb.create_sheet(title=sheet_name)

        columns = item.get("columns", [])
        rows = item.get("rows", [])

        # 标题行
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(len(columns), 1))
        title_cell = ws.cell(row=1, column=1, value=f"{item['table_name']}（共 {len(rows)} 条）")
        title_cell.font = title_font
        title_cell.alignment = Alignment(horizontal="center")

        # 表头
        for ci, col in enumerate(columns, 1):
            cell = ws.cell(row=2, column=ci, value=col)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")

        # 数据
        for ri, row in enumerate(rows, 3):
            for ci, col in enumerate(columns, 1):
                val = row.get(col, "")
                ws.cell(row=ri, column=ci, value=val)

        # 自动列宽
        for ci, col in enumerate(columns, 1):
            max_len = len(str(col))
            for row in rows[:50]:
                val = str(row.get(col, ""))
                if len(val) > max_len:
                    max_len = len(val)
            ws.column_dimensions[ws.cell(row=2, column=ci).column_letter].width = min(max_len + 4, 60)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=f"kangyang_{name_hint}_{timestamp}.xlsx"
    )


def _export_txt(data_list, name_hint):
    """导出 TXT —— 可读文本格式"""
    output = io.StringIO()

    output.write("=" * 60 + "\n")
    output.write("  康养平台数据采集结果\n")
    output.write(f"  导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    output.write("=" * 60 + "\n\n")

    for item in data_list:
        columns = item.get("columns", [])
        rows = item.get("rows", [])

        output.write(f"\n{'─' * 60}\n")
        output.write(f"  [{item['table_name']}] 共 {len(rows)} 条记录\n")
        output.write(f"{'─' * 60}\n\n")

        if not rows:
            output.write("  （无数据）\n")
            continue

        # 字段名
        output.write("  | " + " | ".join(columns) + " |\n")

        # 数据行
        for row in rows[:500]:  # TXT 最多 500 条
            values = [str(row.get(col, ""))[:60] for col in columns]
            output.write("  | " + " | ".join(values) + " |\n")

        if len(rows) > 500:
            output.write(f"\n  ... 还有 {len(rows) - 500} 条记录未显示\n")

    buf = io.BytesIO()
    buf.write(output.getvalue().encode("utf-8"))
    buf.seek(0)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf, mimetype="text/plain", as_attachment=True,
        download_name=f"kangyang_{name_hint}_{timestamp}.txt"
    )


# ═══════════════ 分类导出（页面采集） ═══════════════

def _filter_by_category(results, category):
    """从完整结果中提取指定分类的数据"""
    filtered = []
    for page in results:
        item = {
            "title": page.get("title", ""),
            "url": page.get("url", ""),
            "timestamp": page.get("timestamp", ""),
            "category": category,
        }
        if category == "tables":
            item["tables"] = page.get("tables", [])
        elif category == "text":
            item["raw_text"] = page.get("raw_text", "")
            item["sections"] = [s for s in page.get("sections", [])
                                if s.get("section_type") in ("heading", "paragraph")]
        elif category == "images":
            item["images"] = page.get("images", [])
        elif category == "links":
            item["links"] = page.get("links", [])
        elif category == "forms":
            item["forms"] = page.get("forms", [])
        elif category == "structure":
            item["sections"] = page.get("sections", [])
        else:  # overview
            item.update(page)
        filtered.append(item)
    return filtered


def _export_category_json(results, category, name_hint, cat_label):
    """分类导出 JSON"""
    filtered = _filter_by_category(results, category)
    output = json.dumps(filtered, ensure_ascii=False, indent=2)
    buf = io.BytesIO(output.encode("utf-8"))
    buf.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf, mimetype="application/json", as_attachment=True,
        download_name=f"page_{category}_{timestamp}.json"
    )


def _export_category_csv(results, category, name_hint, cat_label):
    """分类导出 CSV —— 只含当前分类的数据"""
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)

    for page in results:
        title = page.get("title", "")
        url = page.get("url", "")
        writer.writerow([f"# 页面: {title}"])
        writer.writerow([f"# URL: {url}"])
        writer.writerow([])

        if category == "tables":
            tables = page.get("tables", [])
            if not tables:
                writer.writerow(["（无表格数据）"])
                writer.writerow([])
                continue
            for t_idx, tbl in enumerate(tables):
                writer.writerow([f"表格 {t_idx+1}: {tbl.get('caption', '')} "
                                 f"({tbl.get('row_count', 0)} 行 x {tbl.get('col_count', 0)} 列)"])
                headers = tbl.get("headers", [])
                if headers:
                    writer.writerow(headers)
                for row in tbl.get("rows", []):
                    writer.writerow(row)
                writer.writerow([])

        elif category == "text":
            raw_text = page.get("raw_text", "")
            sections = page.get("sections", [])
            if sections:
                writer.writerow(["类型", "级别", "内容"])
                for sec in sections:
                    writer.writerow([
                        sec.get("section_type", ""),
                        sec.get("level", ""),
                        sec.get("text", ""),
                    ])
            elif raw_text:
                writer.writerow(["序号", "文本行"])
                for i, line in enumerate(raw_text.split("\n"), 1):
                    if line.strip():
                        writer.writerow([i, line.strip()])
            else:
                writer.writerow(["（无文本数据）"])
            writer.writerow([])

        elif category == "images":
            images = page.get("images", [])
            if not images:
                writer.writerow(["（无图片数据）"])
                writer.writerow([])
                continue
            writer.writerow(["序号", "URL", "描述", "宽度", "高度"])
            for i, img in enumerate(images, 1):
                writer.writerow([i, img.get("src", ""), img.get("alt", ""),
                                img.get("width", ""), img.get("height", "")])
            writer.writerow([])

        elif category == "links":
            links = page.get("links", [])
            if not links:
                writer.writerow(["（无链接数据）"])
                writer.writerow([])
                continue
            writer.writerow(["序号", "URL", "文本", "类型"])
            for i, link in enumerate(links, 1):
                writer.writerow([i, link.get("href", ""), link.get("text", ""),
                                link.get("link_type", "")])
            writer.writerow([])

        elif category == "forms":
            forms = page.get("forms", [])
            if not forms:
                writer.writerow(["（无表单数据）"])
                writer.writerow([])
                continue
            writer.writerow(["序号", "字段标签", "字段名", "类型", "当前值"])
            for i, field in enumerate(forms, 1):
                writer.writerow([i, field.get("label", ""), field.get("name", ""),
                                field.get("type", ""), field.get("value", "")])
            writer.writerow([])

        elif category == "structure":
            sections = page.get("sections", [])
            if not sections:
                writer.writerow(["（无结构数据）"])
                writer.writerow([])
                continue
            writer.writerow(["序号", "类型", "级别", "内容", "子项"])
            for i, sec in enumerate(sections, 1):
                children = sec.get("children", [])
                children_str = "; ".join(children) if children else ""
                writer.writerow([i, sec.get("section_type", ""), sec.get("level", ""),
                                sec.get("text", ""), children_str])
            writer.writerow([])

        else:  # overview = 全部
            writer.writerow(["（请使用概览导出获取全部数据）"])
            writer.writerow([])

        writer.writerow(["=" * 50])

    buf = io.BytesIO()
    buf.write(output.getvalue().encode("utf-8"))
    buf.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf, mimetype="text/csv", as_attachment=True,
        download_name=f"page_{category}_{timestamp}.csv"
    )


def _export_category_xlsx(results, category, name_hint, cat_label):
    """分类导出 Excel —— 只含当前分类的 Sheet"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="2B579A", end_color="2B579A", fill_type="solid")

    for p_idx, page in enumerate(results):
        page_title = page.get("title", f"页面{p_idx+1}")[:20]
        sheet_name = f"{page_title[:20]}-{cat_label}"
        ws = wb.create_sheet(title=sheet_name)

        if category == "tables":
            tables = page.get("tables", [])
            row_off = 1
            for t_idx, tbl in enumerate(tables):
                ws.cell(row=row_off, column=1, value=f"表格 {t_idx+1}: {tbl.get('caption', '')}").font = Font(bold=True)
                row_off += 1
                headers = tbl.get("headers", [])
                if headers:
                    for ci, h in enumerate(headers, 1):
                        c = ws.cell(row=row_off, column=ci, value=h)
                        c.font = header_font; c.fill = header_fill
                    row_off += 1
                for row in tbl.get("rows", []):
                    for ci, val in enumerate(row, 1):
                        ws.cell(row=row_off, column=ci, value=str(val)[:500])
                    row_off += 1
                row_off += 1
            if not tables:
                ws.cell(row=1, column=1, value="无表格数据")
            ws.column_dimensions['A'].width = 30

        elif category == "text":
            raw_text = page.get("raw_text", "")
            sections = page.get("sections", [])
            if sections:
                ws.append(["类型", "级别", "内容"])
                for ci in range(1, 4):
                    ws.cell(row=1, column=ci).font = header_font
                    ws.cell(row=1, column=ci).fill = header_fill
                for sec in sections:
                    ws.append([sec.get("section_type", ""), sec.get("level", ""), sec.get("text", "")])
            elif raw_text:
                ws.append(["序号", "文本行"])
                for ci in range(1, 3):
                    ws.cell(row=1, column=ci).font = header_font
                    ws.cell(row=1, column=ci).fill = header_fill
                for i, line in enumerate(raw_text.split("\n"), 1):
                    if line.strip():
                        ws.append([i, line.strip()])
            ws.column_dimensions['C'].width = 80

        elif category == "images":
            images = page.get("images", [])
            ws.append(["序号", "URL", "描述", "宽度", "高度"])
            for ci in range(1, 6):
                ws.cell(row=1, column=ci).font = header_font
                ws.cell(row=1, column=ci).fill = header_fill
            for i, img in enumerate(images, 1):
                ws.append([i, img.get("src", ""), img.get("alt", ""),
                          img.get("width", ""), img.get("height", "")])
            ws.column_dimensions['B'].width = 80

        elif category == "links":
            links = page.get("links", [])
            ws.append(["序号", "URL", "文本", "类型"])
            for ci in range(1, 5):
                ws.cell(row=1, column=ci).font = header_font
                ws.cell(row=1, column=ci).fill = header_fill
            for i, link in enumerate(links, 1):
                ws.append([i, link.get("href", ""), link.get("text", ""),
                          link.get("link_type", "")])
            ws.column_dimensions['B'].width = 80

        elif category == "forms":
            forms = page.get("forms", [])
            ws.append(["序号", "字段标签", "字段名", "类型", "当前值"])
            for ci in range(1, 6):
                ws.cell(row=1, column=ci).font = header_font
                ws.cell(row=1, column=ci).fill = header_fill
            for i, field in enumerate(forms, 1):
                ws.append([i, field.get("label", ""), field.get("name", ""),
                          field.get("type", ""), field.get("value", "")])
            ws.column_dimensions['B'].width = 25
            ws.column_dimensions['E'].width = 30

        elif category == "structure":
            sections = page.get("sections", [])
            ws.append(["序号", "类型", "级别", "内容", "子项"])
            for ci in range(1, 6):
                ws.cell(row=1, column=ci).font = header_font
                ws.cell(row=1, column=ci).fill = header_fill
            for i, sec in enumerate(sections, 1):
                children = sec.get("children", [])
                ws.append([i, sec.get("section_type", ""), sec.get("level", ""),
                          sec.get("text", ""), "; ".join(children) if children else ""])
            ws.column_dimensions['D'].width = 60

        else:  # overview
            ws.append(["页面标题", page.get("title", "")])
            ws.append(["URL", page.get("url", "")])
            ws.append(["采集时间", page.get("timestamp", "")])
            ws.append(["图片数量", len(page.get("images", []))])
            ws.append(["链接数量", len(page.get("links", []))])
            ws.append(["表格数量", len(page.get("tables", []))])
            ws.append(["文本长度", page.get("metadata", {}).get("text_length", 0)])
            ws.column_dimensions['B'].width = 80

    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=f"page_{category}_{timestamp}.xlsx"
    )


def _export_category_txt(results, category, name_hint, cat_label):
    """分类导出 TXT —— 只含当前分类的数据"""
    output = io.StringIO()
    output.write("=" * 60 + "\n")
    output.write(f"  康养平台页面采集 - {cat_label}\n")
    output.write(f"  导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    output.write(f"  页面数量: {len(results)}\n")
    output.write("=" * 60 + "\n\n")

    for i, page in enumerate(results):
        output.write(f"{'─' * 60}\n")
        output.write(f"  页面 {i+1}: {page.get('title', '无标题')}\n")
        output.write(f"  URL: {page.get('url', '')}\n")
        output.write(f"{'─' * 60}\n\n")

        if category == "tables":
            tables = page.get("tables", [])
            if not tables:
                output.write("  （无表格数据）\n\n")
                continue
            for t_idx, tbl in enumerate(tables):
                output.write(f"  表格 {t_idx+1}: {tbl.get('caption', '')} "
                            f"({tbl.get('row_count', 0)} 行 x {tbl.get('col_count', 0)} 列)\n")
                headers = tbl.get("headers", [])
                if headers:
                    output.write("  | " + " | ".join(headers) + " |\n")
                for row in tbl.get("rows", []):
                    output.write("  | " + " | ".join([str(v)[:60] for v in row]) + " |\n")
                output.write("\n")

        elif category == "text":
            raw_text = page.get("raw_text", "")
            sections = page.get("sections", [])
            if sections:
                for sec in sections:
                    stype = sec.get("section_type", "")
                    if stype == "heading":
                        output.write(f"  {'#' * sec.get('level', 1)} {sec.get('text', '')}\n")
                    elif stype == "paragraph":
                        output.write(f"  {sec.get('text', '')[:200]}\n")
            elif raw_text:
                for line in raw_text.split("\n")[:200]:
                    if line.strip():
                        output.write(f"  {line.strip()}\n")
            output.write("\n")

        elif category == "images":
            images = page.get("images", [])
            if not images:
                output.write("  （无图片数据）\n\n")
                continue
            for j, img in enumerate(images):
                output.write(f"  {j+1}. {img.get('src', '')}\n")
                if img.get("alt"):
                    output.write(f"     描述: {img['alt']}\n")
                output.write(f"     尺寸: {img.get('width', '?')}x{img.get('height', '?')}\n")
            output.write("\n")

        elif category == "links":
            links = page.get("links", [])
            if not links:
                output.write("  （无链接数据）\n\n")
                continue
            for j, link in enumerate(links):
                output.write(f"  {j+1}. [{link.get('link_type', '')}] {link.get('text', '')}\n")
                output.write(f"      {link.get('href', '')}\n")
            output.write("\n")

        elif category == "forms":
            forms = page.get("forms", [])
            if not forms:
                output.write("  （无表单数据）\n\n")
                continue
            output.write(f"  表单字段（{len(forms)} 个）:\n")
            for j, field in enumerate(forms):
                output.write(f"  {j+1}. {field.get('label', '')} [{field.get('type', '')}]"
                            f"  name={field.get('name', '')}  value={field.get('value', '')}\n")
            output.write("\n")

        elif category == "structure":
            sections = page.get("sections", [])
            if not sections:
                output.write("  （无结构数据）\n\n")
                continue
            for sec in sections[:100]:
                stype = sec.get("section_type", "")
                text = sec.get("text", "")
                level = sec.get("level", 0)
                if stype == "heading":
                    output.write(f"  {'#' * level} {text}\n")
                elif stype == "paragraph":
                    output.write(f"  {text[:200]}\n")
                elif stype == "list":
                    for child in sec.get("children", []):
                        output.write(f"  - {child}\n")
            output.write("\n")

    buf = io.BytesIO()
    buf.write(output.getvalue().encode("utf-8"))
    buf.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(
        buf, mimetype="text/plain", as_attachment=True,
        download_name=f"page_{category}_{timestamp}.txt"
    )


# ═══════════════ 可视化采集 API ═══════════════

# 可视化采集会话状态
visual_collector_state = {
    "running": False,
    "session_id": "",
    "snapshot": None,       # PageSnapshot
    "selected_elements": [],  # [(SelectableElement, field_name), ...]
    "host": "",
    "username": "",
    "password": "",
    "target": "",
    # ── 代理模式 ──
    "proxy_active": False,
    "proxy_cookies": {},     # {name: value}
    "proxy_target_origin": "",
    "proxy_storage_state": None,  # context.storage_state() — 完整认证状态（线程安全）
    "proxy_page": None,      # Playwright page 对象（不可跨线程使用！）
    "proxy_browser": None,   # Playwright browser 对象
    "proxy_context": None,   # Playwright context 对象
}

# ── 动态 URL 拦截器（必须在所有 SPA 脚本之前注入）
# 拦截 JS 运行时动态创建的 script/img/link 的 src/href，将根相对路径重写为代理路径
# 解决 webpack 动态 chunk 加载 (/static/js/0.js) 404 的问题
DYNAMIC_URL_INTERCEPTOR_JS = r"""
<script>
(function(){
  if (window.__kwb_url_intercepted) return;
  window.__kwb_url_intercepted = true;

  var PROXY_BASE = '/api/collector/proxy';

  // 目标平台域名（由代理注入），用于判断绝对 URL 是否属于本平台
  window.__kwb_target_origin = window.__kwb_target_origin || '';

  window.__kwb_shouldProxy = function(url) {
    if (!url || typeof url !== 'string') return false;
    if (url.startsWith(PROXY_BASE) || url.startsWith('/api/collector/')) return false;
    if (url.startsWith('blob:') || url.startsWith('data:') || url.startsWith('javascript:')) return false;
    if (url.startsWith('http:') || url.startsWith('https:')) {
      // 仅当属于目标平台域名时才走代理（外链不代理）
      if (window.__kwb_target_origin && url.indexOf(window.__kwb_target_origin) === 0) return true;
      return false;
    }
    if (url.startsWith('//')) return true;            // 协议相对，默认目标域名
    if (url.startsWith('/') && !url.startsWith('//')) return true;
    return false;
  };

  window.__kwb_proxyUrl = function(url) {
    if (url.startsWith('http:') || url.startsWith('https:')) {
      var p = url.substring(url.indexOf('//') + 2);
      var sl = p.indexOf('/');
      var path = sl >= 0 ? p.substring(sl) : '/';
      return PROXY_BASE + path;
    }
    if (url.startsWith('//')) {
      var p2 = url.substring(2);
      var sl2 = p2.indexOf('/');
      var path2 = sl2 >= 0 ? p2.substring(sl2) : '/';
      return PROXY_BASE + path2;
    }
    if (url.startsWith('/') && !url.startsWith('//')) return PROXY_BASE + url;
    return url;
  };

  // 1. Patch HTMLScriptElement.prototype.src
  function patchSrcProperty(proto, propName) {
    try {
      var desc = Object.getOwnPropertyDescriptor(proto, propName);
      if (!desc || !desc.set) {
        // Walk up the prototype chain
        var p = Object.getPrototypeOf(proto);
        while (p) {
          desc = Object.getOwnPropertyDescriptor(p, propName);
          if (desc && desc.set) break;
          p = Object.getPrototypeOf(p);
        }
      }
      if (!desc || !desc.set) return;
      var origSet = desc.set;
      var origGet = desc.get;
      Object.defineProperty(proto, propName, {
        get: origGet,
        set: function(val) {
          if (typeof val === 'string' && window.__kwb_shouldProxy(val)) val = window.__kwb_proxyUrl(val);
          return origSet.call(this, val);
        },
        configurable: true,
        enumerable: desc.enumerable !== false
      });
    } catch(e) { console.warn('[WorkBuddy] patchSrcProperty failed for', propName, e); }
  }

  patchSrcProperty(HTMLScriptElement.prototype, 'src');
  patchSrcProperty(HTMLImageElement.prototype, 'src');
  if (typeof HTMLLinkElement !== 'undefined') patchSrcProperty(HTMLLinkElement.prototype, 'href');
  if (typeof HTMLSourceElement !== 'undefined') patchSrcProperty(HTMLSourceElement.prototype, 'src');

  // 2. Patch Element.prototype.setAttribute for dynamic src/href
  var origSetAttr = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function(name, value) {
    try {
      if (typeof value === 'string' && typeof name === 'string') {
        var ln = name.toLowerCase();
        var tag = (this.tagName || '').toUpperCase();
        if (ln === 'target') {
          // 关键：把新窗口/顶层打开强制改为 iframe 内跳转，保证 picker 持续可用
          if (value === '_blank' || value === '_top' || value === '_parent') value = '_self';
        }
        if ((ln === 'src' && (tag === 'SCRIPT' || tag === 'IMG' || tag === 'SOURCE')) ||
            (ln === 'href' && (tag === 'LINK' || tag === 'A'))) {
          if (window.__kwb_shouldProxy(value)) value = window.__kwb_proxyUrl(value);
        }
      }
    } catch(_) {}
    return origSetAttr.call(this, name, value);
  };

  // 3. Patch document.createElement to intercept dynamically created script/link/img elements
  // (catches webpack's dynamic chunk loading via new Script())
  var origCreate = document.createElement.bind(document);
  document.createElement = function(tag) {
    var el = origCreate(tag);
    if (typeof tag === 'string') {
      var tl = tag.toLowerCase();
      if (tl === 'script' || tl === 'img' || tl === 'link' || tl === 'source') {
        // The src/href setter is already patched via prototype, so this is just a safety net
        // for cases where the element's src is set before it's added to the DOM
      }
    }
    return el;
  };

  // 4. Patch XMLHttpRequest.prototype.open — 拦截 Axios/原生 XHR 请求通过代理
  var origXHROpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url) {
    try {
      if (typeof url === 'string' && window.__kwb_shouldProxy(url)) {
        var newUrl = window.__kwb_proxyUrl(url);
        console.log('[WorkBuddy] XHR proxy:', url, '→', newUrl);
        url = newUrl;
      }
    } catch(_) {}
    // 兼容 open(method, url) 和 open(method, url, async, user, password)
    if (arguments.length === 2) return origXHROpen.call(this, method, url);
    return origXHROpen.apply(this, [method, url].concat(Array.prototype.slice.call(arguments, 2)));
  };

  // 5. Patch window.fetch — 拦截 fetch 请求通过代理
  if (typeof fetch !== 'undefined') {
    var origFetch = fetch;
    window.fetch = function(input, init) {
      try {
        if (typeof input === 'string') {
          if (window.__kwb_shouldProxy(input)) {
            input = window.__kwb_proxyUrl(input);
            console.log('[WorkBuddy] fetch proxy:', input);
          }
        } else if (input && typeof input === 'object' && input.url) {
          // Request 对象
          var reqUrl = input.url;
          if (typeof reqUrl === 'string' && window.__kwb_shouldProxy(reqUrl)) {
            var newUrl = window.__kwb_proxyUrl(reqUrl);
            console.log('[WorkBuddy] fetch(Request) proxy:', reqUrl, '→', newUrl);
            input = new Request(newUrl, input);
          }
        }
      } catch(_) {}
      return origFetch.call(this, input, init);
    };
  }

  // 6. 强制 HTMLAnchorElement 的 target 属性为 _self（覆盖框架直接赋值 el.target='_blank' 的情况）
  try {
    var aDesc = Object.getOwnPropertyDescriptor(HTMLAnchorElement.prototype, 'target');
    if (aDesc && aDesc.set) {
      var aOrigSet = aDesc.set;
      Object.defineProperty(HTMLAnchorElement.prototype, 'target', {
        get: aDesc.get,
        set: function(v) {
          if (v === '_blank' || v === '_top' || v === '_parent') v = '_self';
          return aOrigSet.call(this, v);
        },
        configurable: true
      });
    }
  } catch(_) {}

  // 7. Patch window.open — 让“新窗口/弹窗”改为 iframe 内跳转，保证 picker 持续可用
  try {
    var origOpen = window.open ? window.open.bind(window) : null;
    window.open = function(url, target, features) {
      try {
        if (url && typeof url === 'string') {
          var u = window.__kwb_shouldProxy(url) ? window.__kwb_proxyUrl(url) : url;
          if (window.__kwb_shouldProxy(u)) u = window.__kwb_proxyUrl(u);
          window.location.href = u;
          return window;
        }
      } catch(_) {}
      return origOpen ? origOpen(url, target, features) : null;
    };
  } catch(_) {}

  console.log('[WorkBuddy] Dynamic URL interceptor ready (XHR + fetch + 链接同框跳转 patched)');
})();
</script>
"""

def _origin_script(target_origin: str) -> str:
    """在代理页面注入目标平台域名，供 URL 拦截器判断绝对 URL 是否属于本平台。"""
    try:
        _o = json.dumps(target_origin or "")
    except Exception:
        _o = '""'
    return "<script>window.__kwb_target_origin=" + _o + ";</script>\n"


# ── 注入到代理页面的元素选择器脚本（八爪鱼风格） ──
ELEMENT_PICKER_JS = r"""
<script>
(function() {
  if (window.__kwb_picker_loaded) return;
  window.__kwb_picker_loaded = true;

  var pickerActive = false;
  var hoverBox = null;
  var hoverLabel = null;
  var selectedHighlights = [];
  var actionPopup = null;
  var pickedCount = 0;

  // ── 工具函数 ──
  function buildSelector(el) {
    if (el.id) {
      try { return '#' + CSS.escape(el.id); } catch(_) { return '#' + el.id; }
    }
    var tag = (el.tagName || 'div').toLowerCase();

    // 1) el-table 单元格：优先使用列唯一类名，如 el-table_1_column_1
    // 这个类名在表头(th)和数据单元格(td)上都存在，天然匹配整列
    if (el.className && typeof el.className === 'string') {
      var classes = el.className.split(/\s+/);
      for (var i = 0; i < classes.length; i++) {
        var c = classes[i];
        if (/^el-table_\d+_column_\d+$/.test(c)) {
          var table = el.closest('.el-table, .el-table__body, .el-table__header');
          if (table && table.id) return '#' + table.id + ' .' + c;
          return '.el-table .' + c;
        }
      }
    }

    // 2) 普通 table / role="table" 单元格：生成 "tr > td:nth-child(N)" 形式
    if (tag === 'th' || tag === 'td') {
      var row = el.parentElement;
      if (row) {
        var cells = Array.from(row.children).filter(function(c) {
          return c.tagName === 'TH' || c.tagName === 'TD';
        });
        var idx = cells.indexOf(el) + 1;
        if (idx > 0) {
          var table = el.closest('table, [role="table"], .el-table');
          var tablePrefix = '';
          if (table) {
            if (table.id) tablePrefix = '#' + table.id + ' ';
            else if (table.className && typeof table.className === 'string') {
              var tableCls = table.className.split(/\s+/).filter(function(c) {
                return c && c.length > 1 && c.indexOf('el-table__') < 0;
              })[0];
              if (tableCls) tablePrefix = '.' + tableCls + ' ';
            }
          }
          return tablePrefix + 'tr > :nth-child(' + idx + ')';
        }
      }
    }

    // 3) 普通元素：保留有意义的类名
    var cls = '';
    if (el.className && typeof el.className === 'string') {
      var rawClasses = el.className.split(/\s+/).filter(function(c) {
        return c && c.length > 1 &&
               !c.startsWith('is-') && !c.startsWith('has-') &&
               !c.startsWith('v-') && !c.startsWith('router-') &&
               !c.startsWith('el-loading');
      });
      // 如果过滤后为空，保留一些结构性类名兜底
      if (rawClasses.length === 0) {
        rawClasses = el.className.split(/\s+/).filter(function(c) {
          return c && (c === 'el-table__cell' || c === 'el-table__header-wrapper' || c === 'el-table__body-wrapper');
        });
      }
      cls = rawClasses.slice(0, 2).join('.');
    }
    if (cls) return tag + '.' + cls;

    // 4) nth-child fallback
    var parent = el.parentElement;
    if (parent) {
      var siblings = parent.children;
      for (var i = 0; i < siblings.length; i++) {
        if (siblings[i] === el) return tag + ':nth-child(' + (i+1) + ')';
      }
    }
    return tag;
  }

  function getXPath(el) {
    if (el.id) return '//*[@id="' + el.id + '"]';
    var parts = [];
    var current = el;
    while (current && current.nodeType === 1) {
      var tag = current.tagName.toLowerCase();
      var parent = current.parentElement;
      if (parent) {
        var siblings = Array.from(parent.children).filter(function(c) { return c.tagName === current.tagName; });
        if (siblings.length > 1) {
          var idx = siblings.indexOf(current) + 1;
          tag += '[' + idx + ']';
        }
      }
      parts.unshift(tag);
      current = parent;
      if (current === document.body) break;
    }
    return '/' + parts.join('/');
  }

  function getElementInfo(el) {
    var tag = (el.tagName || '').toLowerCase();
    var text = (el.textContent || '').trim().substring(0, 200);
    var rect = el.getBoundingClientRect();
    return {
      tag: tag, text: text,
      css: buildSelector(el), xpath: getXPath(el),
      x: Math.round(rect.left + window.scrollX),
      y: Math.round(rect.top + window.scrollY),
      w: Math.round(rect.width), h: Math.round(rect.height),
      id: el.id || '',
      className: (typeof el.className === 'string' ? el.className : ''),
      parentTag: el.parentElement ? el.parentElement.tagName.toLowerCase() : '',
      parentClass: (el.parentElement && typeof el.parentElement.className === 'string') ? el.parentElement.className : '',
      href: el.getAttribute('href') || '',
      src: el.getAttribute('src') || '',
      placeholder: el.getAttribute('placeholder') || '',
      inputName: el.getAttribute('name') || '',
      inputType: el.getAttribute('type') || '',
    };
  }

  // ── 查找相似元素（八爪鱼核心功能） ──
  function findSimilarElements(el) {
    var parent = el.parentElement;
    if (!parent) return [el];
    var tag = el.tagName;
    // 获取元素的"特征类名"（排除框架类名）
    function featureClasses(node) {
      if (!node.className || typeof node.className !== 'string') return [];
      return node.className.split(/\s+/).filter(function(c) {
        return c && c.length > 1 && !c.startsWith('el-') && !c.startsWith('is-') && !c.startsWith('has-') && !c.startsWith('v-');
      });
    }
    var myClasses = featureClasses(el);
    var siblings = Array.from(parent.children).filter(function(s) {
      if (s === el) return true;
      if (s.tagName !== tag) return false;
      // 检查是否有相似的 class 结构
      var sibClasses = featureClasses(s);
      if (myClasses.length > 0 && sibClasses.length > 0) {
        // 至少有一个共同的特征类名
        return myClasses.some(function(c) { return sibClasses.indexOf(c) >= 0; });
      }
      // 如果都没有类名，检查文本长度是否相似
      var myText = (el.textContent || '').trim().length;
      var sibText = (s.textContent || '').trim().length;
      if (myText > 0 && sibText > 0) {
        return Math.abs(myText - sibText) < Math.max(myText, sibText) * 0.5 + 20;
      }
      return true;
    });
    return siblings;
  }

  // ── 查找包含的表格或列表 ──
  function findTableOrList(el) {
    // 向上查找最近的 table, .el-table, ul, .list, [role="table"] 等
    var container = el.closest('table, .el-table, .el-table__body-wrapper, [role="table"], .list, .data-list, .table-wrapper');
    if (container) {
      // Element UI 表格：.el-table 是 div，需要找内部 table
      if (container.classList && (container.classList.contains('el-table') || container.classList.contains('el-table__body-wrapper') || container.classList.contains('table-wrapper'))) {
        var innerTable = container.querySelector('table');
        if (innerTable) return innerTable;
      }
      return container;
    }
    // 检查父级是否是重复结构
    var parent = el.parentElement;
    if (parent && parent.parentElement) {
      var grandparent = parent.parentElement;
      var siblings = Array.from(grandparent.children).filter(function(c) { return c.tagName === parent.tagName; });
      if (siblings.length > 2) return grandparent;
    }
    return null;
  }

  // ── 创建工具栏 ──
  var pickerBar = document.createElement('div');
  pickerBar.id = '__kwb_picker_bar';
  pickerBar.innerHTML =
    '<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">' +
      '<span style="font-weight:700;font-size:13px;color:#409eff;white-space:nowrap;">🔍 元素选择</span>' +
      '<span id="__kwb_picker_status" style="font-size:11px;color:#aaa;white-space:nowrap;">已暂停 — 页面可正常操作</span>' +
      '<span style="flex:1;"></span>' +
      '<span id="__kwb_load_progress" style="font-size:11px;color:#e6a23c;white-space:nowrap;display:none;"></span>' +
      '<span id="__kwb_picked_count" style="font-size:11px;color:#67c23a;font-weight:700;white-space:nowrap;">已选 0</span>' +
      '<button data-action="toggle" style="padding:4px 12px;background:#409eff;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;white-space:nowrap;">🎯 开始选择</button>' +
      '<button data-action="select-table" style="padding:4px 12px;background:#67c23a;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;white-space:nowrap;">📋 选表格</button>' +
      '<button data-action="select-pagination" title="点击页面上的下一页按钮以捕获翻页规则" style="padding:4px 12px;background:#a855f7;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;white-space:nowrap;">🔁 选分页</button>' +
      '<button data-action="clear" style="padding:4px 10px;background:#909399;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;white-space:nowrap;">清空</button>' +
      '<span style="color:#555;margin:0 2px;font-size:12px;">|</span>' +
      '<button data-action="reload" title="普通刷新" style="padding:4px 10px;background:#409eff;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;white-space:nowrap;">🔄 刷新</button>' +
      '<button data-action="force-reload" title="跳过缓存强制重载" style="padding:4px 10px;background:#e6a23c;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;white-space:nowrap;">⚡ 强制重载</button>' +
      '<button data-action="clear-cache-reload" title="清空缓存并重新加载" style="padding:4px 10px;background:#f56c6c;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;white-space:nowrap;">🧹 清空缓存重载</button>' +
    '</div>';
  pickerBar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#1a1a2e;color:#eee;padding:6px 16px;box-shadow:0 2px 12px rgba(0,0,0,.6);font-family:Arial,sans-serif;line-height:1.5;';

  // ── 事件委托：工具栏按钮 ──
  pickerBar.addEventListener('click', function(e) {
    var btn = e.target.closest('button[data-action]');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    var action = btn.getAttribute('data-action');
    if (action === 'toggle') {
      pickerActive = !pickerActive;
      updateToolbarUI();
    } else if (action === 'select-table') {
      doSelectTable();
    } else if (action === 'clear') {
      clearAllSelections();
    } else if (action === 'select-pagination') {
      doSelectPagination();
    } else if (action === 'reload') {
      notifyReloading('刷新');
      location.reload();
    } else if (action === 'force-reload') {
      notifyReloading('强制重载（跳过缓存）');
      location.reload(true);
    } else if (action === 'clear-cache-reload') {
      notifyReloading('清空缓存并重新加载');
      try {
        // 清除 page 级别缓存
        if (typeof localStorage !== 'undefined') localStorage.clear();
        if (typeof sessionStorage !== 'undefined') sessionStorage.clear();
        // 清除所有 cookie（同源下）
        document.cookie.split(';').forEach(function(c) {
          document.cookie = c.replace(/^ +/, '').replace(/=.*/, '=;expires=' + new Date(0).toUTCString() + ';path=/');
        });
      } catch(_) {}
      location.reload(true);
    }
  });

  function notifyReloading(msg) {
    try {
      window.parent.postMessage(JSON.stringify({ type: 'kwb_proxy_reloading', message: msg }), '*');
    } catch(_) {}
  }

  function updateToolbarUI() {
    var toggleBtn = pickerBar.querySelector('button[data-action="toggle"]');
    var statusText = pickerBar.querySelector('#__kwb_picker_status');
    if (!toggleBtn || !statusText) return;
    if (pickerActive) {
      toggleBtn.textContent = '⏸ 暂停选择';
      toggleBtn.style.background = '#f56c6c';
      statusText.textContent = '🔴 选择模式已开启 — 点击页面元素选择数据';
      statusText.style.color = '#f56c6c';
      document.body.style.cursor = 'crosshair';
    } else {
      toggleBtn.textContent = '🎯 开始选择';
      toggleBtn.style.background = '#409eff';
      statusText.textContent = '已暂停 — 页面可正常操作';
      statusText.style.color = '#aaa';
      document.body.style.cursor = '';
      removeHoverBox();
      removeActionPopup();
    }
  }

  // ── 插入工具栏到 DOM ──
  function ensureToolbar() {
    if (!document.getElementById('__kwb_picker_bar')) {
      document.body.insertBefore(pickerBar, document.body.firstChild);
      document.body.style.paddingTop = '44px';
      updateToolbarUI();
      var cnt = pickerBar.querySelector('#__kwb_picked_count');
      if (cnt) cnt.textContent = '已选 ' + pickedCount;
    }
  }
  ensureToolbar();

  // MutationObserver：SPA 可能重新渲染 body，需重新插入工具栏
  var observer = new MutationObserver(function() { ensureToolbar(); });
  observer.observe(document.documentElement, { childList: true, subtree: true });

  // ── hover 高亮框（含信息浮层） ──
  function removeHoverBox() {
    if (hoverBox) { hoverBox.remove(); hoverBox = null; }
    if (hoverLabel) { hoverLabel.remove(); hoverLabel = null; }
  }
  function showHoverBox(el) {
    if (!hoverBox) {
      hoverBox = document.createElement('div');
      hoverBox.style.cssText = 'position:fixed;z-index:2147483644;pointer-events:none;border:2px dashed #409eff;background:rgba(64,158,255,.08);';
      document.body.appendChild(hoverBox);
      hoverLabel = document.createElement('div');
      hoverLabel.style.cssText = 'position:fixed;z-index:2147483645;pointer-events:none;background:#409eff;color:#fff;font-size:10px;padding:2px 6px;border-radius:3px;white-space:nowrap;max-width:320px;overflow:hidden;text-overflow:ellipsis;font-family:Arial,sans-serif;';
      document.body.appendChild(hoverLabel);
    }
    hoverBox.style.display = 'block';
    hoverLabel.style.display = 'block';
    var r = el.getBoundingClientRect();
    hoverBox.style.left = r.left + 'px';
    hoverBox.style.top = r.top + 'px';
    hoverBox.style.width = r.width + 'px';
    hoverBox.style.height = r.height + 'px';
    var tag = (el.tagName || '').toLowerCase();
    var txt = (el.textContent || '').trim().substring(0, 28);
    var hint = txt || (el.src ? '[图片]' : '') || (el.getAttribute('href') ? '[链接]' : '') || (el.getAttribute('placeholder') || '');
    hoverLabel.textContent = '<' + tag + '> ' + hint;
    var lh = r.top - 20;
    hoverLabel.style.left = Math.max(4, r.left) + 'px';
    hoverLabel.style.top = (lh < 4 ? r.bottom + 4 : lh) + 'px';
  }

  // ── 选中高亮（持久，带序号徽标） ──
  function highlightElement(el, color, index) {
    var hl = document.createElement('div');
    var badge = '';
    if (index != null) {
      badge = '<span style="position:absolute;top:-11px;left:-11px;background:' + (color||'#67c23a') + ';color:#fff;font-size:10px;font-weight:700;border-radius:50%;width:18px;height:18px;line-height:18px;text-align:center;display:inline-block;box-shadow:0 1px 3px rgba(0,0,0,.4);">'
             + index + '</span>';
    }
    hl.innerHTML = badge;
    hl.style.cssText = 'position:fixed;z-index:2147483643;pointer-events:none;border:2px solid ' + (color||'#67c23a') + ';background:rgba(103,194,58,.12);';
    var r = el.getBoundingClientRect();
    hl.style.left = r.left + 'px';
    hl.style.top = r.top + 'px';
    hl.style.width = r.width + 'px';
    hl.style.height = r.height + 'px';
    hl.__target = el;
    hl.__color = color || '#67c23a';
    document.body.appendChild(hl);
    selectedHighlights.push(hl);
    return hl;
  }

  // ── 高亮框随页面滚动/缩放实时同步位置 ──
  function refreshHighlightPositions() {
    selectedHighlights.forEach(function(hl) {
      var el = hl.__target;
      if (!el || !el.isConnected) { hl.style.display = 'none'; return; }
      var r = el.getBoundingClientRect();
      hl.style.display = 'block';
      hl.style.left = r.left + 'px';
      hl.style.top = r.top + 'px';
      hl.style.width = r.width + 'px';
      hl.style.height = r.height + 'px';
    });
  }

  function clearAllSelections() {
    selectedHighlights.forEach(function(h) { h.remove(); });
    selectedHighlights = [];
    pickedCount = 0;
    var cnt = pickerBar.querySelector('#__kwb_picked_count');
    if (cnt) cnt.textContent = '已选 0';
    removeActionPopup();
  }

  // ── 操作弹窗（八爪鱼风格：选中后弹出操作面板） ──
  function removeActionPopup() {
    if (actionPopup) { actionPopup.remove(); actionPopup = null; }
  }

  function showActionPopup(el, info, similar) {
    removeActionPopup();
    actionPopup = document.createElement('div');
    actionPopup.id = '__kwb_action_popup';
    actionPopup.style.cssText = 'position:fixed;z-index:2147483646;background:#fff;border-radius:8px;box-shadow:0 4px 24px rgba(0,0,0,.3);padding:0;min-width:280px;max-width:360px;font-family:Arial,sans-serif;overflow:hidden;';

    var r = el.getBoundingClientRect();
    var top = r.bottom + 6;
    var left = r.left;
    if (top + 200 > window.innerHeight) top = r.top - 206;
    if (left + 300 > window.innerWidth) left = window.innerWidth - 310;
    actionPopup.style.left = Math.max(8, left) + 'px';
    actionPopup.style.top = Math.max(52, top) + 'px';

    var tagClr = '#409eff';
    var typeLabel = '文本';
    if (info.tag === 'img') { typeLabel = '图片'; tagClr = '#e6a23c'; }
    else if (info.tag === 'a') { typeLabel = '链接'; tagClr = '#67c23a'; }
    else if (['input','textarea','select'].indexOf(info.tag) >= 0) { typeLabel = '输入框'; tagClr = '#909399'; }
    else if (info.tag === 'td' || info.tag === 'th') { typeLabel = '表格单元格'; tagClr = '#f56c6c'; }

    var html =
      '<div style="background:#f5f7fa;padding:8px 12px;border-bottom:1px solid #ebeef5;display:flex;align-items:center;gap:6px;">' +
        '<span style="background:' + tagClr + ';color:#fff;padding:2px 8px;border-radius:3px;font-size:11px;font-weight:700;">' + escapeHtml(info.tag) + '</span>' +
        '<span style="font-size:11px;color:#909399;">' + typeLabel + '</span>' +
        '<span style="flex:1;"></span>' +
        '<button data-popup-action="close" style="background:none;border:none;cursor:pointer;color:#c0c4cc;font-size:16px;padding:0 4px;">&times;</button>' +
      '</div>' +
      '<div style="padding:8px 12px;font-size:12px;color:#333;max-height:60px;overflow:hidden;border-bottom:1px solid #f0f0f0;">' +
        '<span style="color:#909399;font-size:10px;">内容：</span>' +
        '<span style="font-weight:600;">' + escapeHtml(info.text.substring(0,80) || '(空)') + '</span>' +
      '</div>';

    // 相似元素提示
    if (similar.length > 1) {
      html +=
        '<div style="padding:8px 12px;background:#fdf6ec;border-bottom:1px solid #f0f0f0;">' +
          '<span style="font-size:11px;color:#e6a23c;">⚡ 检测到 ' + similar.length + ' 个相似元素</span>' +
        '</div>';
    }

    html +=
      '<div style="padding:8px;display:flex;flex-direction:column;gap:6px;">' +
        '<button data-popup-action="select-one" style="padding:8px 12px;background:#409eff;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600;">✅ 选中此元素</button>';

    if (similar.length > 1) {
      html += '<button data-popup-action="select-similar" style="padding:8px 12px;background:#e6a23c;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600;">⚡ 选中全部相似元素 (' + similar.length + '个)</button>';
    }

    // 表格/列表检测
    var tableContainer = findTableOrList(el);
    if (tableContainer && tableContainer !== el) {
      html += '<button data-popup-action="select-table-area" style="padding:8px 12px;background:#67c23a;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600;">📋 选择整个表格/列表</button>';
    }

    html += '<button data-popup-action="view-source" style="padding:8px 12px;background:#909399;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600;">👁 查看源码 / 文本兜底</button>';

    html += '</div>';

    // CSS 选择器/XPath 信息（折叠）
    html +=
      '<details style="border-top:1px solid #f0f0f0;padding:4px 12px;font-size:10px;color:#909399;">' +
        '<summary style="cursor:pointer;padding:4px 0;">CSS / XPath</summary>' +
        '<div style="padding:4px 0;word-break:break-all;">' +
          '<div style="color:#409eff;">' + escapeHtml(info.css) + '</div>' +
          '<div style="color:#909399;margin-top:4px;">' + escapeHtml(info.xpath) + '</div>' +
        '</div>' +
      '</details>';

    // 源码 / 文本兜底展示区（点击"查看源码"后展开）
    html +=
      '<div id="__kwb_source_view" style="display:none;padding:8px 12px;background:#1e1e2e;color:#d4d4d4;font-size:10px;font-family:monospace;max-height:160px;overflow:auto;white-space:pre-wrap;word-break:break-all;border-top:1px solid #f0f0f0;"></div>';

    actionPopup.innerHTML = html;

    // 事件委托
    actionPopup.addEventListener('click', function(e) {
      var btn = e.target.closest('button[data-popup-action]');
      if (!btn) return;
      var act = btn.getAttribute('data-popup-action');
      if (act === 'close') {
        removeActionPopup();
      } else if (act === 'select-one') {
        pickElement(el, false);
        removeActionPopup();
      } else if (act === 'select-similar') {
        pickElement(el, true);
        removeActionPopup();
      } else if (act === 'select-table-area' && tableContainer) {
        pickTableArea(tableContainer);
        removeActionPopup();
      } else if (act === 'view-source') {
        var sv = actionPopup.querySelector('#__kwb_source_view');
        if (sv) {
          sv.style.display = sv.style.display === 'none' ? 'block' : 'none';
          if (sv.style.display === 'block' && sv.dataset.loaded !== '1') {
            sv.dataset.loaded = '1';
            try {
              sv.textContent = (el.outerHTML || '').substring(0, 2000);
            } catch(_) {
              sv.textContent = '(无法读取源码)';
            }
          }
        }
      }
    });

    document.body.appendChild(actionPopup);
  }

  function escapeHtml(s) {
    if (!s) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  // 当前页面真实路由（去掉代理前缀），用于多页面采集回放
  function currentSourceUrl() {
    try {
      var p = window.location.pathname.replace(/^\/api\/collector\/proxy/, '') + window.location.search + window.location.hash;
      return p || '/';
    } catch(_) { return ''; }
  }

  // ── 选中元素 → 发送到父窗口 ──
  function pickElement(el, selectAllSimilar) {
    var info = getElementInfo(el);
    var similar = selectAllSimilar ? findSimilarElements(el) : [el];
    var similarInfos = similar.map(function(e) { return getElementInfo(e); });

    // 高亮所有选中元素（带序号徽标）
    similar.forEach(function(e, i) {
      highlightElement(e, selectAllSimilar ? '#e6a23c' : '#67c23a', pickedCount + i + 1);
    });
    pickedCount += similar.length;
    var cnt = pickerBar.querySelector('#__kwb_picked_count');
    if (cnt) cnt.textContent = '已选 ' + pickedCount;

    // 发送消息到父窗口
    window.parent.postMessage(JSON.stringify({
      type: 'kwb_element_picked',
      element: info,
      is_batch: selectAllSimilar && similar.length > 1,
      similar_count: similar.length,
      similar_elements: similarInfos,
      source: 'kwb_proxy',
      source_url: currentSourceUrl(),
      text_pattern: info.text   // 文本兜底样本（CSS/XPath 均失败时用于按文本定位）
    }), '*');
  }

  // ── 选中整个表格/列表 ──
  function pickTableArea(container) {
    var info = getElementInfo(container);
    highlightElement(container, '#67c23a', pickedCount + 1);
    pickedCount++;
    var cnt = pickerBar.querySelector('#__kwb_picked_count');
    if (cnt) cnt.textContent = '已选 ' + pickedCount;

    // 查找表格列
    var rows = container.querySelectorAll('tr, [role="row"]');
    if (rows.length >= 1) {
      var headerRow = rows[0];
      var headerCells = headerRow.querySelectorAll('th, td, [role="columnheader"], [role="gridcell"]');
      var headers = [];
      var colSelectors = [];
      var colXpaths = [];
      headerCells.forEach(function(cell) {
        var txt = (cell.textContent || '').trim().substring(0, 20);
        headers.push(txt || '列' + (headers.length+1));
        colSelectors.push(buildSelector(cell));
        colXpaths.push(getXPath(cell));
      });
      window.parent.postMessage(JSON.stringify({
        type: 'kwb_table_columns',
        headers: headers,
        selectors: colSelectors,
        xpaths: colXpaths,
        row_count: rows.length,
        source: 'kwb_proxy',
        source_url: currentSourceUrl()
      }), '*');
    } else {
      // 列表模式
      window.parent.postMessage(JSON.stringify({
        type: 'kwb_element_picked',
        element: info,
        is_batch: true,
        similar_count: 1,
        similar_elements: [info],
        is_list_area: true,
        source: 'kwb_proxy',
        source_url: currentSourceUrl(),
        text_pattern: info.text
      }), '*');
    }
  }

  // ── "选表格"按钮 ──
  function doSelectTable() {
    // 优先查找 Element UI 表格（.el-table 是 div，内部有 <table>）
    var table = null;
    var elTable = document.querySelector('.el-table');
    if (elTable) {
      table = elTable.querySelector('table');
    }
    if (!table) {
      table = document.querySelector('.el-table table, table.el-table__body, [role="table"] table, .table-wrapper table, table');
    }
    if (!table) {
      // 尝试找 div-based 列表
      var listContainer = document.querySelector('.data-list, .list, [role="table"]');
      if (listContainer) {
        pickTableArea(listContainer);
        showToast('已选中列表区域');
        return;
      }
      alert('未找到表格或列表元素\n请先点击"开始选择"，然后点击表格中的某个单元格');
      return;
    }
    pickTableArea(table);
  }

  // ── "选分页"按钮：捕获下一页按钮 ──
  function doSelectPagination() {
    window.paginationPickMode = true;
    if (pickerActive) { pickerActive = false; updateToolbarUI(); }
    var statusText = pickerBar.querySelector('#__kwb_picker_status');
    if (statusText) {
      statusText.textContent = '🟣 分页模式：请点击页面上的"下一页"按钮';
      statusText.style.color = '#a855f7';
    }
    showToast('分页模式：点击"下一页"按钮以捕获翻页规则');
  }

  // 检测分页器总页数（取分页区内出现的最大数字）
  function detectTotalPages() {
    try {
      var pager = document.querySelector('.el-pagination, [class*="pagination"], .ant-pagination, ul.pagination, [class*="pager"]');
      if (!pager) return 0;
      var nums = [];
      pager.querySelectorAll('*').forEach(function(n) {
        var t = (n.textContent || '').trim();
        if (/^\d+$/.test(t)) nums.push(parseInt(t, 10));
      });
      if (nums.length) return Math.max.apply(null, nums);
    } catch(_) {}
    return 0;
  }

  function showToast(msg) {
    var t = document.createElement('div');
    t.textContent = msg;
    t.style.cssText = 'position:fixed;top:50px;left:50%;transform:translateX(-50%);background:#67c23a;color:#fff;padding:8px 20px;border-radius:4px;font-size:13px;z-index:2147483647;box-shadow:0 2px 8px rgba(0,0,0,.3);';
    document.body.appendChild(t);
    setTimeout(function() { t.remove(); }, 2500);
  }

  // ── 核心点击处理 ──
  function onPickerClick(e) {
    // 关键修复：先检查是否点击了我们自己的 UI，再决定是否拦截
    if (e.target.closest('#__kwb_picker_bar')) return;
    if (e.target.closest('#__kwb_action_popup')) return;

    if (!pickerActive) return;

    e.preventDefault();
    e.stopPropagation();

    var el = e.target;
    // 如果点击的是高亮框，取高亮框对应的目标元素
    if (el.__target) el = el.__target;

    var info = getElementInfo(el);
    var similar = findSimilarElements(el);

    // 高亮当前选中元素
    highlightElement(el, '#409eff');

    // 显示操作弹窗
    showActionPopup(el, info, similar);
  }

  // ── hover 处理 ──
  function onMouseMove(e) {
    if (!pickerActive) return;
    if (e.target.closest('#__kwb_picker_bar')) { removeHoverBox(); return; }
    if (e.target.closest('#__kwb_action_popup')) { removeHoverBox(); return; }
    var el = e.target;
    if (el.__target) el = el.__target;
    showHoverBox(el);
  }

  // ── 注册事件 ──
  // 分页按钮捕获（独立于元素选择模式，capture 阶段优先于 onPickerClick）
  window.paginationPickMode = false;
  document.addEventListener('click', function(e) {
    if (!window.paginationPickMode) return;
    window.paginationPickMode = false;
    try { e.preventDefault(); e.stopPropagation(); } catch(_) {}
    if (pickerActive) { pickerActive = false; updateToolbarUI(); }
    removeActionPopup();
    var el = e.target;
    if (el.__target) el = el.__target;
    try {
      var info = getElementInfo(el);
      var totalPages = detectTotalPages();
      window.parent.postMessage(JSON.stringify({
        type: 'kwb_pagination_selected',
        next_button: { tag: info.tag, text: info.text, css: info.css, xpath: info.xpath },
        next_button_selector: info.css,
        total_pages: totalPages,
        source: 'kwb_proxy'
      }), '*');
      var statusText = pickerBar.querySelector('#__kwb_picker_status');
      if (statusText) { statusText.textContent = '已捕获下一页: ' + (info.text || info.css); statusText.style.color = '#aaa'; }
      showToast('已捕获翻页按钮: ' + (info.text || info.css) + (totalPages ? ' | 共 ' + totalPages + ' 页' : ''));
    } catch(_) {
      showToast('捕获分页失败');
    }
  }, true);

  document.addEventListener('click', onPickerClick, true);
  document.addEventListener('mousemove', onMouseMove, true);
  // 高亮框随滚动/缩放同步
  window.addEventListener('scroll', refreshHighlightPositions, true);
  window.addEventListener('resize', refreshHighlightPositions);
  // 选中元素高亮框在 SPA 路由切换后重新定位
  var __kwb_posTimer = setInterval(refreshHighlightPositions, 800);
  setTimeout(function() { clearInterval(__kwb_posTimer); }, 60000);

  // ESC 键退出选择模式
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && pickerActive) {
      pickerActive = false;
      updateToolbarUI();
      removeHoverBox();
      removeActionPopup();
    } else if (e.key === 'Escape' && actionPopup) {
      removeActionPopup();
    }
  });

  // ── 加载进度追踪 + 通知父窗口 ──
  var __kwb_progress = { loaded: 0, total: 0, stage: document.readyState };

  function notifyProgress() {
    try {
      window.parent.postMessage(JSON.stringify({
        source: 'kwb_proxy',
        type: 'kwb_proxy_progress',
        stage: __kwb_progress.stage,
        loaded: __kwb_progress.loaded,
        total: __kwb_progress.total,
        progress: __kwb_progress.total > 0 ? Math.round(__kwb_progress.loaded / __kwb_progress.total * 100) : 0,
        url: location.href,
        title: document.title
      }), '*');
    } catch(_) {}
    // 更新工具栏进度显示
    var progressEl = document.getElementById('__kwb_load_progress');
    if (progressEl) {
      if (__kwb_progress.stage === 'complete') {
        progressEl.style.display = 'none';
      } else if (__kwb_progress.total > 0) {
        progressEl.style.display = '';
        progressEl.textContent = '⏳ ' + Math.round(__kwb_progress.loaded / __kwb_progress.total * 100) + '%';
      } else {
        progressEl.style.display = '';
        progressEl.textContent = '⏳ 加载中...';
      }
    }
  }

  function notifyReady() {
    __kwb_progress.stage = 'complete';
    notifyProgress();
    try {
      window.parent.postMessage(JSON.stringify({
        source: 'kwb_proxy',
        type: 'kwb_proxy_dom_ready',
        url: location.href,
        title: document.title
      }), '*');
    } catch(_) {}
  }

  // 监听加载阶段
  document.addEventListener('readystatechange', function() {
    __kwb_progress.stage = document.readyState;
    notifyProgress();
  });

  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    setTimeout(notifyReady, 100);
  } else {
    document.addEventListener('DOMContentLoaded', function() {
      __kwb_progress.stage = 'interactive';
      notifyProgress();
      setTimeout(notifyReady, 200);
    });
    window.addEventListener('load', function() {
      __kwb_progress.stage = 'complete';
      notifyProgress();
      setTimeout(notifyReady, 300);
    });
  }

  // 追踪资源加载（粗略计数）
  try {
    var observer = new PerformanceObserver(function(list) {
      var entries = list.getEntries();
      for (var i = 0; i < entries.length; i++) {
        if (entries[i].entryType === 'resource') {
          __kwb_progress.total++;
          if (entries[i].duration > 0 || entries[i].transferSize > 0) {
            __kwb_progress.loaded++;
          }
        }
      }
      notifyProgress();
    });
    observer.observe({ type: 'resource', buffered: true });
  } catch(_) {}

  // 错误捕获（包括资源加载失败）
  window.addEventListener('error', function(e) {
    // 区分资源加载错误和脚本错误
    if (e.target && e.target !== window) {
      // 资源加载失败（img, script, link, video, audio, iframe）
      var el = e.target;
      var tag = (el.tagName || '').toLowerCase();
      // 非关键资源加载失败，静默处理（显示占位符）
      if (tag === 'img') {
        el.style.cssText = el.style.cssText +
          ';min-width:32px;min-height:32px;background:#333;border:1px dashed #555;display:inline-block;';
        el.title = '图片加载失败';
      }
    }
    // 通过 postMessage 报告给父窗口
    try {
      window.parent.postMessage(JSON.stringify({
        source: 'kwb_proxy', type: 'kwb_proxy_error',
        message: e.message || 'resource load error', filename: e.filename || '', line: e.lineno || 0,
        tag: e.target && e.target !== window ? (e.target.tagName || '') : ''
      }), '*');
    } catch(_) {}
  }, true);

  // 非关键资源超时控制：图片 8s、CSS 10s 自动跳过
  (function() {
    var RESOURCE_TIMEOUTS = { img: 8000, link: 10000, font: 6000 };
    function setupTimeoutObserver() {
      var pendingResources = {};
      var origObserver = new MutationObserver(function(mutations) {
        mutations.forEach(function(mutation) {
          mutation.addedNodes.forEach(function(node) {
            if (node.nodeType !== 1) return;
            var tag = node.tagName.toLowerCase();
            var timeout = RESOURCE_TIMEOUTS[tag];
            if (!timeout) return;
            // 记录并设置超时
            var key = node.src || node.href || '';
            if (!key && tag === 'link') key = 'link-' + Math.random();
            if (!key) return;
            pendingResources[key] = { el: node, added: Date.now() };
            setTimeout(function() {
              if (pendingResources[key] && !node.complete && node.complete !== undefined ? !node.complete : true) {
                console.warn('[WorkBuddy] Resource timeout (' + tag + '): ' + (node.src || node.href || 'unknown'));
                delete pendingResources[key];
                try {
                  if (tag === 'link' && node.parentNode) node.parentNode.removeChild(node);
                  if (tag === 'img') {
                    node.src = '';
                    node.style.cssText = node.style.cssText +
                      ';min-width:32px;min-height:32px;background:#333;border:1px dashed #555;';
                    node.title = '加载超时';
                  }
                } catch(_) {}
              }
              delete pendingResources[key];
            }, timeout);
          });
        });
      });
      origObserver.observe(document.documentElement, { childList: true, subtree: true });
    }
    if (document.readyState === 'complete') { setupTimeoutObserver(); }
    else { window.addEventListener('load', setupTimeoutObserver); }
  })();

  // ── 接收父窗口高亮联动请求（右侧规则 hover 时高亮对应页面元素） ──
  window.addEventListener('message', function(e) {
    var d = e.data;
    if (typeof d === 'string') { try { d = JSON.parse(d); } catch(_) { return; } }
    if (!d || !d.type) return;
    if (d.type === 'kwb_highlight') {
      try {
        var els = document.querySelectorAll(d.css);
        for (var i = 0; i < els.length; i++) {
          (function(el) {
            var hl = highlightElement(el, d.color || '#f56c6c');
            setTimeout(function() { if (hl && hl.parentNode) hl.remove(); }, 2200);
          })(els[i]);
        }
      } catch(_) {}
    } else if (d.type === 'kwb_clear_highlights') {
      clearAllSelections();
    }
  });

  console.log('[Picker] 元素选择器已就绪');
})();
</script>
"""


# ═══════════════ 代理模式：实时页面元素选择 ═══════════════

@app.route("/api/collector/proxy_start", methods=["POST"])
def api_collector_proxy_start():
    """
    启动代理模式：登录目标平台并捕获 Cookie，返回代理 URL。

    请求: { "host": "...", "username": "...", "password": "...", "target": "..." }
    响应: { "ok": true, "session_id": "...", "proxy_url": "/api/collector/proxy/...", "title": "..." }
    """
    global visual_collector_state

    # 如果已有代理会话，先关闭它
    if visual_collector_state.get("proxy_active"):
        try:
            for key in ("proxy_page", "proxy_context", "proxy_browser"):
                obj = visual_collector_state.get(key)
                if obj:
                    try: obj.close()
                    except: pass
                visual_collector_state[key] = None
            visual_collector_state["proxy_active"] = False
            logger.info("[ProxyStart] 已关闭旧代理会话")
        except Exception as e:
            logger.warning(f"[ProxyStart] 关闭旧会话失败: {e}")

    data = request.get_json()
    host = (data.get("host") or "").strip()
    username = (data.get("username") or "").strip()
    password = data.get("password", "")
    target = (data.get("target") or "").strip()

    if not host or not username or not target:
        return jsonify({"ok": False, "message": "请填写平台地址、账号和目标页面"}), 400

    visual_collector_state["host"] = host
    visual_collector_state["username"] = username
    visual_collector_state["password"] = password
    visual_collector_state["target"] = target
    visual_collector_state["selected_elements"] = []

    # 保存高级配置
    advanced = data.get("advanced", {})
    visual_collector_state["proxy_advanced"] = {
        "wait_time": advanced.get("wait_time", 2),
        "ajax_wait": advanced.get("ajax_wait", 3),
        "load_timeout": advanced.get("load_timeout", 30),
        "no_ads": advanced.get("no_ads", True),
        "no_anim": advanced.get("no_anim", True),
        "no_video": advanced.get("no_video", True),
        "lazy_img": advanced.get("lazy_img", False),
    }

    # 构建完整目标 URL
    if target.startswith("http"):
        full_url = target
    else:
        full_url = urljoin(host.rstrip("/") + "/", target.lstrip("/"))

    target_origin = urlparse(host).scheme + "://" + urlparse(host).netloc
    visual_collector_state["proxy_target_origin"] = target_origin

    try:
        # 1. 启动 Playwright 登录
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = context.new_page()

        logger.info(f"[ProxyStart] 登录 {host}/login ...")
        page.goto(host.rstrip("/") + "/login", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)

        # 填写登录表单
        try:
            page.fill('input[placeholder*="账号"], input[name="username"], input[type="text"]', username)
        except Exception:
            page.fill('input[type="text"]', username)
        page.fill('input[placeholder*="密码"], input[name="password"], input[type="password"]', password)

        login_btn = page.locator('button:has-text("登录"), button:has-text("登 录"), button[type="submit"], .login-btn')
        if login_btn.count() == 0:
            login_btn = page.locator('button, .el-button')
        login_btn.first.click()

        # 等待登录成功
        page.wait_for_url(lambda u: "/login" not in u.lower(), timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # 2. 导航到目标页面（使用高级配置中的超时和等待时间）
        load_timeout = (advanced.get("load_timeout", 30) or 30) * 1000
        wait_time = (advanced.get("wait_time", 2) or 2) * 1000
        logger.info(f"[ProxyStart] 导航到目标页面 {full_url} (timeout={load_timeout}ms, wait={wait_time}ms)")
        page.goto(full_url, wait_until="domcontentloaded", timeout=load_timeout)
        page.wait_for_timeout(int(wait_time))

        page_title = page.title()

        # 3. 提取所有 Cookie
        cookies = context.cookies()
        cookie_dict = {}
        for c in cookies:
            cookie_dict[c["name"]] = c["value"]

        # 3.1 提取 localStorage / sessionStorage（Vue/SPA 通常把 token 存在这里）
        storage_data = {"local": {}, "session": {}}
        try:
            storage_data = page.evaluate("""() => {
                const local = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    local[k] = localStorage.getItem(k);
                }
                const session = {};
                for (let i = 0; i < sessionStorage.length; i++) {
                    const k = sessionStorage.key(i);
                    session[k] = sessionStorage.getItem(k);
                }
                return { local, session };
            }""")
            logger.info(f"[ProxyStart] 捕获 localStorage {len(storage_data.get('local', {}))} 项, "
                        f"sessionStorage {len(storage_data.get('session', {}))} 项")
        except Exception as e:
            logger.warning(f"[ProxyStart] 提取 storage 失败: {e}")

        logger.info(f"[ProxyStart] 登录成功，捕获 {len(cookie_dict)} 个 Cookie, 页面标题: {page_title}")

        # 4. 保存代理会话
        session_id = hashlib.md5(f"proxy_{host}{target}{time.time()}".encode()).hexdigest()[:16]
        visual_collector_state["session_id"] = session_id
        visual_collector_state["proxy_active"] = True
        visual_collector_state["proxy_cookies"] = cookie_dict
        visual_collector_state["proxy_storage"] = storage_data
        visual_collector_state["proxy_page"] = page
        visual_collector_state["proxy_browser"] = browser
        visual_collector_state["proxy_context"] = context
        visual_collector_state["running"] = False

        # 保存完整认证状态（线程安全，供预览/采集端点复用）
        try:
            visual_collector_state["proxy_storage_state"] = context.storage_state()
            logger.info(f"[ProxyStart] 已保存 storage_state "
                        f"(cookies: {len(visual_collector_state['proxy_storage_state'].get('cookies', []))} 条)")
        except Exception as e:
            logger.warning(f"[ProxyStart] 保存 storage_state 失败: {e}")
            visual_collector_state["proxy_storage_state"] = None

        # 5. 构建代理访问路径
        proxy_path = urlparse(full_url).path or "/"
        if urlparse(full_url).query:
            proxy_path += "?" + urlparse(full_url).query

        return jsonify({
            "ok": True,
            "session_id": session_id,
            "proxy_url": f"/api/collector/proxy{proxy_path}",
            "title": page_title,
            "target_origin": target_origin,
        })

    except Exception as e:
        visual_collector_state["running"] = False
        visual_collector_state["proxy_active"] = False
        logger.error(f"[ProxyStart] 失败: {e}", exc_info=True)
        return jsonify({"ok": False, "message": f"启动代理失败: {str(e)}"}), 500


def _extract_api_info(target_url, method, raw_body):
    """从代理转发的 API 响应提取结构化信息，用于自动建立端点库"""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(target_url)
    except Exception:
        return None
    path = parsed.path  # /dev-api/xxx/list
    try:
        text = raw_body.decode("utf-8", errors="replace")
        data = json.loads(text)
    except Exception:
        return None
    fields = []
    has_pagination = False
    sample_count = 0
    rows = None
    if isinstance(data, dict):
        if isinstance(data.get("rows"), list):
            rows = data["rows"]; has_pagination = "total" in data
        elif isinstance(data.get("records"), list):
            rows = data["records"]; has_pagination = "total" in data
        elif isinstance(data.get("content"), list):
            rows = data["content"]; has_pagination = "total" in data
    elif isinstance(data, list):
        rows = data
    field_samples = {}
    if rows:
        sample_count = len(rows)
        if rows and isinstance(rows[0], dict):
            fields = list(rows[0].keys())
            # 收集每个字段的去重样本值（用于枚举识别 / 编码解码）
            seen = {f: set() for f in fields}
            for r in rows[:50]:
                if not isinstance(r, dict):
                    continue
                for f in fields:
                    v = r.get(f, None)
                    if v is None or v == "":
                        continue
                    if isinstance(v, (str, int, float, bool)) and len(seen[f]) < 8:
                        seen[f].add(v)
            field_samples = {f: list(seen[f]) for f in fields}
    else:
        if isinstance(data, dict):
            fields = [k for k in data.keys() if k not in ("total", "msg", "code", "success", "rows", "records")]
    segs = [s for s in path.split("/") if s]
    last = segs[-1] if segs else ""
    module_seg = segs[-2] if len(segs) >= 2 else ""
    return {
        "path": path, "method": method, "status": 200,
        "fields": fields[:30], "field_count": len(fields),
        "has_pagination": has_pagination, "sample_count": sample_count,
        "field_samples": field_samples,
        "module_seg": module_seg, "last_seg": last,
        "name_hint": (last.replace("List", "").replace("list", "")) or last,
    }


@app.route("/api/collector/proxy", defaults={"subpath": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.route("/api/collector/proxy/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def api_collector_proxy(subpath):
    """
    反向代理：将请求转发到目标平台，对 HTML 注入元素选择器脚本。

    所有请求方法均支持（GET 获取页面/资源，POST/PUT 转发 API 调用）。
    """
    if not visual_collector_state.get("proxy_active"):
        return jsonify({"ok": False, "message": "没有活跃的代理会话"}), 400

    target_origin = visual_collector_state.get("proxy_target_origin", "")
    cookies = visual_collector_state.get("proxy_cookies", {})

    # 构建目标 URL
    query_string = request.query_string.decode("utf-8")
    target_url = urljoin(target_origin.rstrip("/") + "/", subpath.lstrip("/"))
    if query_string:
        target_url += "?" + query_string

    logger.info(f"[Proxy] {request.method} {target_url}")

    try:
        # 转发请求
        req_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        # 转发客户端的一些关键头部
        for h in ["Accept", "Accept-Language", "Content-Type", "X-Requested-With", "Referer"]:
            if h in request.headers:
                req_headers[h] = request.headers[h]
        # 转发自定义认证头
        for h in ["Authorization"]:
            if h in request.headers:
                req_headers[h] = request.headers[h]

        body = request.get_data() or None

        if request.method == "GET":
            resp = _requests.get(target_url, headers=req_headers, cookies=cookies,
                                allow_redirects=True, timeout=30)
        elif request.method == "POST":
            resp = _requests.post(target_url, headers=req_headers, cookies=cookies,
                                 data=body, allow_redirects=True, timeout=30)
        elif request.method == "PUT":
            resp = _requests.put(target_url, headers=req_headers, cookies=cookies,
                                data=body, allow_redirects=True, timeout=30)
        elif request.method == "DELETE":
            resp = _requests.delete(target_url, headers=req_headers, cookies=cookies,
                                   allow_redirects=True, timeout=30)
        else:
            resp = _requests.request(request.method, target_url, headers=req_headers, cookies=cookies,
                                     data=body, allow_redirects=True, timeout=30)

        content_type = resp.headers.get("Content-Type", "")
        raw_body = resp.content
        status_code = resp.status_code

        # ── 自动捕获 API 请求（用于扩充端点库）──
        # 解耦：不再限定 /dev-api/ 前缀，监听任意平台的 JSON 接口（排除代理自身路径）
        _ct = resp.headers.get("Content-Type", "").lower()
        if (visual_collector_state.get("capturing") and status_code == 200
                and "json" in _ct and "/api/collector/" not in target_url):
            try:
                info = _extract_api_info(target_url, request.method, raw_body)
                if info:
                    cap = visual_collector_state.setdefault("captured_apis", {})
                    cap[info["path"]] = info
            except Exception as _e:
                logger.warning(f"[Capture] 解析接口失败 {target_url}: {_e}")

        # 构建响应头
        resp_headers = {}
        SKIP_HEADERS = {
            "transfer-encoding", "content-encoding", "content-length",
            "content-security-policy", "content-security-policy-report-only",
            "x-frame-options", "x-content-security-policy",
            "x-webkit-csp", "strict-transport-security",
            "set-cookie",  # Cookie 由代理管理，不透传
        }
        for k, v in resp.headers.items():
            if k.lower() in SKIP_HEADERS:
                continue
            resp_headers[k] = v

        # 如果请求的是非 HTML 资源（JS/CSS/图片等），但服务器返回了 HTML（可能是登录页重定向）
        # 则不注入脚本，直接返回空内容或错误
        request_ext = "." + subpath.rsplit(".", 1)[-1].lower() if "." in subpath.rsplit("/", 1)[-1] else ""
        is_static_resource = request_ext in (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                                              ".woff", ".woff2", ".ttf", ".eot", ".ico", ".map")
        if is_static_resource and "text/html" in content_type:
            # 静态资源被重定向到 HTML 页面（可能是 session 过期）
            logger.warning(f"[Proxy] 静态资源 {subpath} 返回了 HTML（可能 session 过期）, 返回空内容")
            return Response(b"", status=401, content_type=content_type)

        # 如果是 HTML 页面，注入元素选择器脚本
        if "text/html" in content_type:
            try:
                html = raw_body.decode("utf-8", errors="replace")
            except Exception:
                html = raw_body.decode("latin-1", errors="replace")

            # 1. 注入动态 URL 拦截器（必须在所有 SPA 脚本之前，拦截 webpack 动态 chunk 加载）
            html = _re.sub(r'<base[^>]*>', '', html, flags=_re.IGNORECASE)
            head_pos = html.find("<head")
            if head_pos >= 0:
                head_close = html.find(">", head_pos) + 1
                html = html[:head_close] + _origin_script(target_origin) + DYNAMIC_URL_INTERCEPTOR_JS + html[head_close:]
            else:
                html_close = html.find(">", html.find("<html")) + 1 if "<html" in html else 0
                html = html[:html_close] + _origin_script(target_origin) + DYNAMIC_URL_INTERCEPTOR_JS + html[html_close:]

            # 2. 注入 storage 恢复脚本（必须在 SPA 脚本之前执行，否则 SPA 读不到 token 会一直 loading）
            storage_data = visual_collector_state.get("proxy_storage") or {"local": {}, "session": {}}
            try:
                storage_json = json.dumps(storage_data, ensure_ascii=False)
            except Exception:
                storage_json = '{"local":{},"session":{}}'
            storage_script = (
                "<script>\n"
                "(function(){\n"
                "  try {\n"
                "    var data = " + storage_json + ";\n"
                "    if (data.local) { for (var k in data.local) { try { localStorage.setItem(k, data.local[k]); } catch(_){} } }\n"
                "    if (data.session) { for (var k in data.session) { try { sessionStorage.setItem(k, data.session[k]); } catch(_){} } }\n"
                "  } catch(e) { console.warn('[WorkBuddy Storage] 恢复失败:', e); }\n"
                "})();\n"
                "</script>"
            )
            head_pos = html.find("<head")
            if head_pos >= 0:
                head_close = html.find(">", head_pos) + 1
                html = html[:head_close] + storage_script + html[head_close:]
            else:
                # 无 <head>，注入到 <html> 后
                html_close = html.find(">", html.find("<html")) + 1 if "<html" in html else 0
                html = html[:html_close] + storage_script + html[html_close:]

            # 2. 注入 <base> 标签，让相对 URL 以代理基准解析
            # 让 base 包含当前子路径，这样相对路径能正确解析到目标服务器的对应子路径
            base_path = subpath.rstrip("/") if subpath else ""
            base_href = f"/api/collector/proxy/{base_path}/" if base_path else "/api/collector/proxy/"
            base_tag = f'<base href="{base_href}">'
            head_pos = html.find("<head")
            if head_pos >= 0:
                head_close = html.find(">", head_pos) + 1
                html = html[:head_close] + base_tag + html[head_close:]
            else:
                html_close = html.find(">", html.find("<html")) + 1 if "<html" in html else 0
                html = html[:html_close] + base_tag + html[html_close:]

            # 3. 注入元素选择器脚本到 <body> 之后（因为脚本需要 document.body）
            body_open = html.find("<body")
            if body_open >= 0:
                body_close = html.find(">", body_open) + 1

                # 3a. 注入高级配置脚本（在 ELEMENT_PICKER_JS 之前）
                adv = visual_collector_state.get("proxy_advanced", {})
                adv_config_script = f"""
<script>
window.__kwb_advanced = {{
  no_ads: {str(adv.get("no_ads", True)).lower()},
  no_anim: {str(adv.get("no_anim", True)).lower()},
  no_video: {str(adv.get("no_video", True)).lower()},
  lazy_img: {str(adv.get("lazy_img", False)).lower()},
  wait_time: {adv.get("wait_time", 2)},
  ajax_wait: {adv.get("ajax_wait", 3)},
  load_timeout: {adv.get("load_timeout", 30)}
}};
(function(){{
  if (window.__kwb_advanced.no_ads) {{
    var style = document.createElement('style');
    style.id = '__kwb_no_ads';
    style.textContent = '[class*="ad-"],[class*="-ad"],[id*="ad-"],[id*="-ad"],[class*="banner"],' +
      '[aria-label*="广告"],iframe[src*="doubleclick"],iframe[src*="googlead"]{{display:none!important}}';
    document.head.appendChild(style);
  }}
  if (window.__kwb_advanced.no_anim) {{
    var style2 = document.createElement('style');
    style2.id = '__kwb_no_anim';
    style2.textContent = '*,*::before,*::after{{animation-duration:0s!important;animation-delay:0s!important;' +
      'transition-duration:0s!important;transition-delay:0s!important}}';
    document.head.appendChild(style2);
  }}
  if (window.__kwb_advanced.no_video) {{
    var observer = new MutationObserver(function(mutations) {{
      document.querySelectorAll('video[autoplay],audio[autoplay]').forEach(function(el) {{
        el.pause(); el.removeAttribute('autoplay');
      }});
    }});
    observer.observe(document.documentElement, {{ childList: true, subtree: true }});
    // 立即检查已有元素
    document.querySelectorAll('video,audio').forEach(function(el) {{ el.pause(); }});
  }}
  if (window.__kwb_advanced.lazy_img) {{
    document.querySelectorAll('img:not([loading])').forEach(function(i) {{ i.loading = 'lazy'; }});
  }}
  // 设置超时（由 ELEMENT_PICKER_JS 在 DOM ready 后检查）
}})();
</script>
"""
                html = html[:body_close] + adv_config_script + ELEMENT_PICKER_JS + html[body_close:]
            else:
                # 无 body 标签，追加到末尾
                html += ELEMENT_PICKER_JS

            # 2. 重写绝对 URL（http(s)://target_origin/xxx → /api/collector/proxy/xxx）
            html = html.replace(target_origin + "/", "/api/collector/proxy/")

            # 3. 重写 CSS url() 中的绝对路径
            def _rewrite_css_url(m):
                url = m.group(1).strip().strip('"\'')
                if url.startswith("http") and url.startswith(target_origin):
                    rel = url[len(target_origin):]
                    return f'url("/api/collector/proxy{rel}")'
                if url.startswith("/") and not url.startswith("//") and not url.startswith("/api/collector/"):
                    return f'url("/api/collector/proxy{url}")'
                return m.group(0)

            html = _re.sub(r'url\(([^)]+)\)', _rewrite_css_url, html)

            # 4. 重写 src/href 属性（仅绝对路径 / 开头的）
            def _rewrite_attr(m):
                attr = m.group(1)
                url = m.group(2)
                if url.startswith("http:") or url.startswith("https:"):
                    if url.startswith(target_origin):
                        rel = url[len(target_origin):]
                        return f'{attr}="/api/collector/proxy{rel}"'
                    return m.group(0)
                if url.startswith("//"):
                    return m.group(0)
                if url.startswith("/api/collector/"):
                    return m.group(0)
                if url.startswith("/") and not url.startswith("//"):
                    return f'{attr}="/api/collector/proxy{url}"'
                # 相对路径（如 "js/app.js"）由 <base> 标签处理，不需要改
                return m.group(0)

            html = _re.sub(r'(src|href)="([^"]*)"', _rewrite_attr, html)
            html = _re.sub(r"(src|href)='([^']*)'", _rewrite_attr, html)

            # 5. 重写 data-src（懒加载图片）
            html = _re.sub(r'(data-src)="([^"]*)"', _rewrite_attr, html)

            # 6. 重写 srcset
            def _rewrite_srcset(m):
                full = m.group(2)
                parts = full.split(",")
                new_parts = []
                for part in parts:
                    part = part.strip()
                    space_idx = part.find(" ")
                    if space_idx > 0:
                        url_part = part[:space_idx].strip()
                        desc = part[space_idx:].strip()
                    else:
                        url_part = part
                        desc = ""
                    if url_part.startswith("http") and url_part.startswith(target_origin):
                        url_part = "/api/collector/proxy" + url_part[len(target_origin):]
                    elif url_part.startswith("/") and not url_part.startswith("//") and not url_part.startswith("/api/collector/"):
                        url_part = "/api/collector/proxy" + url_part
                    new_parts.append(url_part + (" " + desc if desc else ""))
                return f'{m.group(1)}="{", ".join(new_parts)}"'

            html = _re.sub(r'(srcset)="([^"]*)"', _rewrite_srcset, html)

            # 注入 API 拦截脚本（让 SPA 的 AJAX 请求也走代理）
            api_interceptor = """
<script>
(function(){
  if(window.__kwb_api_patched) return;
  window.__kwb_api_patched = true;
  var TARGET_ORIGIN = '""" + target_origin + """';
  var PROXY_BASE = '/api/collector/proxy';

  function shouldProxy(url) {
    if (!url || typeof url !== 'string') return false;
    if (url.startsWith(PROXY_BASE) || url.startsWith('/api/collector/')) return false;
    if (url.startsWith('blob:') || url.startsWith('data:') || url.startsWith('javascript:')) return false;
    if (url.startsWith(TARGET_ORIGIN)) return true;
    if (url.startsWith('/') && !url.startsWith('//')) return true;
    return false;
  }

  function proxyUrl(url) {
    if (url.startsWith(TARGET_ORIGIN)) return PROXY_BASE + url.substring(TARGET_ORIGIN.length);
    if (url.startsWith('/') && !url.startsWith('//')) return PROXY_BASE + url;
    return url;
  }

  // Patch fetch
  var _origFetch = window.fetch;
  window.fetch = function(input, init) {
    try {
      var url = typeof input === 'string' ? input : (input && input.url) || '';
      if (shouldProxy(url)) {
        var newUrl = proxyUrl(url);
        if (typeof input === 'string') input = newUrl;
        else if (input instanceof Request) input = new Request(newUrl, input);
      }
    } catch(_) {}
    return _origFetch.call(this, input, init);
  };

  // Patch XMLHttpRequest（修正版：通过 prototype.open 拦截，避免共享实例问题）
  var OrigXHR = window.XMLHttpRequest;
  var origXHROpen = OrigXHR.prototype.open;
  OrigXHR.prototype.open = function(method, url, async, user, password) {
    try {
      if (shouldProxy(url)) url = proxyUrl(url);
    } catch(_) {}
    return origXHROpen.call(this, method, url, async, user, password);
  };

  // Patch axios（许多 Vue 应用使用 axios）
  function patchAxios(axios) {
    if (!axios || axios.__kwbPatched) return;
    axios.__kwbPatched = true;
    if (axios.request) {
      var origReq = axios.request.bind(axios);
      axios.request = function(config) {
        if (config && config.url && shouldProxy(config.url)) {
          config = Object.assign({}, config, { url: proxyUrl(config.url) });
        }
        return origReq(config);
      };
    }
    ['get','post','put','delete','patch','head','options'].forEach(function(m){
      if (!axios[m]) return;
      var orig = axios[m].bind(axios);
      axios[m] = function(url, data, config) {
        if (typeof url === 'string' && shouldProxy(url)) url = proxyUrl(url);
        if (typeof data === 'string' && shouldProxy(data)) data = proxyUrl(data);
        return orig(url, data, config);
      };
    });
    if (axios.create) {
      var origCreate = axios.create.bind(axios);
      axios.create = function() {
        var inst = origCreate.apply(null, arguments);
        patchAxios(inst);
        return inst;
      };
    }
    if (axios.defaults) {
      var _bu = axios.defaults.baseURL;
      if (_bu && shouldProxy(_bu)) {
        axios.defaults.baseURL = proxyUrl(_bu);
      }
    }
  }
  // axios 是异步加载的，每 50ms 检查一次
  var axiosInterval = setInterval(function() {
    if (window.axios) {
      clearInterval(axiosInterval);
      patchAxios(window.axios);
    }
  }, 50);
  setTimeout(function() { clearInterval(axiosInterval); }, 10000);

  console.log('[WorkBuddy Proxy] API 拦截已启用, 目标: ' + TARGET_ORIGIN);
})();

// 通知父窗口页面已就绪
(function(){
  function notifyParent(type) {
    try { window.parent.postMessage(JSON.stringify({type: type, source: 'kwb_proxy'}), '*'); } catch(e) {}
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function(){ notifyParent('kwb_proxy_dom_ready'); });
  } else {
    notifyParent('kwb_proxy_dom_ready');
  }
  window.addEventListener('load', function(){ notifyParent('kwb_proxy_loaded'); });
  // 兜底：3秒后无论如何通知一次（SPA 可能不会触发 load）
  setTimeout(function(){ notifyParent('kwb_proxy_timeout'); }, 3000);
})();
</script>
"""
            inject_pos2 = html.find("</head>")
            if inject_pos2 > 0:
                html = html[:inject_pos2] + api_interceptor + html[inject_pos2:]
            else:
                # 无 </head>，追加到 body 开头
                body_tag = html.find("<body")
                if body_tag >= 0:
                    body_close = html.find(">", body_tag) + 1
                    html = html[:body_close] + api_interceptor + html[body_close:]

            raw_body = html.encode("utf-8")

        # 对外部 CSS 文件做 url() / @import 重写（处理 @font-face 字体、背景图等）
        if "text/css" in content_type:
            try:
                css_text = raw_body.decode("utf-8", errors="replace")
            except Exception:
                css_text = raw_body.decode("latin-1", errors="replace")

            def _rewrite_css_url_external(m):
                url = m.group(1).strip().strip('"\'')
                if url.startswith("http") and url.startswith(target_origin):
                    return f'url("/api/collector/proxy{url[len(target_origin):]}")'
                if url.startswith("/") and not url.startswith("//") and not url.startswith("/api/collector/"):
                    return f'url("/api/collector/proxy{url}")'
                return m.group(0)

            css_text = _re.sub(r'url\(([^)]+)\)', _rewrite_css_url_external, css_text)

            def _rewrite_css_import(m):
                url = m.group(1).strip().strip('"\'')
                if url.startswith("http") and url.startswith(target_origin):
                    return f'@import url("/api/collector/proxy{url[len(target_origin):]}")'
                if url.startswith("/") and not url.startswith("//") and not url.startswith("/api/collector/"):
                    return f'@import url("/api/collector/proxy{url}")'
                return m.group(0)

            css_text = _re.sub(r'@import\s+(?:url\()?["\']?([^"\')]+)["\']?\)?', _rewrite_css_import, css_text)
            raw_body = css_text.encode("utf-8")

        # 返回响应
        return Response(raw_body, status=status_code, headers=resp_headers)

    except _requests.exceptions.RequestException as e:
        logger.error(f"[Proxy] 请求失败: {target_url} - {e}")
        return jsonify({"ok": False, "message": f"代理请求失败: {str(e)}"}), 502


@app.route("/api/collector/capture_start", methods=["POST"])
def api_capture_start():
    """开始捕获代理页面发出的 API 请求（用于扩充端点库）"""
    if not visual_collector_state.get("proxy_active"):
        return jsonify({"ok": False, "message": "请先在步骤1启动代理会话"}), 400
    visual_collector_state["capturing"] = True
    visual_collector_state["captured_apis"] = {}
    return jsonify({"ok": True, "message": "已开始捕获页面 API 请求（在左侧页面操作后自动记录）"})


@app.route("/api/collector/capture_stop", methods=["POST"])
def api_capture_stop():
    visual_collector_state["capturing"] = False
    return jsonify({"ok": True, "message": "已停止捕获"})


@app.route("/api/collector/captured_apis", methods=["GET"])
def api_captured_apis():
    cap = visual_collector_state.get("captured_apis", {})
    items = sorted(cap.values(), key=lambda x: x.get("path", ""))
    return jsonify({
        "ok": True,
        "capturing": visual_collector_state.get("capturing", False),
        "count": len(items),
        "apis": items,
    })


@app.route("/api/config/endpoints/save", methods=["POST"])
def api_config_endpoints_save():
    """把用户勾选捕获到的 API 写入 endpoints.yaml 并热重载（自动推断枚举解码 + 中文名）"""
    from kangyang.config import save_endpoints as _save_endpoints, reload_config, get_config_status
    from kangyang.enum_decoder import infer_endpoint_profile
    data = request.get_json() or {}
    eps = data.get("endpoints") or []
    captured = visual_collector_state.get("captured_apis", {})
    cleaned = []
    for e in eps:
        p = (e.get("path") or "").strip()
        if not p:
            continue
        name = (e.get("name") or "").strip()
        module = (e.get("module") or "").strip()
        info = captured.get(p, {})
        samples = info.get("field_samples", {})
        # 自动推断枚举解码映射 + 中文名（用户手填优先）
        profile = infer_endpoint_profile(p, samples, user_name=name, user_module=module)
        cleaned.append({
            "module": profile["module"] or module,
            "name": profile["name"] or name or info.get("name_hint", ""),
            "path": p,
            "field_enums": profile["field_enums"],
            "field_samples": samples,
        })
    if not cleaned:
        return jsonify({"ok": False, "message": "没有有效的端点数据"}), 400
    ok, total = _save_endpoints(cleaned)
    if not ok:
        return jsonify({"ok": False, "message": "写入 endpoints.yaml 失败，请查看日志"}), 500
    reload_config()
    return jsonify({"ok": True, "message": f"已保存 {len(cleaned)} 个端点，共 {total} 个", "config": get_config_status()})


@app.route("/api/collector/element_picked", methods=["POST"])
def api_collector_element_picked():
    """
    接收来自代理页面中元素选择器的元素信息
    请求: { "session_id": "...", "element": { "tag":"td", "text":"...", "css":"...", ... } }
    """
    data = request.get_json() or {}
    elem = data.get("element") or {}
    session_id = data.get("session_id", "")

    if session_id != visual_collector_state.get("session_id", ""):
        return jsonify({"ok": False, "message": "会话不匹配"}), 400

    # 构建立即添加到 selected_elements 的规则建议
    # 前端可以通过 /api/collector/select 正式添加
    css = elem.get("css", "")
    xpath = elem.get("xpath", "")
    tag = elem.get("tag", "")
    text = elem.get("text", "")

    # 自动推断字段名
    auto_name = text[:15] if text and len(text) <= 15 else (tag + "_" + css.replace(".", "_").replace("#", "_")[:20])

    return jsonify({
        "ok": True,
        "suggestion": {
            "field_name": auto_name,
            "css_selector": css,
            "xpath": xpath,
            "extract_type": "text",
            "sample_value": text[:50] if text else "",
        },
        "element": elem,
    })


@app.route("/api/collector/proxy_stop", methods=["POST"])
def api_collector_proxy_stop():
    """停止代理会话，关闭 Playwright 浏览器"""
    global visual_collector_state
    try:
        page = visual_collector_state.get("proxy_page")
        browser = visual_collector_state.get("proxy_browser")
        context = visual_collector_state.get("proxy_context")
        if page:
            try: page.close()
            except: pass
        if context:
            try: context.close()
            except: pass
        if browser:
            try: browser.close()
            except: pass
    except Exception as e:
        logger.warning(f"[ProxyStop] 清理浏览器失败: {e}")

    visual_collector_state["proxy_active"] = False
    visual_collector_state["proxy_page"] = None
    visual_collector_state["proxy_browser"] = None
    visual_collector_state["proxy_context"] = None
    visual_collector_state["proxy_cookies"] = {}
    visual_collector_state["proxy_storage_state"] = None
    return jsonify({"ok": True, "message": "代理会话已停止"})


@app.route("/api/collector/start_session", methods=["POST"])
def api_collector_start_session():
    """
    启动可视化采集会话：登录平台、打开目标页面、截图、提取元素。

    请求: {
        "host": "http://192.168.18.143:1024",
        "username": "admin",
        "password": "admin123",
        "target": "/elderly/overview"
    }
    响应: {
        "ok": true,
        "session_id": "abc123",
        "snapshot": {
            "url": "...", "title": "...",
            "screenshot_base64": "...",        // PNG base64
            "viewport_width": 1440, "viewport_height": 900,
            "elements": [
                {"tag":"td","text":"张三","css":"...","x":200,"y":150,"w":60,"h":20,...}
            ],
            "elements_count": 150
        }
    }
    """
    global visual_collector_state

    if visual_collector_state["running"]:
        return jsonify({"ok": False, "message": "已有可视化采集会话正在运行"}), 409

    data = request.get_json()
    host = data.get("host", "").strip()
    username = data.get("username", "").strip()
    password = data.get("password", "")
    target = data.get("target", "").strip()

    if not host or not username or not target:
        return jsonify({"ok": False, "message": "请填写平台地址、账号和目标页面"}), 400

    visual_collector_state["running"] = True
    visual_collector_state["host"] = host
    visual_collector_state["username"] = username
    visual_collector_state["password"] = password
    visual_collector_state["target"] = target
    visual_collector_state["selected_elements"] = []

    try:
        snapshot = create_snapshot(
            host=host, username=username, password=password,
            target=target, headless=True,
        )

        session_id = hashlib.md5(f"{host}{target}{time.time()}".encode()).hexdigest()[:16]
        cache_snapshot(session_id, snapshot)

        visual_collector_state["session_id"] = session_id
        visual_collector_state["snapshot"] = snapshot
        visual_collector_state["running"] = False

        elements_json = [
            {
                "id": i,
                "tag": e.tag,
                "text": e.text,
                "css": e.css_selector,
                "xpath": e.xpath,
                "x": e.x,
                "y": e.y,
                "w": e.width,
                "h": e.height,
                "attrs": e.attributes,
                "parent_tag": e.parent_tag,
                "depth": e.depth,
                "visible": e.visible,
            }
            for i, e in enumerate(snapshot.elements)
        ]

        return jsonify({
            "ok": True,
            "session_id": session_id,
            "snapshot": {
                "url": snapshot.url,
                "title": snapshot.title,
                "screenshot_base64": snapshot.screenshot_base64,
                "viewport_width": snapshot.viewport_width,
                "viewport_height": snapshot.viewport_height,
                "full_page_height": snapshot.full_page_height,
                "elements": elements_json,
                "elements_count": len(elements_json),
            },
        })

    except Exception as e:
        visual_collector_state["running"] = False
        logger.error(f"[Collector] 启动会话失败: {e}", exc_info=True)
        return jsonify({"ok": False, "message": f"启动失败: {str(e)}"}), 500


@app.route("/api/collector/element_info", methods=["POST"])
def api_collector_element_info():
    """
    根据点击坐标查找最近元素。

    请求: {"session_id": "abc123", "x": 200, "y": 150}
    响应: {
        "ok": true,
        "element": {tag, text, css, xpath, ...},
        "siblings": [...],      // 兄弟元素（用于列表选择）
        "region_elements": [...] // 附近区域的所有元素
    }
    """
    data = request.get_json()
    session_id = data.get("session_id", "")
    click_x = float(data.get("x", 0))
    click_y = float(data.get("y", 0))

    snapshot = get_cached_snapshot(session_id)
    if not snapshot:
        return jsonify({"ok": False, "message": "会话不存在或已过期"}), 400

    element = find_element_by_position(snapshot, click_x, click_y)

    if not element:
        # 扩大搜索范围
        region = get_region_elements(snapshot, click_x - 100, click_y - 50, click_x + 100, click_y + 50)
        return jsonify({
            "ok": True,
            "element": None,
            "message": "附近没有找到可选元素",
            "nearby": [
                {"tag": e.tag, "text": e.text, "x": e.x, "y": e.y}
                for e in region[:10]
            ],
        })

    # 查找兄弟元素（用于列表提取）
    siblings = get_sibling_elements(snapshot, element)

    # 查找附近的同类型元素
    region = get_region_elements(snapshot, element.x - 200, element.y - 300, element.x + 200, element.y + 300)

    return jsonify({
        "ok": True,
        "element": {
            "tag": element.tag,
            "text": element.text,
            "css": element.css_selector,
            "xpath": element.xpath,
            "x": element.x,
            "y": element.y,
            "w": element.width,
            "h": element.height,
            "attrs": element.attributes,
            "parent_tag": element.parent_tag,
            "depth": element.depth,
            "best_selector": build_selector_for_element(element),
        },
        "siblings_count": min(len(siblings), 20),
        "siblings": [
            {"tag": s.tag, "text": s.text, "css": s.css_selector}
            for s in siblings[:20]
        ],
        "region_elements_count": len(region),
    })


@app.route("/api/collector/select", methods=["POST"])
def api_collector_select():
    """
    确认选择指定元素作为采集目标。

    请求: {
        "session_id": "abc123",
        "element_index": 5,         // 从 snapshop.elements 列表中的索引（旧截图模式）
        "field_name": "姓名",        // 用户指定的字段名
        "extract_type": "text",      // text / attribute / list / table
        "attribute_name": "",        // 仅 attribute 类型时用
        "is_list": false,
        // ── 代理模式（新）──
        "css_selector_override": "td.name",  // 直接从代理页面获取的选择器
        "xpath_override": "/html/body/...",   // 直接从代理页面获取的 XPath
    }
    响应: {"ok": true, "rule": {...}}
    """
    global visual_collector_state

    data = request.get_json()
    session_id = data.get("session_id", "")
    field_name = data.get("field_name", "").strip()
    extract_type = data.get("extract_type", "text")
    attribute_name = data.get("attribute_name", "")
    is_list = data.get("is_list", False)
    # 多页面 / 详情页采集相关字段
    source_url = data.get("source_url_override", "")
    scope = data.get("scope_override", "page")
    is_detail_link = bool(data.get("is_detail_link_override", False))

    # ── 代理模式：直接使用前端传来的选择器 ──
    css_override = data.get("css_selector_override", "")
    xpath_override = data.get("xpath_override", "")

    if not field_name:
        return jsonify({"ok": False, "message": "请填写字段名称"}), 400

    if css_override and xpath_override:
        # ── 代理模式：直接使用前端传来的选择器 ──
        best_selector = css_override
        xpath = xpath_override
        sample_text = ""
        text_pattern = data.get("text_pattern_override", "")

        # 去重：仅当「字段名 + 选择器 + 提取方式 + 提取属性」完全一致才算重复。
        # 注意：不能仅凭 css_selector 相同就拦截——不同字段（如同一 <a> 既要文本又要 href）
        # 或 buildSelector 退化出的相同结构选择器，都会合法地共享同一个 css 字符串。
        for existing in visual_collector_state["selected_elements"]:
            _, existing_name, existing_rule = existing
            if (existing_rule.field_name == field_name
                    and existing_rule.css_selector == best_selector
                    and existing_rule.extract_type == extract_type
                    and existing_rule.attribute_name == attribute_name):
                return jsonify({"ok": False, "message": f"'{field_name}' 已添加过（选择器与提取方式完全相同）"}), 400

    else:
        # ── 旧截图模式：从缓存中查找元素 ──
        element_index = data.get("element_index", 0)
        snapshot = get_cached_snapshot(session_id)
        if not snapshot:
            return jsonify({"ok": False, "message": "会话不存在或已过期"}), 400

        if element_index < 0 or element_index >= len(snapshot.elements):
            return jsonify({"ok": False, "message": "元素索引无效"}), 400

        element = snapshot.elements[element_index]

        # 去重：截图模式下字段名应唯一，仅当「字段名 + 元素选择器」都相同时拦截；
        # 不再仅凭 css_selector 相同就拒绝，避免不同字段共享同一结构选择器时被误拦。
        for existing in visual_collector_state["selected_elements"]:
            if existing[1] == field_name and existing[0].css_selector == element.css_selector:
                return jsonify({"ok": False, "message": f"'{field_name}' 已添加过"}), 400

        # 构建最佳选择器
        best_selector = build_selector_for_element(element)
        xpath = element.xpath
        sample_text = element.text
        text_pattern = element.text

    # 如果是表格/列表提取，优化选择器
    if extract_type in ("table", "list") or is_list:
        # 旧的截图模式可能生成 th:nth-child(N)，这种只匹配表头；
        # 改造成 tr > :nth-child(N) 可匹配整列（表头+数据行）
        m = _re.match(r"^(th|td):nth-child\((\d+)\)$", best_selector.strip())
        if m:
            best_selector = f"tr > :nth-child({m.group(2)})"
        # 若已经是更复杂/完整的形式，保留原样

    rule = ExtractionRule(
        field_name=field_name,
        css_selector=best_selector,
        xpath=xpath,
        extract_type=extract_type,
        attribute_name=attribute_name,
        sample_value=sample_text,
        is_list=is_list,
        parent_index=len(visual_collector_state["selected_elements"]),
        text_pattern=text_pattern,
        source_url=source_url,
        scope=scope,
        is_detail_link=is_detail_link,
    )

    visual_collector_state["selected_elements"].append((None, field_name, rule))

    return jsonify({
        "ok": True,
        "selected_count": len(visual_collector_state["selected_elements"]),
        "rule": {
            "field_name": rule.field_name,
            "css_selector": rule.css_selector,
            "xpath": rule.xpath,
            "extract_type": rule.extract_type,
            "attribute_name": rule.attribute_name,
            "sample_value": rule.sample_value,
            "is_list": rule.is_list,
            "source_url": rule.source_url,
            "scope": rule.scope,
            "is_detail_link": rule.is_detail_link,
        },
    })


@app.route("/api/collector/deselect", methods=["POST"])
def api_collector_deselect():
    """取消选择某个元素"""
    global visual_collector_state
    data = request.get_json()
    field_name = data.get("field_name", "").strip()

    before = len(visual_collector_state["selected_elements"])
    visual_collector_state["selected_elements"] = [
        s for s in visual_collector_state["selected_elements"]
        if s[1] != field_name
    ]
    after = len(visual_collector_state["selected_elements"])

    return jsonify({
        "ok": True,
        "removed": before - after > 0,
        "selected_count": after,
    })


@app.route("/api/collector/selected", methods=["GET"])
def api_collector_selected():
    """列出当前已选择的元素和规则"""
    return jsonify({
        "ok": True,
        "selected": [
            {
                "element": {
                    "tag": s[0].tag,
                    "text": s[0].text,
                    "css": s[0].css_selector,
                    "xpath": s[0].xpath,
                },
                "field_name": s[1],
                "rule": {
                    "field_name": s[2].field_name,
                    "css_selector": s[2].css_selector,
                    "xpath": s[2].xpath,
                    "extract_type": s[2].extract_type,
                    "attribute_name": s[2].attribute_name,
                    "sample_value": s[2].sample_value,
                    "is_list": s[2].is_list,
                    "source_url": getattr(s[2], "source_url", ""),
                    "scope": getattr(s[2], "scope", "page"),
                    "is_detail_link": getattr(s[2], "is_detail_link", False),
                },
            }
            for s in visual_collector_state["selected_elements"]
        ],
        "count": len(visual_collector_state["selected_elements"]),
    })


@app.route("/api/collector/preview", methods=["POST"])
def api_collector_preview():
    """
    预览采集结果（根据当前选中的元素规则）。

    返回: {"ok": true, "columns": [...], "rows": [...], "total": N}
    """
    global visual_collector_state

    from kangyang.visual_collector import ExtractionRule

    raw_rules = [s[2] for s in visual_collector_state["selected_elements"]]
    if not raw_rules:
        return jsonify({"ok": False, "message": "请先选择至少一个元素"}), 400

    rules = []
    for r in raw_rules:
        if isinstance(r, dict):
            rules.append(ExtractionRule(
                field_name=r.get("field_name", ""),
                css_selector=r.get("css_selector", ""),
                xpath=r.get("xpath", ""),
                extract_type=r.get("extract_type", "text"),
                attribute_name=r.get("attribute_name", ""),
                sample_value=r.get("sample_value", ""),
                is_list=r.get("is_list", False),
                parent_index=r.get("parent_index", 0),
                source_url=r.get("source_url", ""),
                scope=r.get("scope", "page"),
                is_detail_link=r.get("is_detail_link", False),
            ))
        else:
            rules.append(r)

    host = visual_collector_state["host"]
    username = visual_collector_state["username"]
    password = visual_collector_state["password"]
    target = visual_collector_state["target"]

    if not host or not target:
        return jsonify({"ok": False, "message": "会话状态异常，请重新启动"}), 400

    try:
        # 优先使用 proxy_start 保存的 storage_state（线程安全，无需重新登录）
        # 注意：不能直接传递 proxy_page 对象（Playwright sync API 对象不可跨线程使用）
        storage_state = visual_collector_state.get("proxy_storage_state")

        logger.info(f"[Collector] 预览提取, storage_state={storage_state is not None}")
        result = preview_extraction(
            host=host, username=username, password=password,
            target=target, rules=rules, headless=True,
            storage_state=storage_state,
        )
        # 缓存最近一次预览结果，供「写入 MySQL」接口直接读取（无需重新提取）
        visual_collector_state["last_preview"] = {
            "columns": result.get("columns", []),
            "rows": result.get("rows", []),
            "total": result.get("total", 0),
        }
        return jsonify({
            "ok": True,
            "columns": result["columns"],
            "rows": result["rows"][:100],
            "total": result["total"],
            "errors": result.get("errors", []),
            "diagnostics": result.get("diagnostics", []),
            "global_diag": result.get("global_diag", {}),
        })
    except Exception as e:
        return jsonify({"ok": False, "message": f"预览失败: {str(e)}"}), 500


@app.route("/api/collector/save", methods=["POST"])
def api_collector_save():
    """
    将当前会话的采集规则保存为任务。

    请求: {
        "name": "老人档案数据采集",
        "description": "采集老人档案页面的姓名、年龄、联系方式",
        "export_format": "csv",
        "pagination": {"enabled": false}
    }
    响应: {"ok": true, "task": {...}}
    """
    global visual_collector_state

    data = request.get_json()
    name = data.get("name", "").strip()
    description = data.get("description", "")
    export_format = data.get("export_format", "csv")
    pagination = data.get("pagination", {"enabled": False})
    if not isinstance(pagination, dict):
        pagination = {"enabled": False}
    pagination.setdefault("next_button_selector", "")
    pagination.setdefault("page_size", 20)
    pagination.setdefault("max_pages", 0)
    pagination.setdefault("retry_count", 3)

    if not name:
        return jsonify({"ok": False, "message": "请输入任务名称"}), 400

    raw_rules = [s[2] for s in visual_collector_state["selected_elements"]]
    if not raw_rules:
        return jsonify({"ok": False, "message": "请先选择至少一个元素"}), 400

    # 确保 rules 都是 ExtractionRule 对象（不做 dict 转换，避免 to_dict() 崩溃）
    rules = []
    for r in raw_rules:
        if isinstance(r, dict):
            rules.append(ExtractionRule(
                field_name=r.get("field_name", ""),
                css_selector=r.get("css_selector", ""),
                xpath=r.get("xpath", ""),
                extract_type=r.get("extract_type", "text"),
                attribute_name=r.get("attribute_name", ""),
                sample_value=r.get("sample_value", ""),
                is_list=r.get("is_list", False),
                parent_index=r.get("parent_index", 0),
                source_url=r.get("source_url", ""),
                scope=r.get("scope", "page"),
                is_detail_link=r.get("is_detail_link", False),
            ))
        else:
            rules.append(r)

    try:
        # 构建完整 URL
        target = visual_collector_state["target"]
        host = visual_collector_state["host"]
        full_url = target if target.startswith("http") else host.rstrip("/") + (target if target.startswith("/") else "/" + target)

        from kangyang.task_manager import create_task as tm_create
        from datetime import datetime

        task = create_task(
            name=name,
            target_url=full_url,
            description=description,
            route=target if not target.startswith("http") else "",
            login_required=True,
            login_host=host,
            rules=rules,
            pagination=pagination,
            export_format=export_format,
        )

        return jsonify({
            "ok": True,
            "task": task.to_dict(),
            "message": f"任务 '{name}' 已保存",
        })
    except Exception as e:
        return jsonify({"ok": False, "message": f"保存失败: {str(e)}"}), 500


# ── 任务管理 API ──

@app.route("/api/collector/tasks", methods=["GET"])
def api_collector_tasks_list():
    """列出所有采集任务"""
    try:
        tasks = list_tasks()
        return jsonify({"ok": True, "tasks": tasks, "total": len(tasks)})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/collector/tasks/<task_id>", methods=["GET"])
def api_collector_task_detail(task_id):
    """获取任务详情"""
    task = get_task(task_id)
    if not task:
        return jsonify({"ok": False, "message": "任务不存在"}), 404
    return jsonify({"ok": True, "task": task.to_dict()})


@app.route("/api/collector/tasks/<task_id>", methods=["PUT"])
def api_collector_task_update(task_id):
    """更新任务"""
    data = request.get_json()
    task = update_task(task_id, data)
    if not task:
        return jsonify({"ok": False, "message": "任务不存在"}), 404
    return jsonify({"ok": True, "task": task.to_dict()})


@app.route("/api/collector/tasks/<task_id>", methods=["DELETE"])
def api_collector_task_delete(task_id):
    """删除任务"""
    ok = delete_task(task_id)
    if not ok:
        return jsonify({"ok": False, "message": "任务不存在"}), 404
    return jsonify({"ok": True, "message": "任务已删除"})


@app.route("/api/collector/tasks/<task_id>/duplicate", methods=["POST"])
def api_collector_task_duplicate(task_id):
    """复制任务"""
    data = request.get_json() or {}
    new_name = data.get("name", "")
    new_task = duplicate_task(task_id, new_name)
    if not new_task:
        return jsonify({"ok": False, "message": "任务不存在或复制失败"}), 404
    return jsonify({"ok": True, "task": new_task.to_dict()})


@app.route("/api/collector/tasks/<task_id>/export", methods=["GET"])
def api_collector_task_export(task_id):
    """导出任务为 JSON"""
    json_str = export_task_json(task_id)
    if not json_str:
        return jsonify({"ok": False, "message": "任务不存在"}), 404
    buf = io.BytesIO(json_str.encode("utf-8"))
    buf.seek(0)
    return send_file(
        buf, mimetype="application/json", as_attachment=True,
        download_name=f"task_{task_id}.json"
    )


@app.route("/api/collector/tasks/import", methods=["POST"])
def api_collector_task_import():
    """导入任务"""
    if "file" not in request.files:
        return jsonify({"ok": False, "message": "请上传 JSON 文件"}), 400
    file = request.files["file"]
    try:
        json_str = file.read().decode("utf-8")
        task = import_task(json_str)
        if not task:
            return jsonify({"ok": False, "message": "JSON 格式无效"}), 400
        return jsonify({"ok": True, "task": task.to_dict()})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/collector/tasks/<task_id>/execute", methods=["POST"])
def api_collector_task_execute(task_id):
    """
    执行一个采集任务。

    请求: {
        "username": "admin",
        "password": "admin123",
        "host": "http://...",    // 覆盖任务默认
        "headless": true,
        "max_rows": 5000
    }
    响应: {"ok": true, "success": true, "data": {...}, "errors": [...]}
    """
    task = get_task(task_id)
    if not task:
        return jsonify({"ok": False, "message": "任务不存在"}), 404

    data = request.get_json() or {}
    username = data.get("username", "")
    password = data.get("password", "")
    host = data.get("host", "")
    headless = data.get("headless", True)
    max_rows = data.get("max_rows", 0)
    resume = bool(data.get("resume", False))
    start_page = int(data.get("start_page", 1) or 1)

    if host:
        task.login_host = host

    if not username and not task.login_host:
        return jsonify({"ok": False, "message": "未配置登录信息"}), 400

    try:
        # ── 优先复用 proxy 会话的 storage_state，跳过重新登录 ──
        storage_state = visual_collector_state.get("proxy_storage_state")
        logger.info(f"[TaskExecute] storage_state={'available' if storage_state else 'none'}")
        
        result = execute_collection(
            task=task,
            username=username,
            password=password,
            headless=headless,
            max_rows=max_rows,
            storage_state=storage_state,
            resume=resume,
            start_page=start_page,
        )

        if result["success"]:
            mark_task_run(task_id)

        saved_files = result.get("data", {}).get("saved_files", {}) or {}
        rel_files = {}
        collected_dir = result.get("data", {}).get("collected_dir", "")
        import os as _os
        if collected_dir:
            for _fmt, _p in saved_files.items():
                try:
                    _rel = _os.path.relpath(_p, collected_dir).replace("\\", "/")
                    rel_files[_fmt] = "/api/collector/file/" + _rel
                except Exception:
                    rel_files[_fmt] = _p
        return jsonify({
            "ok": True,
            "success": result["success"],
            "data": result["data"],
            "saved_files": rel_files,
            "errors": result.get("errors", []),
            "error": result.get("error", ""),
            "message": result.get("error", "") if not result["success"] else "",
            "task_name": task.name,
        })
    except Exception as e:
        return jsonify({"ok": False, "message": f"执行异常: {str(e)}"}), 500


@app.route("/api/collector/file/<path:filepath>", methods=["GET"])
def api_collector_file(filepath):
    """下载 data/collected 下的采集结果文件（含图片），带路径穿越防护。"""
    import os as _os
    base_dir = _os.path.normpath(_os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "data", "collected"
    ))
    target = _os.path.normpath(_os.path.join(base_dir, filepath))
    if not target.startswith(base_dir) or not _os.path.isfile(target):
        return jsonify({"ok": False, "message": "文件不存在或无权访问"}), 404
    try:
        return send_file(target, as_attachment=True)
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/collector/session_status", methods=["GET"])
def api_collector_session_status():
    """获取当前可视化采集会话状态"""
    return jsonify({
        "ok": True,
        "running": visual_collector_state["running"],
        "session_id": visual_collector_state["session_id"],
        "host": visual_collector_state["host"],
        "target": visual_collector_state["target"],
        "selected_count": len(visual_collector_state["selected_elements"]),
        "has_snapshot": visual_collector_state["snapshot"] is not None,
    })


@app.route("/api/collector/session_end", methods=["POST"])
def api_collector_session_end():
    """结束当前可视化采集会话"""
    global visual_collector_state
    session_id = visual_collector_state.get("session_id", "")
    clear_cache()
    visual_collector_state = {
        "running": False,
        "session_id": "",
        "snapshot": None,
        "selected_elements": [],
        "host": "",
        "username": "",
        "password": "",
        "target": "",
    }
    return jsonify({"ok": True, "message": "会话已结束"})


# ═══════════════ 图片下载 ═══════════════

@app.route("/api/images/download", methods=["POST"])
def api_images_download():
    """下载采集到的图片，打包为 ZIP 返回"""
    import zipfile
    import tempfile
    import shutil
    try:
        import urllib.request as urllib_req
    except ImportError:
        import urllib as urllib_req

    data = request.get_json(force=True, silent=True) or {}
    urls = data.get("urls", [])
    if not urls:
        return jsonify({"ok": False, "message": "没有要下载的图片 URL"}), 400

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 创建输出目录
    out_dir = os.path.join(PROJECT_ROOT, "output", "image_downloads")
    os.makedirs(out_dir, exist_ok=True)

    zip_path = os.path.join(out_dir, f"images_{timestamp}.zip")
    downloaded = 0
    failed = []
    used_names = set()

    # 使用临时目录收集图片再打包
    tmp_dir = tempfile.mkdtemp(prefix="imgdl_")
    try:
        for i, url in enumerate(urls):
            try:
                req = urllib_req.Request(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                )
                resp = urllib_req.urlopen(req, timeout=20)
                raw = resp.read()
                if not raw or len(raw) < 100:
                    failed.append({"url": url[:120], "error": "响应内容过小（<100字节）"})
                    continue

                # 确定文件名
                content_type = resp.headers.get("Content-Type", "image/png")
                ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                           "image/webp": ".webp", "image/svg+xml": ".svg", "image/bmp": ".bmp"}
                ext = ext_map.get(content_type.split(";")[0].strip(), ".png")

                # 从 URL 提取原始文件名
                try:
                    from urllib.parse import urlparse
                    path = urlparse(url).path
                    orig_name = os.path.basename(path) if path else ""
                except Exception:
                    orig_name = ""

                if orig_name and "." in orig_name:
                    base, _ext = os.path.splitext(orig_name)
                    if _ext.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"}:
                        # 安全文件名
                        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in orig_name)
                        if safe_name and "." in safe_name:
                            name = safe_name
                        else:
                            name = f"image_{i:04d}{ext}"
                    else:
                        name = f"image_{i:04d}{ext}"
                else:
                    name = f"image_{i:04d}{ext}"

                # 避免重名
                final_name = name
                counter = 1
                while final_name in used_names:
                    stem, fext = os.path.splitext(name)
                    final_name = f"{stem}_{counter}{fext}"
                    counter += 1
                used_names.add(final_name)

                # 写入临时目录
                filepath = os.path.join(tmp_dir, final_name)
                with open(filepath, "wb") as f:
                    f.write(raw)
                downloaded += 1
                if downloaded <= 3:
                    logger.info(f"[ImageDownload] 已下载 ({downloaded}): {url[:80]} -> {final_name}")

            except Exception as e:
                failed.append({"url": url[:120], "error": str(e)})

        if downloaded == 0:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            err_detail = "; ".join([f"{f['url'][:40]}: {f['error']}" for f in failed[:3]])
            return jsonify({"ok": False, "message": f"所有 {len(urls)} 张图片下载失败", "errors": failed[:10],
                            "detail": err_detail}), 500

        # 打包为 ZIP
        import zipfile as zf_module
        with zf_module.ZipFile(zip_path, "w", zf_module.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(tmp_dir):
                for fname in files:
                    zf.write(os.path.join(root, fname), fname)

        logger.info(f"[ImageDownload] 打包完成: {downloaded}/{len(urls)} 张, {len(failed)} 失败, {os.path.getsize(zip_path)} 字节")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return send_file(
        zip_path,
        as_attachment=True,
        download_name=f"images_{downloaded}pics_{timestamp}.zip",
        mimetype="application/zip",
    )


# ═══════════════ 启动 ═══════════════

if __name__ == "__main__":
    print("\n" + "=" * 55)
    print("  康养数据 Web 采集控制台")
    print("=" * 55)
    print(f"  访问地址: http://localhost:5000")
    print(f"  按 Ctrl+C 停止\n")
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
