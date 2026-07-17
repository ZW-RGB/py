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
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, session

logger = logging.getLogger(__name__)

# 把父目录加入 path，以导入 kangyang 模块
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from kangyang.api_client import RuoYiApiClient
from kangyang.api_crawler import KNOWN_ENDPOINTS
from kangyang.intent_parser import parse_intent, execute_custom_crawl
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


def run_crawl(host, username, password, selected_paths):
    """后台线程：执行爬取"""
    global crawl_state

    endpoints = [ep for ep in KNOWN_ENDPOINTS if ep["path"] in selected_paths]

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
    return render_template("index.html", endpoints=KNOWN_ENDPOINTS, routes=KNOWN_ROUTES)


# ═══════════════ API 路由 ═══════════════

@app.route("/api/tables")
def api_tables():
    """返回所有可爬取的数据表列表"""
    return jsonify(KNOWN_ENDPOINTS)


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
        intent = parse_intent(user_query)
        if "error" in intent:
            return jsonify({"ok": False, "message": f"意图解析失败: {intent['error']}"}), 500

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
    if not intent or not intent.get("target_api"):
        return jsonify({"ok": False, "message": "请先完成意图解析"}), 400

    custom_crawl_state["running"] = True
    custom_crawl_state["progress"] = 10
    custom_crawl_state["status_text"] = "正在连接平台..."
    custom_crawl_state["intent_info"] = intent

    try:
        custom_crawl_state["status_text"] = "正在调用 API 获取数据..."
        custom_crawl_state["progress"] = 30

        rows = execute_custom_crawl(host, username, password, intent)

        custom_crawl_state["progress"] = 90
        custom_crawl_state["status_text"] = "正在整理数据..."

        if isinstance(rows, list) and len(rows) > 0:
            if "error" in rows[0]:
                custom_crawl_state["error"] = rows[0].get("error", "未知错误")
                custom_crawl_state["running"] = False
                return jsonify({"ok": False, "message": custom_crawl_state["error"]}), 500

            # 提取缺失字段信息后过滤内部字段
            missing_fields = rows[0].get("_missing_fields", [])
            for row in rows:
                row.pop("_missing_fields", None)
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
        rows, columns = _extract_rows_from_page_result(result, user_fields, is_paginated)

        custom_crawl_state["results"] = rows
        custom_crawl_state["columns"] = columns
        custom_crawl_state["progress"] = 100
        custom_crawl_state["status_text"] = "完成"

        # 检查缺失字段
        missing_fields = [f for f in user_fields if f not in columns] if user_fields else []

        return jsonify({
            "ok": True,
            "count": len(rows),
            "columns": columns,
            "rows": rows[:200],
            "total_rows": len(rows),
            "intent": intent,
            "missing_fields": missing_fields,
            "source_type": "page",
            "page_info": {
                "title": result.get("title", ""),
                "url": result.get("url", ""),
                "total_pages": result.get("total_pages_scraped", 1),
                "tables_count": len(result.get("merged_tables", result.get("tables", []))),
            },
        })

    except Exception as e:
        custom_crawl_state["error"] = str(e)
        logger.error(f"页面采集异常: {e}", exc_info=True)
        return jsonify({"ok": False, "message": f"页面采集异常: {str(e)}"}), 500
    finally:
        custom_crawl_state["running"] = False


def _extract_rows_from_page_result(result, user_fields, is_paginated):
    """
    从页面采集结果中提取表格数据为行格式

    参数:
        result: scrape_paginated() 或 scrape_single_page() 的返回值
        user_fields: 用户指定要的字段列表
        is_paginated: 是否分页采集

    返回: (rows, columns)
    """
    rows = []
    columns = []

    if is_paginated:
        tables = result.get("merged_tables", [])
    else:
        tables = result.get("tables", [])

    if tables:
        # 取第一个表格（通常页面主体表格）
        main_table = tables[0]
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
                        if score > best_score and score >= 0.5:
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

    return rows, columns


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
}


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
        "element_index": 5,     // 从 snapshop.elements 列表中的索引
        "field_name": "姓名",    // 用户指定的字段名
        "extract_type": "text",  // text / attribute / list / table
        "attribute_name": "",    // 仅 attribute 类型时用
        "is_list": false
    }
    响应: {"ok": true, "selected_count": N}
    """
    global visual_collector_state

    data = request.get_json()
    session_id = data.get("session_id", "")
    element_index = data.get("element_index", 0)
    field_name = data.get("field_name", "").strip()
    extract_type = data.get("extract_type", "text")
    attribute_name = data.get("attribute_name", "")
    is_list = data.get("is_list", False)

    if not field_name:
        return jsonify({"ok": False, "message": "请填写字段名称"}), 400

    snapshot = get_cached_snapshot(session_id)
    if not snapshot:
        return jsonify({"ok": False, "message": "会话不存在或已过期"}), 400

    if element_index < 0 or element_index >= len(snapshot.elements):
        return jsonify({"ok": False, "message": "元素索引无效"}), 400

    element = snapshot.elements[element_index]

    # 检查是否重复选择
    for existing in visual_collector_state["selected_elements"]:
        if existing[0].css_selector == element.css_selector:
            return jsonify({"ok": False, "message": f"该元素已选为 '{existing[1]}'"}), 400

    # 构建最佳选择器
    best_selector = build_selector_for_element(element)

    # 如果是表格提取，优化选择器覆盖整列
    if extract_type in ("table", "list") or is_list:
        # 尝试从 CSS 选择器调整为更通用的行内模式
        if ":nth-child" in best_selector:
            # 对于表格列，移除 nth-child 以匹配所有行
            # 但保留列的位置（如 td 的第 N 个）
            pass

    rule = ExtractionRule(
        field_name=field_name,
        css_selector=best_selector,
        xpath=element.xpath,
        extract_type=extract_type,
        attribute_name=attribute_name,
        sample_value=element.text,
        is_list=is_list,
        parent_index=len(visual_collector_state["selected_elements"]),
    )

    visual_collector_state["selected_elements"].append((element, field_name, rule))

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
        result = preview_extraction(
            host=host, username=username, password=password,
            target=target, rules=rules, headless=True,
        )
        return jsonify({
            "ok": True,
            "columns": result["columns"],
            "rows": result["rows"][:100],
            "total": result["total"],
            "errors": result.get("errors", []),
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

    if not name:
        return jsonify({"ok": False, "message": "请输入任务名称"}), 400

    raw_rules = [s[2] for s in visual_collector_state["selected_elements"]]
    if not raw_rules:
        return jsonify({"ok": False, "message": "请先选择至少一个元素"}), 400

    def _rule_to_dict(r):
        """兼容 ExtractionRule 对象和 dict"""
        if isinstance(r, dict):
            return {
                "field_name": r.get("field_name", ""),
                "css_selector": r.get("css_selector", ""),
                "xpath": r.get("xpath", ""),
                "extract_type": r.get("extract_type", "text"),
                "attribute_name": r.get("attribute_name", ""),
                "sample_value": r.get("sample_value", ""),
                "is_list": r.get("is_list", False),
                "parent_index": r.get("parent_index", 0),
            }
        return {
            "field_name": r.field_name,
            "css_selector": r.css_selector,
            "xpath": r.xpath,
            "extract_type": r.extract_type,
            "attribute_name": r.attribute_name,
            "sample_value": r.sample_value,
            "is_list": r.is_list,
            "parent_index": r.parent_index,
        }

    rules = [_rule_to_dict(r) for r in raw_rules]

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

    if host:
        task.login_host = host

    if not username and not task.login_host:
        return jsonify({"ok": False, "message": "未配置登录信息"}), 400

    try:
        result = execute_collection(
            task=task,
            username=username,
            password=password,
            headless=headless,
            max_rows=max_rows,
        )

        if result["success"]:
            mark_task_run(task_id)

        return jsonify({
            "ok": True,
            "success": result["success"],
            "data": result["data"],
            "errors": result.get("errors", []),
            "task_name": task.name,
        })
    except Exception as e:
        return jsonify({"ok": False, "message": f"执行异常: {str(e)}"}), 500


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


# ═══════════════ 启动 ═══════════════

if __name__ == "__main__":
    print("\n" + "=" * 55)
    print("  康养数据 Web 采集控制台")
    print("=" * 55)
    print(f"  访问地址: http://localhost:5000")
    print(f"  按 Ctrl+C 停止\n")
    app.run(host="0.0.0.0", port=5000, debug=True)
