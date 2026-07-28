# -*- coding: utf-8 -*-
"""
可视化数据采集引擎 — 基于 Playwright 的无代码元素选择与数据提取

核心功能：
  1. 打开目标页面 → 截图 + 提取可点击元素的坐标和选择器
  2. 点击式元素选择 → 用户在前端点击截图位置，系统返回最近的元素
  3. CSS/XPath 选择器自动生成（智能选择策略确保稳健性）
  4. 数据预览提取 → 测试选择器，预览提取结果
  5. 采集规则生成 → 输出可保存的执行规则
"""
import base64
import hashlib
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any

from PIL import Image

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════


@dataclass
class SelectableElement:
    """页面上一个可选择的数据元素"""
    tag: str                    # div, span, td, th, a, p, li, etc.
    text: str                   # 元素文本内容（截断到 100 字符）
    xpath: str                  # XPath 选择器
    css_selector: str           # CSS 选择器（尽量简短稳定）
    x: float                    # 元素中心 X 坐标（视口坐标）
    y: float                    # 元素中心 Y 坐标（视口坐标）
    width: float                # 元素宽度
    height: float               # 元素高度
    attributes: Dict[str, str]  # id, class, data-* 等
    parent_tag: str             # 父元素标签
    depth: int                  # DOM 深度
    visible: bool               # 是否可见


@dataclass
class PageSnapshot:
    """页面快照 — 截图 + 所有可选择元素的清单"""
    url: str
    title: str
    screenshot_base64: str      # 页面截图（base64 PNG）
    viewport_width: int
    viewport_height: int
    full_page_height: int       # 整页高度
    elements: List[SelectableElement] = field(default_factory=list)
    scroll_position: float = 0.0  # 当前滚动位置（百分比）


@dataclass
class ExtractionRule:
    """单条提取规则"""
    field_name: str             # 字段名（用户自定义，如"姓名"）
    css_selector: str           # CSS 选择器
    xpath: str                  # XPath 选择器（备用）
    extract_type: str           # text / html / attribute / list / table
    attribute_name: str         # 仅 attribute 类型时用（如 href, src）
    sample_value: str           # 预览值
    is_list: bool               # 是否提取为列表
    parent_index: int           # 在父容器中的 index（用于列表提取）
    text_pattern: str = ""      # 文本兜底样本（CSS/XPath 均失败时按文本定位）
    source_url: str = ""        # 捕获时所在页面的真实路由（多页面采集时用于回放导航）
    scope: str = "page"         # page=固定来源页字段；detail=列表遍历时在各详情页提取
    is_detail_link: bool = False  # 是否为“进入详情页的链接”规则（在列表页捕获）

    def get(self, key: str, default=None):
        """兼容 dict 访问方式"""
        return getattr(self, key, default)


def _extract_value(locator, extract_type: str = "text", attribute_name: str = "") -> str:
    """
    从 Playwright Locator 中提取值，支持多种 fallback 策略。
    解决 Vue/Element UI 页面中 inner_text 为空但 value/textContent 有值的情况。
    """
    if extract_type == "attribute":
        return locator.get_attribute(attribute_name) or ""

    strategies = [
        lambda: locator.input_value(),            # input / textarea
        lambda: locator.inner_text().strip(),     # 可见文本
        lambda: locator.text_content().strip(),   # 完整文本（含隐藏）
        lambda: locator.evaluate("el => el.value || el.textContent || el.innerText || ''").strip(),
        lambda: locator.get_attribute("value") or "",
        lambda: locator.get_attribute("title") or "",
        lambda: locator.get_attribute("alt") or "",
    ]
    for fn in strategies:
        try:
            val = fn()
            if val:
                return val
        except Exception:
            continue
    return ""


@dataclass
class CollectionTask:
    """完整的采集任务配置"""
    task_id: str
    name: str
    description: str
    target_url: str             # 目标页面 URL（完整地址）
    route: str                  # Vue 路由（如果是康养平台页面）
    login_required: bool        # 是否需要登录
    login_host: str             # 登录地址
    rules: List[ExtractionRule] = field(default_factory=list)
    pagination: Dict = field(default_factory=lambda: {
        "enabled": False,
        "page_size": 20,
        "max_pages": 0,         # 0 = 自动全部
        "next_button_selector": "",
    })
    export_format: str = "csv"          # csv / json / xlsx
    created_at: str = ""
    updated_at: str = ""
    last_run: Optional[str] = None
    total_runs: int = 0

    def to_dict(self) -> dict:
        def _r(r):
            """兼容 ExtractionRule 对象和 dict"""
            if hasattr(r, 'field_name'):
                return {
                    "field_name": r.field_name,
                    "css_selector": r.css_selector,
                    "xpath": r.xpath,
                    "extract_type": r.extract_type,
                    "attribute_name": r.attribute_name,
                    "sample_value": r.sample_value,
                    "is_list": r.is_list,
                    "parent_index": r.parent_index,
                    "text_pattern": r.text_pattern,
                    "source_url": getattr(r, "source_url", ""),
                    "scope": getattr(r, "scope", "page"),
                    "is_detail_link": getattr(r, "is_detail_link", False),
                }
            return {
                "field_name": r.get("field_name", ""),
                "css_selector": r.get("css_selector", ""),
                "xpath": r.get("xpath", ""),
                "extract_type": r.get("extract_type", "text"),
                "attribute_name": r.get("attribute_name", ""),
                "sample_value": r.get("sample_value", ""),
                "is_list": r.get("is_list", False),
                "parent_index": r.get("parent_index", 0),
                "text_pattern": r.get("text_pattern", ""),
                "source_url": r.get("source_url", ""),
                "scope": r.get("scope", "page"),
                "is_detail_link": r.get("is_detail_link", False),
            }
        return {
            "task_id": self.task_id,
            "name": self.name,
            "description": self.description,
            "target_url": self.target_url,
            "route": self.route,
            "login_required": self.login_required,
            "login_host": self.login_host,
            "rules": [_r(r) for r in self.rules],
            "pagination": self.pagination,
            "export_format": self.export_format,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_run": self.last_run,
            "total_runs": self.total_runs,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CollectionTask":
        rules = [
            ExtractionRule(
                field_name=r["field_name"],
                css_selector=r["css_selector"],
                xpath=r["xpath"],
                extract_type=r.get("extract_type", "text"),
                attribute_name=r.get("attribute_name", ""),
                sample_value=r.get("sample_value", ""),
                is_list=r.get("is_list", False),
                parent_index=r.get("parent_index", 0),
                text_pattern=r.get("text_pattern", ""),
                source_url=r.get("source_url", ""),
                scope=r.get("scope", "page"),
                is_detail_link=r.get("is_detail_link", False),
            )
            for r in d.get("rules", [])
        ]
        return cls(
            task_id=d["task_id"],
            name=d["name"],
            description=d.get("description", ""),
            target_url=d.get("target_url", ""),
            route=d.get("route", ""),
            login_required=d.get("login_required", False),
            login_host=d.get("login_host", ""),
            rules=rules,
            pagination=d.get("pagination", {"enabled": False}),
            export_format=d.get("export_format", "csv"),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            last_run=d.get("last_run"),
            total_runs=d.get("total_runs", 0),
        )


# ═══════════════════════════════════════════════════
# 核心引擎
# ═══════════════════════════════════════════════════

# JS 注入脚本：提取页面上所有有意义的可视元素
_ELEMENT_EXTRACTOR_JS = """
() => {
    const results = [];
    const seen = new Set();

    // 生成唯一且尽可能稳定的 CSS 选择器
    function buildCSSSelector(el) {
        if (el.id) return '#' + CSS.escape(el.id);
        const path = [];
        let current = el;
        while (current && current !== document.body && current !== document.documentElement) {
            let selector = current.tagName.toLowerCase();
            if (current.id) {
                path.unshift('#' + CSS.escape(current.id));
                break;
            }
            if (current.className && typeof current.className === 'string') {
                const classes = current.className.trim().split(/\\s+/).filter(c => c && !c.startsWith('el-') && !c.startsWith('is-') && !c.startsWith('has-')).slice(0, 2);
                if (classes.length) selector += '.' + classes.map(c => CSS.escape(c)).join('.');
            }
            // 使用唯一 nth-child 避免歧义
            const parent = current.parentElement;
            if (parent) {
                const siblings = Array.from(parent.children).filter(s => s.tagName === current.tagName);
                if (siblings.length > 1) {
                    const idx = siblings.indexOf(current) + 1;
                    selector += ':nth-child(' + idx + ')';
                }
            }
            path.unshift(selector);
            current = current.parentElement;
            if (path.length > 5) break;
        }
        return path.join(' > ');
    }

    function buildXPath(el) {
        if (el.id) return `//*[@id="${el.id}"]`;
        const parts = [];
        let current = el;
        while (current && current !== document.body && current !== document.documentElement) {
            let tag = current.tagName.toLowerCase();
            if (current.id) {
                parts.unshift(`//*[@id="${current.id}"]`);
                break;
            }
            const parent = current.parentElement;
            if (parent) {
                const siblings = Array.from(parent.children).filter(s => s.tagName === current.tagName);
                if (siblings.length > 1) {
                    const idx = siblings.indexOf(current) + 1;
                    tag += `[${idx}]`;
                }
            }
            parts.unshift(tag);
            current = current.parentElement;
            if (parts.length > 6) break;
        }
        return '/' + parts.join('/');
    }

    function isVisible(el) {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        // 只取视口内可见元素（含部分可见）
        if (rect.bottom < -200 || rect.top > window.innerHeight + 200) return false;
        return true;
    }

    function processElement(el, depth) {
        if (depth > 12) return;
        const tag = el.tagName.toLowerCase();
        const text = (el.textContent || '').trim().substring(0, 100);
        if (!text && !['img', 'input', 'select', 'button'].includes(tag)) return;

        const rect = el.getBoundingClientRect();
        const key = `${tag}|${text}|${Math.round(rect.x)}|${Math.round(rect.y)}`;
        if (seen.has(key)) return;
        seen.add(key);

        const attrs = {};
        for (const attr of el.attributes) {
            if (['id', 'class', 'data-field', 'data-label', 'name', 'type', 'href', 'src', 'placeholder', 'title', 'role'].includes(attr.name)) {
                attrs[attr.name] = attr.value.substring(0, 200);
            }
        }

        results.push({
            tag,
            text,
            xpath: buildXPath(el),
            css: buildCSSSelector(el),
            x: Math.round(rect.x + rect.width / 2),
            y: Math.round(rect.y + rect.height / 2),
            w: Math.round(rect.width),
            h: Math.round(rect.height),
            attrs,
            parentTag: el.parentElement ? el.parentElement.tagName.toLowerCase() : '',
            depth,
            visible: isVisible(el),
        });
    }

    // 策略1: 遍历表格单元格（最优先）
    document.querySelectorAll('td, th').forEach(el => processElement(el, 3));

    // 策略2: 带有文本内容的元素（段落/标题/列表项/标签/span）
    document.querySelectorAll('p, h1, h2, h3, h4, h5, h6, li, label, span, a, div, button, strong, em, dt, dd').forEach(el => {
        const text = (el.textContent || '').trim();
        if (text.length > 0 && text.length < 200 && el.children.length <= 2) {
            processElement(el, 4);
        }
    });

    // 策略3: 表单元素
    document.querySelectorAll('input, select, textarea').forEach(el => {
        processElement(el, 3);
    });

    // 策略4: 图片
    document.querySelectorAll('img').forEach(el => {
        if (el.src && !el.src.startsWith('data:')) {
            processElement(el, 3);
        }
    });

    // 去重：合并重叠元素（取最小的合理元素）
    // 如果有多个元素中心接近，保留非 div/span 的或文本更短的
    const merged = [];
    const used = new Set();
    for (let i = 0; i < results.length; i++) {
        if (used.has(i)) continue;
        let best = results[i];
        for (let j = i + 1; j < results.length; j++) {
            if (used.has(j)) continue;
            const a = results[i], b = results[j];
            const dist = Math.abs(a.x - b.x) + Math.abs(a.y - b.y);
            if (dist < 15) {
                // 优先选择非 div/span 的，或文本更精确的
                if ((a.tag === 'div' || a.tag === 'span') && !(b.tag === 'div' || b.tag === 'span')) {
                    best = b; used.add(i);
                } else if ((b.tag === 'div' || b.tag === 'span') && !(a.tag === 'div' || a.tag === 'span')) {
                    best = a; used.add(j);
                } else if (b.text.length > 0 && b.text.length <= a.text.length) {
                    best = b; used.add(i);
                } else {
                    used.add(j);
                }
            }
        }
        if (!used.has(i)) merged.push(best);
    }

    return merged;
}
"""


def create_snapshot(
    host: str,
    username: str,
    password: str,
    target: str,
    headless: bool = True,
) -> PageSnapshot:
    """
    创建页面快照：打开目标页面并截图，提取所有可选择元素。

    参数:
        host: 康养平台地址 (http://192.168.18.143:1024)
        username, password: 登录凭证
        target: Vue 路由 (/elderly/overview) 或完整 URL
        headless: 是否使用无头模式

    返回:
        PageSnapshot — 包含截图 base64 和元素列表
    """
    from playwright.sync_api import sync_playwright

    # 构建完整 URL
    if target.startswith("http"):
        full_url = target
    else:
        host_clean = host.rstrip("/")
        target_clean = target if target.startswith("/") else "/" + target
        full_url = host_clean + target_clean

    logger.info(f"[VisualCollector] 创建页面快照: {full_url}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = context.new_page()

        # 调试截图目录
        debug_dir = os.environ.get("KWB_DEBUG_DIR", os.path.join(os.path.dirname(__file__), "..", "debug_snapshots"))
        os.makedirs(debug_dir, exist_ok=True)

        def _save_debug(name: str):
            """保存调试截图到本地，方便排查空白问题"""
            try:
                path = os.path.join(debug_dir, f"{name}_{int(time.time())}.png")
                page.screenshot(path=path, full_page=False, type="png")
                logger.info(f"[VisualCollector] 调试截图已保存: {path}")
                return path
            except Exception as e:
                logger.warning(f"[VisualCollector] 调试截图保存失败: {e}")
                return None

        def _page_stats():
            """返回当前页面基本统计"""
            try:
                stats = page.evaluate("""() => {
                    const body = document.body;
                    const html = document.documentElement;
                    return {
                        url: location.href,
                        title: document.title,
                        bodyTextLength: body ? body.innerText.length : 0,
                        bodyChildCount: body ? body.children.length : 0,
                        documentWidth: html ? html.scrollWidth : 0,
                        documentHeight: html ? html.scrollHeight : 0,
                    };
                }""")
                return stats
            except Exception:
                return {}

        try:
            # ── 登录 ──
            login_url = host.rstrip("/") + "/login"
            logger.info(f"[VisualCollector] 登录 {login_url}")
            try:
                page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                logger.error(f"[VisualCollector] 访问登录页失败: {e}")
                _save_debug("login_page_error")
                raise RuntimeError(f"无法访问登录页 {login_url}，请确认平台地址正确且服务器可访问。原始错误: {e}")

            # 等待登录表单渲染
            try:
                page.wait_for_selector(
                    'input[placeholder*="账号"], input[name="username"], input[type="text"]',
                    timeout=15000
                )
            except Exception:
                stats = _page_stats()
                logger.warning(f"[VisualCollector] 未检测到登录表单，当前页面状态: {stats}")
                _save_debug("login_form_missing")
                raise RuntimeError(f"登录页未检测到账号输入框。当前页面: {stats.get('url')}, 标题: {stats.get('title')}")

            # 填写登录表单
            page.fill('input[placeholder*="账号"], input[name="username"]', username)
            page.fill('input[placeholder*="密码"], input[name="password"]', password)

            # 点击登录按钮（兼容 Element UI / 若依 多种按钮样式）
            login_btn_sel = (
                'button:has-text("登录"), button:has-text("登 录"), '
                'button[type="submit"], .login-btn, button.el-button:has-text("登录")'
            )
            login_btn = page.locator(login_btn_sel)
            count = login_btn.count()
            if count == 0:
                # 最后兜底：尝试通过点击任意 button 或 el-button
                login_btn = page.locator('button, .el-button')
                count = login_btn.count()
                logger.warning(f"[VisualCollector] 未匹配到登录按钮选择器，兜底找到 {count} 个按钮")
                _save_debug("login_btn_fallback")

            if count > 0:
                login_btn.first.click()
                logger.info("[VisualCollector] 已点击登录按钮")
            else:
                stats = _page_stats()
                _save_debug("login_btn_missing")
                raise RuntimeError(
                    f"登录页未找到登录按钮。当前页面: {stats.get('url')}, "
                    f"页面内容长度: {stats.get('bodyTextLength', 0)}。"
                    f"可能是页面结构与预期不符，请检查 debug_snapshots/ 目录下的截图。"
                )

            # 等待登录完成（跳转到首页）
            time.sleep(1)
            if "/login" in page.url.lower():
                try:
                    page.wait_for_url(
                        lambda u: "/login" not in u.lower(),
                        timeout=30000,
                        wait_until="domcontentloaded"
                    )
                except Exception:
                    # fallback：等待侧边栏或首页特征元素出现
                    logger.warning("[VisualCollector] wait_for_url 超时，尝试元素等待...")
                    try:
                        page.wait_for_selector(
                            '.sidebar-container, .navbar, .app-main, .el-menu, .main-container',
                            timeout=10000
                        )
                    except Exception as inner_e:
                        _save_debug("login_redirect_timeout")
                        raise RuntimeError(f"登录后跳转超时，请检查账号密码或网络。当前 URL: {page.url}") from inner_e
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                logger.warning("[VisualCollector] domcontentloaded 等待超时，继续执行")
            page.wait_for_timeout(1500)
            logger.info(f"[VisualCollector] 登录成功，当前 URL: {page.url}")

            # ── 导航到目标页面 ──
            logger.info(f"[VisualCollector] 导航到目标页面: {full_url}")
            try:
                page.goto(full_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                _save_debug("target_nav_error")
                raise RuntimeError(f"导航到目标页面失败: {full_url}, 错误: {e}")

            # 等待 Vue/前端框架渲染
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                logger.warning("[VisualCollector] 目标页 domcontentloaded 等待超时")
            page.wait_for_timeout(1500)

            # 轮询检查页面是否有实际内容，最多等 10 秒
            for _ in range(20):
                stats = _page_stats()
                if stats.get("bodyTextLength", 0) > 50 and stats.get("bodyChildCount", 0) > 0:
                    break
                time.sleep(0.5)
            logger.info(f"[VisualCollector] 目标页面状态: {stats}")

            # 如果页面仍为空，尝试刷新一次
            if stats.get("bodyTextLength", 0) < 20:
                logger.warning("[VisualCollector] 目标页面内容极少，尝试刷新...")
                page.reload(wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)
                stats = _page_stats()
                logger.info(f"[VisualCollector] 刷新后状态: {stats}")

            # ── 获取页面信息 ──
            title = page.title()
            viewport = page.viewport_size or {"width": 1440, "height": 900}

            # ── 截图 ──
            _save_debug("before_final_screenshot")
            screenshot_bytes = page.screenshot(full_page=False, type="png")
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

            # 获取整页高度
            full_page_height = page.evaluate("() => document.body.scrollHeight")

            # ── 提取元素 ──
            raw_elements = page.evaluate(_ELEMENT_EXTRACTOR_JS)

            elements = []
            for el_data in raw_elements:
                elem = SelectableElement(
                    tag=el_data.get("tag", "div"),
                    text=el_data.get("text", ""),
                    xpath=el_data.get("xpath", ""),
                    css_selector=el_data.get("css", ""),
                    x=float(el_data.get("x", 0)),
                    y=float(el_data.get("y", 0)),
                    width=float(el_data.get("w", 0)),
                    height=float(el_data.get("h", 0)),
                    attributes=el_data.get("attrs", {}),
                    parent_tag=el_data.get("parentTag", ""),
                    depth=int(el_data.get("depth", 0)),
                    visible=bool(el_data.get("visible", False)),
                )
                elements.append(elem)

            logger.info(f"[VisualCollector] 快照完成: {len(elements)} 个元素, 截图 {len(screenshot_b64) // 1024} KB")

            return PageSnapshot(
                url=full_url,
                title=title,
                screenshot_base64=screenshot_b64,
                viewport_width=viewport["width"],
                viewport_height=viewport["height"],
                full_page_height=full_page_height,
                elements=elements,
            )

        except Exception as e:
            logger.error(f"[VisualCollector] 快照创建失败: {e}", exc_info=True)
            raise
        finally:
            browser.close()


def find_element_by_position(snapshot: PageSnapshot, click_x: float, click_y: float) -> Optional[SelectableElement]:
    """
    根据点击坐标查找最近的元素。

    返回距离点击位置最近的可选择元素（欧几里得距离最小）。
    如果所有元素距离都超过 30px，返回 None。
    """
    best_elem = None
    best_dist = float("inf")

    for elem in snapshot.elements:
        # 只考虑可见元素
        if not elem.visible:
            continue
        # 跳过超大容器（可能是整个页面区域）
        if elem.width > 1000 and elem.height > 500:
            continue

        dx = click_x - elem.x
        dy = click_y - elem.y
        dist = (dx * dx + dy * dy) ** 0.5

        if dist < best_dist:
            best_dist = dist
            best_elem = elem

    if best_dist <= 30 and best_elem:
        return best_elem
    return None


def get_region_elements(
    snapshot: PageSnapshot,
    x1: float, y1: float, x2: float, y2: float,
) -> List[SelectableElement]:
    """获取矩形区域内的所有元素"""
    return [
        e for e in snapshot.elements
        if e.visible
        and e.x >= x1 and e.x <= x2
        and e.y >= y1 and e.y <= y2
    ]


def get_sibling_elements(
    snapshot: PageSnapshot,
    reference: SelectableElement,
    max_count: int = 50,
) -> List[SelectableElement]:
    """
    获取与参考元素同级的兄弟元素列表（用于列表/表格数据提取）。
    匹配条件：相同 tag + 相同 parent_tag + 垂直排列（y 递增）
    """
    siblings = []
    ref_css_base = reference.css_selector.rsplit(":", 1)[0] if ":" in reference.css_selector else reference.css_selector

    for elem in snapshot.elements:
        if elem is reference:
            continue
        if elem.tag != reference.tag:
            continue
        if elem.parent_tag != reference.parent_tag:
            continue

        # X 坐标相近（同一列）+ Y 坐标不同（不同行）
        x_diff = abs(elem.x - reference.x)
        y_diff = abs(elem.y - reference.y)

        if x_diff < 120 and y_diff > 10:
            siblings.append(elem)

    # 按 Y 坐标排序
    siblings.sort(key=lambda e: e.y)
    return siblings[:max_count]


def _collect_page_diagnostics(page) -> Dict[str, Any]:
    """收集页面的诊断信息：控制台错误 + 截图"""
    diag = {}

    # 收集 console 错误
    console_errors: List[str] = []
    try:
        page.evaluate("""() => {
            if (!window.__kwb_console_errors) {
                window.__kwb_console_errors = [];
                var origError = console.error;
                console.error = function() {
                    window.__kwb_console_errors.push(Array.from(arguments).join(' '));
                    return origError.apply(console, arguments);
                };
            }
        }""")
        console_errors = page.evaluate("() => window.__kwb_console_errors || []")
        diag["console_errors"] = console_errors[:20]
    except Exception as e:
        logger.warning(f"[VisualCollector] 收集 console 错误失败: {e}")

    # 截图
    try:
        screenshot_dir = os.path.join(tempfile.gettempdir(), "workbuddy_previews")
        os.makedirs(screenshot_dir, exist_ok=True)
        screenshot_path = os.path.join(screenshot_dir, f"preview_{int(time.time())}.png")
        page.screenshot(path=screenshot_path, full_page=False)
        diag["screenshot"] = screenshot_path
        logger.info(f"[VisualCollector] 截图已保存: {screenshot_path}")
    except Exception as e:
        logger.warning(f"[VisualCollector] 截图失败: {e}")

    return diag


def _run_extraction_on_page(
    page,
    rules: List[ExtractionRule],
    full_url: str = "",
    console_errors: Optional[List[str]] = None,
    screenshot_path: str = "",
) -> Dict[str, Any]:
    """
    在已有 page 上执行规则提取（不创建新浏览器）。

    参数:
        page: Playwright Page 对象（已登录、已加载目标页面）
        rules: 提取规则列表
        full_url: 目标 URL（日志/诊断用）
        console_errors: 页面控制台错误列表（在提取前收集）
        screenshot_path: 页面截图保存路径

    返回:
        {"columns": [...], "rows": [...], "total": N, "errors": [...], "diagnostics": [...]}
    """
    columns = []
    all_rows = []
    errors = []
    diagnostics = []
    global_diag = {}  # 全局诊断（非逐字段）

    # ── 页面诊断信息 ──
    try:
        actual_url = page.url
        actual_title = page.title()
        logger.info(f"[VisualCollector] 页面诊断: URL={actual_url}, Title='{actual_title}'")
        global_diag["page_url"] = actual_url
        global_diag["page_title"] = actual_title

        # 检查是否跳转到登录页
        if "/login" in actual_url.lower():
            diagnostics.append({
                "field": "*",
                "selector": "",
                "reason": f"预览页面跳转到了登录页 ({actual_url})，认证可能已失效。请重新启动代理会话。",
            })
            return {
                "columns": [],
                "rows": [],
                "total": 0,
                "errors": [{"field": "*", "selector": "", "error": f"页面重定向到登录页: {actual_url}"}],
                "diagnostics": diagnostics,
            }
    except Exception as diag_err:
        logger.warning(f"[VisualCollector] 页面诊断失败: {diag_err}")

    # ── 全局页面信息收集（一次性） ──
    try:
        global_diag["el_table_count"] = page.locator('.el-table').count()
        global_diag["body_text_preview"] = (page.inner_text('body') or '')[:500]
        global_diag["body_html_preview"] = (page.inner_html('body') or '')[:2000]
        logger.info(f"[VisualCollector] 全局诊断: .el-table={global_diag['el_table_count']}, "
                    f"body_text='{global_diag['body_text_preview'][:100]}'")

        if console_errors:
            global_diag["console_errors"] = console_errors[:10]  # 最多10条
            for err in console_errors[:3]:
                logger.info(f"[VisualCollector] 控制台错误: {err}")

        if screenshot_path:
            global_diag["screenshot"] = screenshot_path
    except Exception as diag_err:
        logger.warning(f"[VisualCollector] 全局诊断收集失败: {diag_err}")

    for rule in rules:
        field_name = rule.field_name if hasattr(rule, "field_name") else rule.get("field_name", "")
        css_selector = rule.css_selector if hasattr(rule, "css_selector") else rule.get("css_selector", "")
        xpath = rule.xpath if hasattr(rule, "xpath") else rule.get("xpath", "")
        extract_type = rule.extract_type if hasattr(rule, "extract_type") else rule.get("extract_type", "text")
        attribute_name = rule.attribute_name if hasattr(rule, "attribute_name") else rule.get("attribute_name", "")
        is_list = rule.is_list if hasattr(rule, "is_list") else rule.get("is_list", False)
        text_pattern = rule.text_pattern if hasattr(rule, "text_pattern") else rule.get("text_pattern", "")

        columns.append(field_name)
        try:
            els = page.locator(css_selector)
            count = els.count()
            logger.info(f"[VisualCollector] 字段 '{field_name}': CSS '{css_selector}' 匹配 {count} 个元素")

            # CSS 选择器匹配为 0，尝试 XPath 回退
            if count == 0 and xpath:
                logger.info(f"[VisualCollector]   → CSS 无匹配，尝试 XPath: {xpath}")
                try:
                    els = page.locator(f"xpath={xpath}")
                    count = els.count()
                    logger.info(f"[VisualCollector]   → XPath 匹配 {count} 个元素")
                except Exception:
                    count = 0

            # 仍然 0，尝试部分选择器匹配
            if count == 0 and '.' in css_selector:
                parts = css_selector.split(' ')
                if len(parts) > 0:
                    simple_sel = parts[0]
                    logger.info(f"[VisualCollector]   → 尝试简化选择器: {simple_sel}")
                    try:
                        els = page.locator(simple_sel)
                        count = els.count()
                        logger.info(f"[VisualCollector]   → 简化选择器匹配 {count} 个元素")
                    except Exception:
                        count = 0

            # 仍然 0，尝试按文本兜底（text_pattern）：匹配不含子元素且文本包含的叶子节点
            if count == 0 and text_pattern:
                try:
                    safe_pat = text_pattern.replace("\\", "\\\\").replace("'", "\\'")
                    text_xpath = f"xpath=//*[not(*) and contains(normalize-space(.), '{safe_pat}')]"
                    els = page.locator(text_xpath)
                    count = els.count()
                    logger.info(f"[VisualCollector]   → 文本兜底匹配 {count} 个元素 (pattern='{text_pattern[:30]}')")
                except Exception:
                    count = 0

            if count == 0:
                # 收集页面上实际存在的元素信息用于诊断
                diag_info = {
                    "field": field_name,
                    "selector": css_selector,
                    "xpath": xpath,
                    "reason": f"选择器在目标页面上未匹配到任何元素。当前页面 URL: {global_diag.get('page_url', actual_url if 'actual_url' in dir() else 'unknown')}",
                    "el_table_count": global_diag.get("el_table_count", -1),
                }
                diagnostics.append(diag_info)
                errors.append({
                    "field": field_name,
                    "selector": css_selector,
                    "error": f"选择器未匹配: {css_selector}",
                })

            values = []
            for i in range(min(count, 200)):
                val = _extract_value(els.nth(i), extract_type, attribute_name)
                values.append(val)
                if i < 3:
                    snippet = val[:80] + '...' if len(val) > 80 else val
                    logger.info(f"[VisualCollector]   - 第 {i+1} 个值: '{snippet}'")

            # 填充到行
            for row_idx in range(len(values)):
                while row_idx >= len(all_rows):
                    all_rows.append({})
                all_rows[row_idx][field_name] = values[row_idx]

        except Exception as e:
            logger.error(f"[VisualCollector] 字段 '{field_name}' 提取异常: {e}")
            diagnostics.append({
                "field": field_name,
                "selector": css_selector,
                "error": str(e),
            })
            errors.append({"field": field_name, "selector": css_selector, "error": str(e)})

    total = len(all_rows)
    logger.info(f"[VisualCollector] 预览完成: {total} 行, {len(columns)} 列, {len(errors)} 个错误")

    result = {
        "columns": columns,
        "rows": all_rows[:200],
        "total": total,
        "errors": errors,
    }
    if diagnostics:
        result["diagnostics"] = diagnostics
    if global_diag:
        result["global_diag"] = global_diag
    return result


def preview_extraction(
    host: str,
    username: str,
    password: str,
    target: str,
    rules: List[ExtractionRule],
    headless: bool = True,
    storage_state: Optional[Dict[str, Any]] = None,  # 可选：复用认证状态（避免重新登录）
) -> Dict[str, Any]:
    """
    预览采集规则提取结果。

    参数:
        rules: 提取规则列表
        storage_state: 可选，Playwright storage_state 格式的认证状态。
                      传入后跳过登录，直接用该状态创建 context。
                      （从 proxy_start 的 context.storage_state() 获取，线程安全）

    返回:
        {"columns": [...], "rows": [...], "total": N, "errors": [...]}
    """
    from playwright.sync_api import sync_playwright

    if target.startswith("http"):
        full_url = target
    else:
        full_url = host.rstrip("/") + (target if target.startswith("/") else "/" + target)

    logger.info(f"[VisualCollector] 预览提取: {full_url}, {len(rules)} 条规则")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)

        # ── 如果有已认证的 storage_state，直接用它创建 context ──
        if storage_state:
            logger.info(f"[VisualCollector] 使用 storage_state 创建已认证 context "
                        f"(cookies: {len(storage_state.get('cookies', []))} 条)")
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
                storage_state=storage_state,
            )
            page = context.new_page()
            try:
                # 导航到目标页面（已认证，无需登录）
                # 使用 networkidle 等待 SPA 完成初始渲染和数据加载
                logger.info(f"[VisualCollector] 导航到 {full_url} (networkidle)")
                page.goto(full_url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(1000)

                # 验证是否跳转到登录页
                actual_url = page.url
                logger.info(f"[VisualCollector] 当前页面 URL: {actual_url}")
                if "/login" in actual_url.lower():
                    logger.warning(f"[VisualCollector] storage_state 认证失败，页面在登录页 {actual_url}")
                    context.close()
                else:
                    # 等待 Element UI 表格完全渲染
                    try:
                        page.wait_for_selector('.el-table, table, [class*="table"]', timeout=15000)
                        logger.info("[VisualCollector] 表格已渲染")
                    except Exception:
                        logger.info("[VisualCollector] 未检测到表格元素，使用固定等待")
                    page.wait_for_timeout(2000)

                    # 收集诊断：控制台错误 + 截图
                    page_diag = _collect_page_diagnostics(page)
                    result = _run_extraction_on_page(
                        page, rules, full_url,
                        console_errors=page_diag.get("console_errors"),
                        screenshot_path=page_diag.get("screenshot", ""),
                    )
                    context.close()
                    # 如果提取成功（非登录页），直接返回
                    if not any(e.get("error", "").startswith("页面重定向到登录页") for e in result.get("errors", [])):
                        return result

                    # 否则回退到手动登录
                    logger.info("[VisualCollector] storage_state 结果异常，回退到手动登录")
            except Exception as e:
                logger.warning(f"[VisualCollector] storage_state 方式失败: {e}，回退到手动登录", exc_info=True)
                context.close()

        # ── 回退：新建浏览器并手动登录 ──
        logger.info("[VisualCollector] 手动登录方式")
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = context.new_page()

        try:
            # 登录
            login_url = host.rstrip("/") + "/login"
            logger.info(f"[VisualCollector] 导航到登录页: {login_url}")
            page.goto(login_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(1000)
            page.fill('input[placeholder*="账号"], input[name="username"]', username)
            page.fill('input[placeholder*="密码"], input[name="password"]', password)
            # 登录按钮（兼容 Element UI / 若依）
            btn_sel = 'button:has-text("登录"), button:has-text("登 录"), button[type="submit"], .login-btn, button.el-button:has-text("登录")'
            login_btn = page.locator(btn_sel)
            if login_btn.count() == 0:
                login_btn = page.locator('button, .el-button')
            login_btn.first.click()
            logger.info("[VisualCollector] 已点击登录按钮，等待跳转...")
            page.wait_for_url(lambda u: "/login" not in u.lower(), timeout=30000, wait_until="networkidle")
            page.wait_for_timeout(2000)

            # 导航到目标页面
            logger.info(f"[VisualCollector] 导航到目标页面: {full_url}")
            page.goto(full_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(1000)

            # 验证
            actual_url = page.url
            logger.info(f"[VisualCollector] 当前页面 URL: {actual_url}")
            try:
                page.wait_for_selector('.el-table, table, [class*="table"]', timeout=15000)
                logger.info("[VisualCollector] 表格已渲染")
            except Exception:
                logger.info("[VisualCollector] 未检测到表格元素，使用固定等待")
            page.wait_for_timeout(2000)

            # 收集诊断：控制台错误 + 截图
            page_diag = _collect_page_diagnostics(page)
            return _run_extraction_on_page(
                page, rules, full_url,
                console_errors=page_diag.get("console_errors"),
                screenshot_path=page_diag.get("screenshot", ""),
            )

        except Exception as e:
            logger.error(f"[VisualCollector] 预览失败: {e}", exc_info=True)
            return {"columns": [], "rows": [], "total": 0, "errors": [{"field": "*", "error": str(e)}]}
        finally:
            context.close()
            browser.close()


def _download_image(url, save_dir, cookies=None, timeout=30):
    """下载图片到 save_dir，返回本地相对路径（如 images/xxx.jpg）；失败返回 ''。"""
    try:
        import requests
        os.makedirs(save_dir, exist_ok=True)
        ext = os.path.splitext(url.split('?')[0])[1].lower()
        if ext not in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg'):
            ext = '.jpg'
        fname = hashlib.md5(url.encode('utf-8')).hexdigest()[:16] + ext
        fpath = os.path.join(save_dir, fname)
        if os.path.exists(fpath):
            return os.path.relpath(fpath, os.path.dirname(save_dir)).replace('\\', '/')
        req_cookies = {}
        if cookies:
            for c in cookies:
                if isinstance(c, dict):
                    req_cookies[c.get("name", "")] = c.get("value", "")
        headers = {"User-Agent": "Mozilla/5.0", "Referer": url}
        r = requests.get(url, headers=headers, cookies=req_cookies, timeout=timeout)
        if r.status_code == 200 and r.content:
            with open(fpath, 'wb') as f:
                f.write(r.content)
            return os.path.relpath(fpath, os.path.dirname(save_dir)).replace('\\', '/')
    except Exception as e:
        logger.warning(f"[VisualCollector] 图片下载失败 {url}: {e}")
    return ""


def _save_collection_files(task, columns, rows, out_dir, export_format):
    """按 export_format 将采集结果落地为文件，返回 {格式: 路径}。"""
    import csv as _csv
    saved = {}
    os.makedirs(out_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    name_hint = re.sub(r'[^\w\u4e00-\u9fa5-]', '_', (task.name or 'data'))[:40] or 'data'
    if export_format in ('csv', 'all'):
        path = os.path.join(out_dir, f"{name_hint}_{ts}.csv")
        with open(path, 'w', encoding='utf-8-sig', newline='', errors='replace') as f:
            w = _csv.writer(f)
            w.writerow(columns)
            for r in rows:
                w.writerow([r.get(c, '') for c in columns])
        saved['csv'] = path
    if export_format in ('json', 'all'):
        path = os.path.join(out_dir, f"{name_hint}_{ts}.json")
        with open(path, 'w', encoding='utf-8', errors='replace') as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        saved['json'] = path
    if export_format in ('txt', 'all'):
        path = os.path.join(out_dir, f"{name_hint}_{ts}.txt")
        with open(path, 'w', encoding='utf-8', errors='replace') as f:
            for r in rows:
                f.write(' | '.join(str(r.get(c, '')) for c in columns) + '\n')
        saved['txt'] = path
    if export_format in ('xlsx', 'all'):
        try:
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(columns)
            for r in rows:
                ws.append([r.get(c, '') for c in columns])
            path = os.path.join(out_dir, f"{name_hint}_{ts}.xlsx")
            wb.save(path)
            saved['xlsx'] = path
        except Exception as e:
            logger.warning(f"[VisualCollector] xlsx 导出失败，回退 csv: {e}")
            if 'csv' not in saved:
                path = os.path.join(out_dir, f"{name_hint}_{ts}.csv")
                with open(path, 'w', encoding='utf-8-sig', newline='', errors='replace') as f:
                    w = _csv.writer(f)
                    w.writerow(columns)
                    for r in rows:
                        w.writerow([r.get(c, '') for c in columns])
                saved['csv'] = path
    return saved


def execute_collection(
    task: CollectionTask,
    username: str = "",
    password: str = "",
    headless: bool = True,
    max_rows: int = 0,
    storage_state: Optional[Dict[str, Any]] = None,
    resume: bool = False,
    start_page: int = 1,
) -> Dict[str, Any]:
    """
    执行一个完整的采集任务（供 CLI 和 API 调用）。

    参数:
        task: 采集任务配置
        username, password: 覆盖任务中的登录凭证
        headless: 无头模式
        max_rows: 最大行数限制（0 = 不限制）
        storage_state: Playwright storage_state 格式的认证状态（可选）。
                      传入后跳过登录，直接用该状态创建已认证 context。

    返回:
        {"success": bool, "data": {"columns": [...], "rows": [...], "total": N}, "errors": [...]}
    """
    if not task.rules:
        return {"success": False, "error": "任务未配置提取规则", "data": {"columns": [], "rows": [], "total": 0}}

    host = task.login_host
    user = username or ""
    pwd = password or ""

    if not host:
        return {"success": False, "error": "未配置登录地址", "data": {"columns": [], "rows": [], "total": 0}}

    target = task.target_url or task.route
    if not target:
        return {"success": False, "error": "未配置目标页面", "data": {"columns": [], "rows": [], "total": 0}}

    from playwright.sync_api import sync_playwright

    if target.startswith("http"):
        full_url = target
    else:
        full_url = host.rstrip("/") + (target if target.startswith("/") else "/" + target)

    logger.info(f"[VisualCollector] 执行采集任务: {task.name} → {full_url}")

    # ── 断点续采与多格式落地的准备 ──
    retry_count = max(1, int(task.pagination.get("retry_count", 3) or 3))
    storage_cookies = storage_state.get("cookies") if storage_state else None
    collected_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "collected", task.task_id or "default"
    )
    os.makedirs(collected_dir, exist_ok=True)
    progress_path = os.path.join(collected_dir, ".progress.json")

    # 断点续采：读取已有进度
    existing_rows = []
    if resume and os.path.exists(progress_path):
        try:
            with open(progress_path, "r", encoding="utf-8") as _pf:
                _prog = json.load(_pf)
            start_page = max(int(start_page or 1), int(_prog.get("current_page", 1)))
            existing_rows = _prog.get("rows", []) or []
            logger.info(f"[VisualCollector] 断点续采: 从第 {start_page} 页继续，已采 {len(existing_rows)} 行")
        except Exception as _pe:
            logger.warning(f"[VisualCollector] 读取断点进度失败: {_pe}")

    def save_progress(page_num, rows, status="running"):
        try:
            with open(progress_path, "w", encoding="utf-8") as _pf:
                json.dump({
                    "current_page": page_num,
                    "total_rows": len(rows),
                    "status": status,
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }, _pf, ensure_ascii=False)
        except Exception:
            pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)

        # ── 如果有已认证的 storage_state，直接用它创建 context，跳过登录 ──
        if storage_state:
            logger.info(f"[VisualCollector] 使用 storage_state 创建已认证 context "
                        f"(cookies: {len(storage_state.get('cookies', []))} 条)")
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
                storage_state=storage_state,
            )
        else:
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
            )
        page = context.new_page()

        try:
            # 登录（仅在没有 storage_state 时才执行）
            if task.login_required and not storage_state:
                login_url = host.rstrip("/") + "/login"
                page.goto(login_url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(1000)
                page.fill('input[placeholder*="账号"], input[name="username"]', user)
                page.fill('input[placeholder*="密码"], input[name="password"]', pwd)
                # 登录按钮（兼容 Element UI / 若依）
                btn_sel = 'button:has-text("登录"), button:has-text("登 录"), button[type="submit"], .login-btn, button.el-button:has-text("登录")'
                login_btn = page.locator(btn_sel)
                if login_btn.count() == 0:
                    login_btn = page.locator('button, .el-button')
                login_btn.first.click()
                page.wait_for_url(lambda u: "/login" not in u.lower(), timeout=30000, wait_until="networkidle")
                page.wait_for_timeout(2000)

            # ── 收集规则并按“来源页”分组（支持任意页面 / 详情页捕获）──
            rules_all = task.rules

            def _rule_attr(rule, name, default=None):
                return rule.get(name, default) if isinstance(rule, dict) else getattr(rule, name, default)

            detail_link_rules = [r for r in rules_all if _rule_attr(r, "is_detail_link", False)]
            detail_rules = [r for r in rules_all if _rule_attr(r, "scope", "page") == "detail"]
            page_groups = {}
            for r in rules_all:
                if _rule_attr(r, "is_detail_link", False):
                    continue
                if _rule_attr(r, "scope", "page") != "page":
                    continue
                su = (_rule_attr(r, "source_url", "") or "").strip()
                if not su:
                    su = target  # 旧规则无来源页时回退到任务目标页
                page_groups.setdefault(su, []).append(r)
            if not page_groups:
                page_groups[target] = [r for r in rules_all if not _rule_attr(r, "is_detail_link", False)]

            # 主列表页：含列表规则或详情链接规则的来源页（用于分页 / 断点续采）
            primary_su = None
            for su, grules in page_groups.items():
                if any(_rule_attr(r, "is_list", False) for r in grules) or any(_rule_attr(r, "is_detail_link", False) for r in grules):
                    primary_su = su
                    break

            all_columns = []
            errors = []
            page_results = {}
            total_pages = 0

            def _normalize_nav_url(url):
                if not url:
                    return ""
                u = str(url).strip()
                if u.startswith("/api/collector/proxy"):
                    u = u[len("/api/collector/proxy"):] or "/"
                if u.startswith("http://") or u.startswith("https://"):
                    try:
                        from urllib.parse import urlparse
                        p = urlparse(u)
                        u = p.path or "/"
                        if p.query:
                            u += "?" + p.query
                    except Exception:
                        pass
                if not u.startswith("/"):
                    u = "/" + u
                return u

            def _navigate(url, attempt_retry=retry_count):
                norm = _normalize_nav_url(url)
                full = norm if norm.startswith("http") else (host.rstrip("/") + norm)
                for _a in range(1, attempt_retry + 1):
                    try:
                        logger.info(f"[VisualCollector] 导航到 {full} (尝试 {_a}/{attempt_retry})")
                        page.goto(full, wait_until="networkidle", timeout=60000)
                        page.wait_for_timeout(1500)
                        return True
                    except Exception as _ne:
                        logger.warning(f"[VisualCollector] 导航失败(尝试 {_a}): {_ne}")
                        if _a < attempt_retry:
                            page.wait_for_timeout(2000 * _a)
                return False

            def _extract_rows(rules):
                if not rules:
                    return []
                vals_map = {}
                max_count = 0
                for rule in rules:
                    fn = _rule_attr(rule, "field_name", "")
                    css = _rule_attr(rule, "css_selector", "")
                    et = _rule_attr(rule, "extract_type", "text")
                    an = _rule_attr(rule, "attribute_name", "")
                    try:
                        els = page.locator(css)
                        count = els.count()
                        vals = []
                        limit = min(count, (max_rows if max_rows > 0 else 500))
                        for i in range(limit):
                            try:
                                v = _extract_value(els.nth(i), et, an)
                            except Exception:
                                v = ""
                            if an == "src" and v and str(v).lower().startswith("http"):
                                try:
                                    _local = _download_image(v, os.path.join(collected_dir, "images"), cookies=storage_cookies)
                                    if _local:
                                        v = _local
                                except Exception:
                                    pass
                            vals.append(v)
                        vals_map[fn] = vals
                        if len(vals) > max_count:
                            max_count = len(vals)
                    except Exception as e:
                        errors.append({"field": fn, "error": str(e)[:200]})
                        vals_map[fn] = []
                rows = []
                for i in range(max_count):
                    row = {}
                    for rule in rules:
                        fn = _rule_attr(rule, "field_name", "")
                        vv = vals_map.get(fn, [])
                        row[fn] = vv[i] if i < len(vv) else ""
                    rows.append(row)
                return rows

            resume_skip = max(1, int(start_page or 1)) if resume else 1

            # ── 逐来源页采集 ──
            for su, grules in page_groups.items():
                if not _navigate(su):
                    errors.append({"field": "*", "error": f"来源页导航失败: {su}"})
                    continue
                has_list = any(_rule_attr(r, "is_list", False) for r in grules) or any(_rule_attr(r, "is_detail_link", False) for r in grules)
                paginate = bool(task.pagination.get("enabled")) and has_list
                is_primary = (su == primary_su)
                skip = resume_skip if (is_primary and resume) else 1
                rows_page = []
                pnum = 1
                # 断点续采：跳过已采集的列表页
                if skip > 1 and paginate:
                    _sk = 1
                    while _sk < skip:
                        ns = task.pagination.get("next_button_selector", "")
                        if not ns:
                            break
                        try:
                            nb = page.locator(ns).first
                            if not nb.is_visible() or nb.is_disabled():
                                break
                            nb.click()
                            _sk += 1
                            page.wait_for_timeout(1500)
                        except Exception:
                            break
                    pnum = _sk
                while True:
                    rws = _extract_rows(grules)
                    rows_page.extend(rws)
                    total_pages += 1
                    if not paginate:
                        break
                    if task.pagination.get("max_pages", 0) > 0 and pnum >= task.pagination["max_pages"]:
                        break
                    ns = task.pagination.get("next_button_selector", "")
                    if ns:
                        try:
                            nb = page.locator(ns).first
                            if not nb.is_visible() or nb.is_disabled():
                                break
                            nb.click()
                            pnum += 1
                            page.wait_for_timeout(1500)
                        except Exception:
                            break
                    else:
                        break
                page_results[su] = rows_page

            # 断点续采：主列表页已采数据前置合并
            if resume and primary_su and primary_su in page_results:
                page_results[primary_su] = list(existing_rows) + page_results[primary_su]

            # ── 列表遍历详情页：逐条进入详情页抓取 scope=detail 字段 ──
            if detail_link_rules and detail_rules:
                link_rule = detail_link_rules[0]
                link_field = _rule_attr(link_rule, "field_name", "")
                link_src = (_rule_attr(link_rule, "source_url", "") or "").strip() or primary_su or target
                base_rows = page_results.get(link_src, [])
                for row in base_rows:
                    du = _normalize_nav_url(row.get(link_field, ""))
                    if not du or du == "/":
                        continue
                    if not _navigate(du):
                        errors.append({"field": link_field, "error": f"详情页导航失败: {du}"})
                        continue
                    dvals = _extract_rows(detail_rules)
                    if dvals:
                        row.update(dvals[0])
                    else:
                        for dr in detail_rules:
                            row[_rule_attr(dr, "field_name", "")] = ""

            # ── 汇总输出 ──
            multi = len(page_groups) > 1
            all_rows = []
            for su, rws in page_results.items():
                for row in rws:
                    if multi:
                        row = dict(row)
                        row["__source_page__"] = su
                    all_rows.append(row)
            seen_cols = []
            for r in rules_all:
                fn = _rule_attr(r, "field_name", "")
                if fn and fn not in seen_cols:
                    seen_cols.append(fn)
            if multi and "__source_page__" not in seen_cols:
                seen_cols.append("__source_page__")
            all_columns = seen_cols
            page_num = total_pages

            logger.info(f"[VisualCollector] 采集完成: {len(all_rows)} 行, {len(all_columns)} 列, {page_num} 页（来源页 {len(page_groups)} 个）")

            # 多格式落地
            saved_files = _save_collection_files(task, all_columns, all_rows, collected_dir, task.export_format)
            save_progress(page_num, all_rows, "done")

            return {
                "success": True,
                "data": {
                    "columns": all_columns,
                    "rows": all_rows,
                    "total": len(all_rows),
                    "pages": page_num,
                },
                "errors": errors,
                "saved_files": saved_files,
                "collected_dir": collected_dir,
            }

        except Exception as e:
            logger.error(f"[VisualCollector] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e), "data": {"columns": [], "rows": [], "total": 0}}
        finally:
            browser.close()


def build_selector_for_element(element: SelectableElement) -> str:
    """
    为元素构建最优 CSS 选择器（优先使用 ID > data-field > data-label > class）。
    返回最简洁且能唯一定位的 CSS 选择器。
    """
    if element.attributes.get("id"):
        return f"#{element.attributes['id']}"
    if element.attributes.get("data-field"):
        return f"[data-field='{element.attributes['data-field']}']"
    if element.attributes.get("data-label"):
        return f"[data-label='{element.attributes['data-label']}']"
    return element.css_selector


# ── 会话级缓存（避免每次创建新 Playwright 实例）──

_snapshot_cache: Dict[str, PageSnapshot] = {}


def cache_snapshot(key: str, snapshot: PageSnapshot):
    _snapshot_cache[key] = snapshot


def get_cached_snapshot(key: str) -> Optional[PageSnapshot]:
    return _snapshot_cache.get(key)


def clear_cache():
    _snapshot_cache.clear()
