# -*- coding: utf-8 -*-
"""
侧边栏路由自动发现

登录康养平台后，扫描侧边栏菜单，动态提取所有 Vue 路由路径和名称。
与 config_loader 配合使用，减少对 KNOWN_ROUTES 硬编码的依赖。

策略（按优先级）：
  1. 解析 .el-menu-item 的 index 属性（若依 router 模式，最可靠）
  2. 解析 .sidebar-container 内所有 <a> 标签的 href
  3. 兜底：逐个点击菜单项，观察 URL 变化

用法:
    from kangyang.route_discovery import discover_routes

    routes = discover_routes(
        host="http://192.168.18.143:1024",
        username="admin",
        password="admin123"
    )
    # → [{name: "入住处理", route: "/elderly/checkin", module: "老人管理"}, ...]
"""
import re
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class DiscoveryResult:
    routes: list = field(default_factory=list)
    source: str = ""          # "index_attr" | "href" | "click_scan" | "fallback"
    total_found: int = 0
    errors: list = field(default_factory=list)


def _ensure_playwright():
    """延迟导入 Playwright"""
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        raise ImportError("需要安装 playwright: pip install playwright && playwright install chromium")


# ── 若依侧边栏选择器 ──────────────────────────────

_SIDEBAR_SELECTORS = [
    ".sidebar-container",
    ".el-menu-vertical",
    "#app .el-menu",
    ".el-menu",
]

_MENU_ITEM_SELECTOR = ".el-menu-item"
_SUBMENU_TITLE_SELECTOR = ".el-submenu__title"
_SUBMENU_SELECTOR = ".el-submenu"
_SIDEBAR_LINK_SELECTOR = ".sidebar-container a, .el-menu-item a, .sidebar-container .el-menu-item"


# ── 核心发现逻辑 ────────────────────────────────

def discover_routes(host: str, username: str, password: str,
                    headless: bool = True, timeout: int = 30000) -> DiscoveryResult:
    """
    登录康养平台，自动发现侧边栏所有路由

    Args:
        host: 平台地址
        username: 用户名
        password: 密码
        headless: 是否无头模式
        timeout: 超时时间（毫秒）

    Returns:
        DiscoveryResult(routes=[{name, route, module}], ...)
    """
    sync_playwright = _ensure_playwright()
    result = DiscoveryResult()

    launch_kwargs = dict(
        headless=headless,
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
    )

    mgr = sync_playwright()
    try:
        pw = mgr.__enter__()
    except Exception as e:
        result.errors.append(f"Playwright 初始化失败: {e}")
        return result

    try:
        browser = pw.chromium.launch(**launch_kwargs)
    except Exception as e:
        mgr.__exit__(None, None, None)
        result.errors.append(f"Chromium 启动失败: {e}")
        return result

    page = browser.new_page(viewport={"width": 1920, "height": 1080})

    try:
        # Step 1: 登录
        login_url = f"{host.rstrip('/')}/login"
        page.goto(login_url, wait_until="domcontentloaded", timeout=timeout)
        page.wait_for_selector(
            'input[placeholder*="账号"], input[placeholder*="用户名"], .el-input__inner',
            timeout=15000
        )

        # 填写用户名
        username_input = page.locator(
            'input[placeholder*="账号"], input[placeholder*="用户名"], .login-form .el-input__inner'
        ).first
        username_input.click()
        username_input.fill("")
        username_input.fill(username)

        # 填写密码
        password_input = page.locator('input[type="password"], input[placeholder*="密码"]').first
        password_input.click()
        password_input.fill("")
        password_input.fill(password)

        # 点击登录
        login_btn = page.locator(
            'button:has-text("登录"), button:has-text("登 录"), .login-form button.el-button--primary'
        ).first
        login_btn.click()

        # 等待登录完成
        page.wait_for_selector(
            ".sidebar-container, .el-menu, .navbar",
            timeout=20000
        )
        time.sleep(2)
        logger.info("登录成功，开始扫描侧边栏菜单")

        # Step 2: 展开所有子菜单
        _expand_all_submenus(page)

        # Step 3: 策略1 - index 属性
        routes_via_index = _extract_routes_via_index(page)
        if routes_via_index:
            result.routes = routes_via_index
            result.source = "index_attr"
            result.total_found = len(routes_via_index)
            logger.info(f"策略1(index属性)成功: 发现 {result.total_found} 个路由")
            return result

        # Step 4: 策略2 - href
        routes_via_href = _extract_routes_via_href(page, host)
        if routes_via_href:
            result.routes = routes_via_href
            result.source = "href"
            result.total_found = len(routes_via_href)
            logger.info(f"策略2(href)成功: 发现 {result.total_found} 个路由")
            return result

        # Step 5: 策略3 - 点击扫描
        routes_via_click = _extract_routes_via_click(page, host)
        if routes_via_click:
            result.routes = routes_via_click
            result.source = "click_scan"
            result.total_found = len(routes_via_click)
            logger.info(f"策略3(点击)成功: 发现 {result.total_found} 个路由")
            return result

        # 所有策略失败
        result.source = "fallback"
        result.errors.append("所有自动发现策略均失败，请检查页面结构")
        logger.warning("侧边栏路由自动发现失败")

    except Exception as e:
        logger.error(f"路由自动发现异常: {e}")
        result.errors.append(str(e))
    finally:
        try:
            browser.close()
            mgr.__exit__(None, None, None)
        except Exception:
            pass

    return result


# ── 策略实现 ──────────────────────────────────

def _expand_all_submenus(page):
    """展开侧边栏中所有折叠的子菜单"""
    try:
        submenu_titles = page.locator(_SUBMENU_TITLE_SELECTOR)
        count = submenu_titles.count()
        for i in range(count):
            try:
                el = submenu_titles.nth(i)
                # 检查是否已展开（is-opened 类）
                parent = el.locator("..")
                is_opened = parent.evaluate(
                    "(el) => el.classList.contains('is-opened')"
                )
                if not is_opened:
                    el.click()
                    time.sleep(0.3)
            except Exception:
                pass
        logger.info(f"已展开子菜单 (共 {count} 个)")
    except Exception as e:
        logger.warning(f"展开子菜单失败: {e}")


def _extract_routes_via_index(page) -> list:
    """策略1: 从 .el-menu-item 的 index 属性提取路由"""
    try:
        menu_items = page.locator(_MENU_ITEM_SELECTOR)
        count = menu_items.count()
        if count == 0:
            return []

        routes = []
        seen_routes = set()

        for i in range(count):
            try:
                item = menu_items.nth(i)
                # 在 Vue/若依中，index 属性通常就是路由路径
                route_path = item.get_attribute("index") or ""
                if not route_path:
                    continue

                # 清理路径
                route_path = route_path.strip()
                if route_path in seen_routes:
                    continue
                if not route_path.startswith("/"):
                    route_path = "/" + route_path

                # 提取菜单项文本
                text = item.inner_text().strip()
                # 若依菜单文字："图标\n入住处理" → 取最后一行非空文字
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                name = lines[-1] if lines else route_path
                # 排除纯图标文字（通常是单字或空）
                if len(name) <= 1:
                    continue

                # 尝试确定模块名（从父级 submenu 标题获取）
                module = _get_parent_module(item)

                routes.append({
                    "name": name,
                    "route": route_path,
                    "module": module,
                })
                seen_routes.add(route_path)

            except Exception as e:
                logger.debug(f"跳过菜单项[{i}]: {e}")

        return routes

    except Exception as e:
        logger.warning(f"策略1(index)失败: {e}")
        return []


def _extract_routes_via_href(page, host: str) -> list:
    """策略2: 从侧边栏 <a> 标签的 href 属性提取路由"""
    try:
        base = host.rstrip("/")
        links = page.locator(_SIDEBAR_LINK_SELECTOR)
        count = links.count()
        if count == 0:
            return []

        routes = []
        seen_routes = set()

        for i in range(count):
            try:
                el = links.nth(i)
                href = (el.get_attribute("href") or "").strip()
                if not href or href == "#" or href.startswith("javascript:"):
                    continue

                # 将绝对 URL 转为相对路由
                if href.startswith(base):
                    route_path = href[len(base):]
                elif href.startswith("http"):
                    continue  # 外部链接跳过
                else:
                    route_path = href

                # 清理查询参数
                route_path = route_path.split("?")[0].split("#")[0].rstrip("/")
                if not route_path or route_path == "/":
                    continue
                if route_path in seen_routes:
                    continue

                # 获取可见文本作为名称
                text = el.inner_text().strip()
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                name = lines[-1] if lines else route_path

                if len(name) <= 1:
                    continue

                module = _get_parent_module(el)

                routes.append({
                    "name": name,
                    "route": route_path,
                    "module": module,
                })
                seen_routes.add(route_path)

            except Exception:
                continue

        return routes

    except Exception as e:
        logger.warning(f"策略2(href)失败: {e}")
        return []


def _extract_routes_via_click(page, host: str) -> list:
    """策略3(兜底): 逐个点击菜单项，记录 URL 变化"""
    try:
        base = host.rstrip("/")
        menu_items = page.locator(_MENU_ITEM_SELECTOR)
        count = menu_items.count()
        if count == 0:
            return []

        routes = []
        seen_routes = set()
        max_items = min(count, 50)  # 安全上限

        for i in range(max_items):
            try:
                # 重新获取元素（DOM 可能已变化）
                items = page.locator(_MENU_ITEM_SELECTOR)
                if i >= items.count():
                    break

                item = items.nth(i)
                text = item.inner_text().strip()
                name = text.split("\n")[-1].strip() if "\n" in text else text

                if len(name) <= 1:
                    continue

                # 记录当前 URL
                current_url = page.url

                # 点击菜单项
                item.click()
                time.sleep(0.5)

                # 获取新 URL
                new_url = page.url

                if new_url != current_url:
                    route_path = new_url
                    if route_path.startswith(base):
                        route_path = route_path[len(base):]
                    route_path = route_path.split("?")[0].split("#")[0].rstrip("/")

                    if route_path and route_path != "/" and route_path not in seen_routes:
                        module = _get_parent_module(item)
                        routes.append({
                            "name": name,
                            "route": route_path,
                            "module": module,
                        })
                        seen_routes.add(route_path)

            except Exception:
                continue

        return routes

    except Exception as e:
        logger.warning(f"策略3(点击扫描)失败: {e}")
        return []


def _get_parent_module(element) -> str:
    """向上查找父级 submenu 标题作为模块名"""
    try:
        parent = element.locator("xpath=ancestor::li[contains(@class, 'el-submenu')]")
        if parent.count() > 0:
            title_el = parent.first.locator(_SUBMENU_TITLE_SELECTOR)
            if title_el.count() > 0:
                title_text = title_el.first.inner_text().strip()
                # 取第一行有意义文本（去掉图标字符）
                lines = [l.strip() for l in title_text.split("\n") if l.strip() and len(l.strip()) > 1]
                if lines:
                    return lines[0]
    except Exception:
        pass
    return ""


# ── 便捷函数 ──────────────────────────────────

def discover_and_merge(host: str, username: str, password: str,
                       strategy: str = "append", headless: bool = True) -> dict:
    """
    一键发现 + 合并：自动发现路由并合并到全局配置

    Args:
        host, username, password: 平台凭据
        strategy: "append" 追加 | "replace" 替换
        headless: 无头模式

    Returns:
        {"added": int, "total": int, "source": str, "routes": [...]}
    """
    from kangyang.config import merge_discovered_routes, get_routes, get_config_status

    discovery = discover_routes(host, username, password, headless=headless)

    if not discovery.routes:
        return {
            "added": 0,
            "total": len(get_routes()),
            "source": get_config_status()["routes_source"],
            "errors": discovery.errors,
            "routes": get_routes(),
        }

    added, total = merge_discovered_routes(discovery.routes, strategy=strategy)

    return {
        "added": added,
        "total": total,
        "source": get_config_status()["routes_source"],
        "discovery_source": discovery.source,
        "discovery_errors": discovery.errors,
        "routes": get_routes(),
        "discovered_routes": discovery.routes,
    }
