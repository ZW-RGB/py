# -*- coding: utf-8 -*-
import os as _os_builtin
_os_builtin.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')
"""
Playwright 驱动的页面完整爬取模块
───────────────────────────────────
功能：
  - 无头浏览器启动 & 生命周期管理
  - RuoYi 平台自动登录（表单填写 + 提交）
  - 页面导航 & Vue SPA 动态内容等待
  - 结构化内容提取：
      · 文本（段落/标题/列表）
      · 图片（src/alt/尺寸）
      · 链接（href/文本/类型）
      · 表格（表头/数据行）
      · 表单（字段/标签/值）
      · 页面结构（标题层级、段落、列表）
  - 可选页面截图
  - 与现有 kangyang 模块架构保持一致：dataclass + dict 序列化
"""
import json
import time
import base64
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

# Playwright 延迟导入 —— 避免缺少依赖时阻塞其他模块
_playwright_available = False


def _ensure_playwright():
    """检查并导入 Playwright"""
    global _playwright_available
    if _playwright_available:
        return
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # noqa: F401
        _playwright_available = True
    except ImportError:
        raise ImportError(
            "Playwright 未安装。请运行: pip install playwright && python -m playwright install chromium"
        )


# ═══════════════ 数据结构 ═══════════════

@dataclass
class ImageInfo:
    src: str
    alt: str = ""
    width: str = ""
    height: str = ""
    is_svg: bool = False


@dataclass
class LinkInfo:
    href: str
    text: str = ""
    is_internal: bool = True
    link_type: str = "page"  # page / api / file / external


@dataclass
class TableInfo:
    caption: str = ""
    headers: List[str] = field(default_factory=list)
    rows: List[List[str]] = field(default_factory=list)
    row_count: int = 0
    col_count: int = 0


@dataclass
class FormInfo:
    form_id: str = ""
    form_action: str = ""
    fields: List[Dict] = field(default_factory=list)  # [{label, name, type, value, placeholder}]


@dataclass
class PageSection:
    """页面结构中的一块内容"""
    section_type: str = ""   # heading / paragraph / list / blockquote / code
    level: int = 0           # heading 层级
    text: str = ""
    children: List[str] = field(default_factory=list)


@dataclass
class PageContent:
    """完整的页面采集结果"""
    url: str = ""
    title: str = ""
    timestamp: str = ""
    # ── 页面结构 ──
    sections: List[dict] = field(default_factory=list)
    # ── 分类提取 ──
    images: List[dict] = field(default_factory=list)
    links: List[dict] = field(default_factory=list)
    tables: List[dict] = field(default_factory=list)
    forms: List[dict] = field(default_factory=list)
    # ── 纯文本 ──
    raw_text: str = ""
    # ── 元数据 ──
    metadata: dict = field(default_factory=dict)
    # ── 截图（base64）──
    screenshot_base64: str = ""
    # ── 错误信息 ──
    error: str = ""

    def to_dict(self, include_screenshot: bool = True) -> dict:
        """序列化为字典（可用于 JSON 导出）"""
        d = asdict(self)
        if not include_screenshot:
            d.pop("screenshot_base64", None)
        return d


# ═══════════════ 前端路由：从 YAML 配置加载 + 支持自动发现 ═══════════════
# 热更新: 调用 kangyang.config.reload_config() / 合并: kangyang.config.merge_discovered_routes()

from kangyang.config import get_routes

KNOWN_ROUTES = get_routes()


# ═══════════════ 页面爬取器 ═══════════════

class PageScraper:
    """
    Playwright 驱动的页面完整爬取器

    用法:
        scraper = PageScraper()
        scraper.login(host="http://192.168.18.143:1024", username="admin", password="admin123")

        # 方式1：按 Vue 路由爬取
        content = scraper.scrape_by_route("/elderly/checkin")

        # 方式2：按完整 URL 爬取
        content = scraper.scrape_page("http://192.168.18.143:1024/elderly/checkin")

        # 方式3：批量爬取（根据路由列表）
        results = scraper.scrape_all(host="...", routes=["/elderly/...", "/health/..."])

        scraper.close()
    """

    def __init__(self, headless: bool = True, timeout: int = 30000, viewport: dict = None):
        """
        Args:
            headless: 是否无头模式（默认 True）
            timeout: 默认操作超时（毫秒）
            viewport: 视口大小，默认 {"width": 1920, "height": 1080}
        """
        _ensure_playwright()
        self.headless = headless
        self.timeout = timeout
        self.viewport = viewport or {"width": 1920, "height": 1080}
        self._playwright_pw = None    # Playwright 实例
        self._playwright_mgr = None   # PlaywrightContextManager 实例（生命周期管理）
        self._browser = None
        self._page = None
        self._host = ""
        self._logged_in = False

    # ── 生命周期 ─────────────────────────────

    def _ensure_browser(self):
        """延迟启动浏览器（使用上下文管理器确保 Playwright 生命周期正确）"""
        if self._browser is not None:
            return

        from playwright.sync_api import sync_playwright

        launch_kwargs = dict(
            headless=self.headless,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )

        # 使用 __enter__/__exit__ 协议，确保 Playwright 生命周期正确
        self._playwright_mgr = sync_playwright()
        try:
            self._playwright_pw = self._playwright_mgr.__enter__()
        except Exception as e:
            self._playwright_mgr = None
            raise RuntimeError(
                f"Playwright 初始化失败: {e}\n"
                "请运行: playwright install chromium"
            ) from e

        try:
            self._browser = self._playwright_pw.chromium.launch(**launch_kwargs)
        except Exception as e:
            self._playwright_mgr.__exit__(None, None, None)
            self._playwright_pw = None
            self._playwright_mgr = None
            raise RuntimeError(
                f"Chromium 浏览器启动失败: {e}\n"
                "请确认 Playwright 浏览器已安装: playwright install chromium"
            ) from e

        self._page = self._browser.new_page(viewport=self.viewport)
        logger.info("浏览器已启动 (headless=%s)", self.headless)

    def close(self):
        """关闭浏览器 & 释放资源"""
        if self._page:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright_pw and self._playwright_mgr:
            try:
                self._playwright_mgr.__exit__(None, None, None)
            except Exception:
                pass
            self._playwright_pw = None
            self._playwright_mgr = None
        elif self._playwright_pw:
            # fallback: 尝试 stop() 方式
            try:
                self._playwright_pw.stop()
            except Exception:
                pass
            self._playwright_pw = None
            self._playwright_mgr = None
        self._logged_in = False
        logger.info("浏览器已关闭")

    # ── 登录 ─────────────────────────────────

    def login(self, host: str, username: str, password: str) -> bool:
        """
        通过浏览器自动登录 RuoYi 平台

        Args:
            host: 平台地址，如 http://192.168.18.143:1024
            username: 用户名
            password: 密码

        Returns:
            是否登录成功
        """
        from playwright.sync_api import TimeoutError as PWTimeout

        self._host = host.rstrip("/")
        self._ensure_browser()

        try:
            # 1. 导航到登录页
            login_url = f"{self._host}/login"
            logger.info("正在访问登录页: %s", login_url)
            self._page.goto(login_url, wait_until="domcontentloaded", timeout=self.timeout)

            # 2. 等待登录表单出现
            #    若依的登录表单通常包含: el-input (用户名/密码)、el-button (登录按钮)
            self._page.wait_for_selector('input[placeholder*="账号"], input[placeholder*="用户名"], .el-input__inner', timeout=30000)

            # 3. 填写用户名
            username_input = self._page.locator('input[placeholder*="账号"], input[placeholder*="用户名"]').first
            if username_input.count() == 0:
                # 若依可能会用 .el-input__inner 类名
                username_input = self._page.locator('.login-form .el-input__inner, form .el-input__inner').first
            username_input.click()
            username_input.fill("")  # 清空
            username_input.fill(username)
            logger.info("已填写用户名")

            # 4. 填写密码
            password_inputs = self._page.locator('input[type="password"], input[placeholder*="密码"]')
            if password_inputs.count() > 0:
                password_input = password_inputs.first
                password_input.click()
                password_input.fill("")
                password_input.fill(password)
                logger.info("已填写密码")

            # 5. 若有验证码输入框，直接留空（若依默认不开启验证码）
            #    若依验证码: 输入 code="" 即可（服务端不校验）

            # 6. 点击登录按钮
            login_btn = self._page.locator('button:has-text("登录"), button:has-text("登 录"), .login-btn')
            if login_btn.count() > 0:
                login_btn.first.click()
                logger.info("已点击登录按钮")

            # 7. 等待页面跳转（登录成功后会重定向到首页）
            time.sleep(2)
            try:
                # 等待侧边栏或首页特征元素出现
                self._page.wait_for_selector(
                    '.sidebar-container, .navbar, .app-main, .el-menu, .main-container',
                    timeout=30000
                )
                self._logged_in = True
                logger.info("✓ 登录成功")
                return True
            except PWTimeout:
                # 检查是否有错误提示
                error_msg = self._page.locator('.el-message--error, .el-message__content').first
                if error_msg.count() > 0:
                    msg_text = error_msg.inner_text()
                    logger.error("登录失败: %s", msg_text)
                else:
                    logger.error("登录超时: 未检测到登录后页面特征")
                return False

        except PWTimeout as e:
            logger.error("登录过程超时: %s", e)
            return False
        except Exception as e:
            logger.error("登录异常: %s", e)
            return False

    # ── 页面导航 ─────────────────────────────

    def scrape_by_route(self, route_path: str) -> PageContent:
        """
        根据 Vue 路由路径爬取页面

        Args:
            route_path: 前端路由，如 '/elderly/checkin'

        Returns:
            PageContent
        """
        if not self._logged_in:
            return PageContent(error="未登录，请先调用 login()")

        # 若依框架通常使用 # 号路由，如 http://host/#/elderly/checkin
        # 也可能是 history 模式，如 http://host/elderly/checkin
        # 两种都尝试
        full_url = f"{self._host}{route_path}"
        return self.scrape_page(full_url)

    def scrape_page(self, url: str) -> PageContent:
        """
        爬取指定 URL 的完整页面内容

        Args:
            url: 完整页面 URL

        Returns:
            PageContent
        """
        from playwright.sync_api import TimeoutError as PWTimeout

        if not self._page:
            self._ensure_browser()

        if not self._logged_in:
            return PageContent(error="未登录，请先调用 login()")

        start_time = time.time()

        try:
            # 1. 导航
            logger.info("正在导航到: %s", url)
            self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)

            # 2. 等待动态内容渲染
            self._wait_for_content()

            # 3. 滚动页面触发懒加载
            self._scroll_to_load()

            # 4. 等待最终稳定
            time.sleep(1)

            # 5. 提取
            content = PageContent()
            content.url = self._page.url
            content.title = self._page.title()
            content.timestamp = datetime.now().isoformat()
            content.metadata["load_time_ms"] = int((time.time() - start_time) * 1000)

            content.sections = self._extract_structure()
            content.images = self._extract_images()
            content.links = self._extract_links()
            content.tables = self._extract_tables()
            content.forms = self._extract_forms()
            content.raw_text = self._extract_all_text()

            # 元数据
            content.metadata["total_images"] = len(content.images)
            content.metadata["total_links"] = len(content.links)
            content.metadata["total_tables"] = len(content.tables)
            content.metadata["text_length"] = len(content.raw_text)

            logger.info("✓ 页面提取完成: %d 图片, %d 链接, %d 表格, %d 字符",
                        content.metadata["total_images"],
                        content.metadata["total_links"],
                        content.metadata["total_tables"],
                        content.metadata["text_length"])

            return content

        except PWTimeout as e:
            logger.error("页面加载超时: %s", e)
            return self._partial_extract(url, f"页面加载超时: {e}")
        except Exception as e:
            logger.error("页面爬取异常: %s", e)
            return self._partial_extract(url, f"爬取异常: {e}")

    def scrape_all(self, host: str, routes: List[str] = None) -> List[PageContent]:
        """
        批量爬取多个页面

        Args:
            host: 平台地址
            routes: Vue 路由列表，为空时使用默认 KNOWN_ROUTES

        Returns:
            [PageContent, ...]
        """
        if self._logged_in is False or self._host != host.rstrip("/"):
            if not self.login(host, "", ""):
                return []

        if routes is None:
            routes = [r["route"] for r in get_routes()]

        results = []
        for i, route in enumerate(routes):
            logger.info("批量爬取 [%d/%d]: %s", i + 1, len(routes), route)
            content = self.scrape_by_route(route)
            results.append(content)

        return results

    def screenshot(self) -> str:
        """截取当前页面截图，返回 base64"""
        if not self._page:
            return ""
        screenshot_bytes = self._page.screenshot(full_page=False, type="png")
        return base64.b64encode(screenshot_bytes).decode("utf-8")

    def screenshot_full(self) -> str:
        """截取整页截图（含滚动区域），返回 base64"""
        if not self._page:
            return ""
        screenshot_bytes = self._page.screenshot(full_page=True, type="png")
        return base64.b64encode(screenshot_bytes).decode("utf-8")

    # ── 内部方法: 等待 & 滚动 ──────────────────

    def _wait_for_content(self):
        """等待 Vue SPA 内容渲染完成"""
        try:
            # 策略1: 等待网络空闲
            self._page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            logger.debug("domcontentloaded 超时，继续尝试...")

        # 策略2: 等待常见 Element UI / 若依 组件出现
        selectors = [
            ".el-table",         # Element UI 表格
            ".el-form",          # Element UI 表单
            ".app-main",         # 若依主区域
            ".main-container",   # 若依主容器
            "table",             # 普通表格
            "form",              # 普通表单
            ".content",          # 通用内容区
        ]
        waited = False
        for sel in selectors:
            try:
                self._page.wait_for_selector(sel, timeout=3000)
                waited = True
                logger.debug("检测到内容元素: %s", sel)
                break
            except Exception:
                continue

        if not waited:
            logger.debug("未检测到明确内容元素，继续提取")

        # 策略3: 等待 loading 消失
        try:
            self._page.wait_for_selector(".el-loading-mask", state="hidden", timeout=5000)
            logger.debug("loading 遮罩已消失")
        except Exception:
            pass

        # 额外等待
        time.sleep(0.5)

    def _scroll_to_load(self):
        """滚动页面触发懒加载内容"""
        try:
            # 获取页面总高度
            prev_height = 0
            for attempt in range(5):
                height = self._page.evaluate("() => document.body.scrollHeight")
                if height == prev_height:
                    break
                prev_height = height

                # 分段滚动
                for y in range(0, height, 400):
                    self._page.evaluate(f"() => window.scrollTo(0, {y})")
                    time.sleep(0.1)

                # 滚回顶部
                self._page.evaluate("() => window.scrollTo(0, 0)")
                time.sleep(0.5)
        except Exception as e:
            logger.debug("滚动加载跳过: %s", e)

    # ── 分页爬取 ──────────────────────────────

    def _detect_pagination(self) -> dict:
        """
        检测 Element UI 分页组件，返回分页信息

        Returns:
            {
                "found": bool,
                "total_items": int,     # 总记录数
                "page_size": int,       # 当前每页条数
                "current_page": int,    # 当前页码
                "total_pages": int,     # 总页数
            }
        """
        if not self._page:
            return {"found": False}
        try:
            info = self._page.evaluate("""() => {
                const pagination = document.querySelector('.el-pagination');
                if (!pagination) return {found: false};
                const result = {found: true};

                // 总记录数
                const totalEl = pagination.querySelector('.el-pagination__total');
                if (totalEl) {
                    const match = (totalEl.textContent || '').match(/(\\d+)/);
                    result.total_items = match ? parseInt(match[1]) : 0;
                } else {
                    result.total_items = 0;
                }

                // 每页条数
                const sizeInput = pagination.querySelector(
                    '.el-pagination__sizes .el-select .el-input__inner, ' +
                    '.el-pagination__sizes input'
                );
                if (sizeInput) {
                    const val = sizeInput.value || sizeInput.textContent || '';
                    result.page_size = parseInt(val) || 10;
                } else {
                    result.page_size = 10;
                }

                // 当前页码
                const activePage = pagination.querySelector('.el-pager li.active, .number.active');
                if (activePage) {
                    result.current_page = parseInt(activePage.textContent) || 1;
                } else {
                    result.current_page = 1;
                }

                // 总页数
                if (result.total_items > 0 && result.page_size > 0) {
                    result.total_pages = Math.ceil(result.total_items / result.page_size);
                } else {
                    // 从分页按钮推算
                    const pageItems = pagination.querySelectorAll('.el-pager li.number');
                    let maxPage = 0;
                    pageItems.forEach(li => {
                        const n = parseInt(li.textContent);
                        if (!isNaN(n) && n > maxPage) maxPage = n;
                    });
                    result.total_pages = maxPage || 1;
                }

                // 检测是否有分页
                if (result.total_pages <= 1 && result.total_items <= result.page_size) {
                    result.found = false;
                }

                return result;
            }""")
            if info and info.get("found"):
                logger.info("检测到分页: 共 %s 条, 每页 %s 条, 共 %s 页 (当前第 %s 页)",
                            info.get("total_items"), info.get("page_size"),
                            info.get("total_pages"), info.get("current_page"))
            else:
                logger.debug("未检测到 Element UI 分页组件")
            return info or {"found": False}
        except Exception as e:
            logger.debug("分页检测失败: %s", e)
            return {"found": False}

    def _change_page_size(self, page_size: int) -> bool:
        """
        修改每页显示条数（10/20/30/50）

        Args:
            page_size: 目标条数

        Returns:
            是否成功
        """
        if not self._page:
            return False
        try:
            current = self._detect_pagination()
            if current.get("page_size") == page_size:
                logger.debug("每页条数已是 %s，无需切换", page_size)
                return True

            # 1. 点击每页条数下拉
            size_trigger = self._page.locator(
                '.el-pagination__sizes .el-select, .el-pagination__sizes .el-input'
            ).first
            if size_trigger.count() == 0:
                logger.debug("未找到分页尺寸下拉")
                return False

            size_trigger.click()
            time.sleep(0.5)

            # 2. 等待下拉菜单出现
            try:
                self._page.wait_for_selector(
                    '.el-select-dropdown:not(.is-hidden), .el-popper',
                    timeout=3000
                )
            except Exception:
                pass
            time.sleep(0.3)

            # 3. 点击目标条数选项
            target_text = str(page_size)
            option_found = False

            all_options = self._page.locator('.el-select-dropdown__item')
            for i in range(all_options.count()):
                opt = all_options.nth(i)
                text = (opt.text_content() or "").strip()
                if text == target_text or text.startswith(target_text):
                    opt.click()
                    option_found = True
                    break

            if option_found:
                logger.info("已切换每页条数为 %s", page_size)
                time.sleep(2)  # 等待表格重新加载
                self._wait_for_content()
                return True
            else:
                logger.debug("未找到分页选项 %s", page_size)
                self._page.keyboard.press("Escape")
                return False

        except Exception as e:
            logger.debug("切换每页条数失败: %s", e)
            return False

    def _navigate_to_page(self, page_num: int, max_retries: int = 3) -> bool:
        """
        翻到指定页码

        Args:
            page_num: 目标页码（从 1 开始）

        Returns:
            是否成功
        """
        if not self._page:
            return False

        for attempt in range(max_retries):
            try:
                # 检查是否已经在目标页
                current = self._detect_pagination()
                if current.get("current_page") == page_num:
                    return True

                # 方式1: 点击页码按钮
                page_btn = self._page.locator(
                    f'.el-pager li.number:has-text("{page_num}"):not(.active)'
                )
                if page_btn.count() > 0:
                    page_btn.first.click()
                    logger.debug("点击页码 %s 按钮", page_num)
                    time.sleep(1.5)
                    self._wait_for_table_update()
                    return True

                # 方式2: 跳转输入框（若依分页常常有"前往"输入框）
                jumper = self._page.locator('.el-pagination__jump input, .el-pagination__editor input')
                if jumper.count() > 0:
                    jumper.first.click()
                    jumper.first.fill("")
                    jumper.first.fill(str(page_num))
                    jumper.first.press("Enter")
                    logger.debug("输入跳转到第 %s 页", page_num)
                    time.sleep(2)
                    self._wait_for_table_update()
                    return True

                # 方式3: 逐页点击"下一页"（当页码按钮不可见时）
                current_pg = current.get("current_page", 1)
                if current_pg < page_num:
                    steps_needed = page_num - current_pg
                    for step in range(min(steps_needed, 20)):
                        next_btn = self._page.locator('.btn-next:not(.disabled)')
                        if next_btn.count() == 0:
                            break
                        next_btn.first.click()
                        time.sleep(1.2)
                        self._wait_for_table_update()
                    new_current = self._detect_pagination()
                    if new_current.get("current_page") == page_num:
                        return True

                logger.debug("翻页尝试 %s/%s 失败", attempt + 1, max_retries)
                time.sleep(1)

            except Exception as e:
                logger.debug("翻到第 %s 页异常: %s", page_num, e)
                time.sleep(1)

        logger.warning("无法翻到第 %s 页（共 %s 次尝试）", page_num, max_retries)
        return False

    def _wait_for_table_update(self):
        """等待表格数据刷新（翻页/切换每页条数后）"""
        try:
            self._page.wait_for_selector(".el-loading-mask", state="visible", timeout=3000)
            logger.debug("检测到 loading 遮罩，等待数据加载...")
        except Exception:
            pass
        try:
            self._page.wait_for_selector(".el-loading-mask", state="hidden", timeout=10000)
            logger.debug("loading 遮罩已消失")
        except Exception:
            pass
        time.sleep(0.5)
        try:
            self._page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

    def scrape_with_pagination(self, route_or_url: str, page_start: int = 1,
                                page_end: int = None, page_size: int = None) -> dict:
        """
        分页爬取 —— 遍历多页，合并所有页面的内容

        Args:
            route_or_url: 页面路由或完整 URL
            page_start: 起始页码（从 1 开始）
            page_end: 结束页码（None = 自动检测总页数，爬取全部页）
            page_size: 每页条数（None = 保持默认，可选 10/20/30/50）

        Returns:
            {
                "pages": [...],              # 每页的 PageContent 序列化结果
                "merged_tables": [...],      # 合并且去重的表格数据
                "merged_links": [...],       # 合并且去重的链接
                "merged_images": [...],      # 合并且去重的图片
                "merged_text": str,          # 合并文本
                "merged_forms": [...],       # 合并表单
                "merged_structure": [...],   # 合并页面结构
                "pagination_info": {...},    # 分页信息
                "total_pages_scraped": int,  # 实际爬取的页数
                "error": str,
            }
        """
        from playwright.sync_api import TimeoutError as PWTimeout

        if not self._page:
            self._ensure_browser()

        if not self._logged_in:
            return {"error": "未登录，请先调用 login()", "total_pages_scraped": 0}

        # ── 1. 导航到页面 ──
        if route_or_url.startswith("http://") or route_or_url.startswith("https://"):
            full_url = route_or_url
        else:
            full_url = f"{self._host}{route_or_url}"

        try:
            logger.info("分页爬取 - 导航到: %s", full_url)
            self._page.goto(full_url, wait_until="domcontentloaded", timeout=self.timeout)
            self._wait_for_content()
        except PWTimeout:
            return {"error": "页面加载超时", "total_pages_scraped": 0}

        # ── 2. 检测分页 ──
        pagination = self._detect_pagination()
        if not pagination.get("found"):
            # 没有分页组件，当作单页处理
            logger.info("未检测到分页组件，按单页采集")
            single = self._extract_page_content(full_url)
            return {
                "pages": [single],
                "merged_tables": single.get("tables", []),
                "merged_links": single.get("links", []),
                "merged_images": single.get("images", []),
                "merged_text": single.get("raw_text", ""),
                "merged_forms": single.get("forms", []),
                "merged_structure": single.get("sections", []),
                "pagination_info": pagination,
                "total_pages_scraped": 1,
            }

        # ── 3. 设置每页条数 ──
        if page_size and page_size != pagination.get("page_size"):
            self._change_page_size(page_size)
            # 重新检测（总页数可能变了）
            time.sleep(1)
            pagination = self._detect_pagination()

        total_pages = pagination.get("total_pages", 1)
        if page_end is None or page_end > total_pages:
            page_end = total_pages
        page_end = max(page_start, min(page_end, total_pages))

        logger.info("分页爬取: 第 %s-%s 页 / 共 %s 页 (每页 %s 条)",
                     page_start, page_end, total_pages, pagination.get("page_size"))

        # ── 4. 逐页爬取 ──
        all_pages = []
        all_tables = []
        all_links = []
        all_images = []
        all_texts = []
        all_forms = []
        all_structure = []
        seen_link_urls = set()
        seen_image_urls = set()

        for pg in range(page_start, page_end + 1):
            logger.info("正在爬取第 %s/%s 页...", pg, page_end)

            if pg > page_start:
                ok = self._navigate_to_page(pg)
                if not ok:
                    logger.warning("翻到第 %s 页失败，停止分页爬取", pg)
                    break

            # 刷新后重新等待内容
            self._scroll_to_load()
            time.sleep(0.5)

            # 提取当前页内容
            page_content = self._extract_page_content(full_url)
            page_content["page_number"] = pg
            all_pages.append(page_content)

            # 合并表格
            for t in page_content.get("tables", []):
                if t not in all_tables:
                    all_tables.append(t)

            # 合并链接（去重）
            for link in page_content.get("links", []):
                url_key = link.get("href", "")
                if url_key and url_key not in seen_link_urls:
                    seen_link_urls.add(url_key)
                    all_links.append(link)

            # 合并图片（去重）
            for img in page_content.get("images", []):
                src_key = img.get("src", "")
                if src_key and src_key not in seen_image_urls:
                    seen_image_urls.add(src_key)
                    all_images.append(img)

            # 合并文本（追加分页标记）
            all_texts.append(f"--- 第 {pg} 页 ---\n{page_content.get('raw_text', '')}")

            # 合并表单
            for f in page_content.get("forms", []):
                if f not in all_forms:
                    all_forms.append(f)

            # 合并页面结构
            for s in page_content.get("sections", []):
                if s not in all_structure:
                    all_structure.append(s)

            logger.info("第 %s 页完成: %d 表格, %d 链接", pg,
                        len(page_content.get("tables", [])),
                        len(page_content.get("links", [])))

        logger.info("分页爬取完成: 共 %d 页, %d 表格, %d 链接, %d 图片",
                     len(all_pages), len(all_tables), len(all_links), len(all_images))

        return {
            "pages": all_pages,
            "merged_tables": all_tables,
            "merged_links": all_links,
            "merged_images": all_images,
            "merged_text": "\n".join(all_texts),
            "merged_forms": all_forms,
            "merged_structure": all_structure,
            "pagination_info": pagination,
            "total_pages_scraped": len(all_pages),
        }

    def _extract_page_content(self, url: str) -> dict:
        """提取当前页面内容（不导航，纯提取）—— 返回 dict 格式"""
        from dataclasses import asdict
        content = PageContent()
        content.url = url
        content.timestamp = datetime.now().isoformat()
        try:
            if self._page:
                content.title = self._page.title()
                content.sections = self._extract_structure()
                content.images = self._extract_images()
                content.links = self._extract_links()
                content.tables = self._extract_tables()
                content.forms = self._extract_forms()
                content.raw_text = self._extract_all_text()
        except Exception as e:
            content.error = str(e)
        return asdict(content)

    def _partial_extract(self, url: str, error_msg: str) -> PageContent:
        """出错时尝试提取已加载的内容"""
        content = PageContent()
        content.url = url
        content.error = error_msg
        content.timestamp = datetime.now().isoformat()
        try:
            if self._page:
                content.title = self._page.title()
                content.raw_text = self._extract_all_text()
                content.sections = self._extract_structure()
                content.tables = self._extract_tables()
        except Exception:
            pass
        return content

    # ── 内部方法: 内容提取 ─────────────────────

    def _extract_all_text(self) -> str:
        """提取页面上所有可见文本"""
        if not self._page:
            return ""
        try:
            text = self._page.evaluate("""() => {
                const el = document.querySelector('.app-main, .main-container, main, body');
                if (!el) return document.body ? document.body.innerText : '';
                return el.innerText || '';
            }""")
            return text.strip() if text else ""
        except Exception:
            return ""

    def _extract_structure(self) -> List[dict]:
        """提取页面结构：标题层级、段落、列表"""
        if not self._page:
            return []

        try:
            sections = self._page.evaluate("""() => {
                const container = document.querySelector('.app-main, .main-container, main, body');
                if (!container) return [];

                const result = [];
                const walker = document.createTreeWalker(
                    container,
                    NodeFilter.SHOW_ELEMENT,
                    {
                        acceptNode: function(node) {
                            const tag = node.tagName;
                            if (/^H[1-6]$/.test(tag) || tag === 'P' || tag === 'UL' || tag === 'OL' || tag === 'BLOCKQUOTE' || tag === 'PRE') {
                                return NodeFilter.FILTER_ACCEPT;
                            }
                            // 跳过 script/style/nav/footer/header 内的元素
                            if (node.closest('script, style, nav, footer, header')) {
                                return NodeFilter.FILTER_REJECT;
                            }
                            return NodeFilter.FILTER_SKIP;
                        }
                    }
                );

                let node;
                while (node = walker.nextNode()) {
                    const tag = node.tagName;
                    const section = {
                        section_type: '',
                        level: 0,
                        text: '',
                        children: []
                    };

                    if (/^H([1-6])$/.test(tag)) {
                        section.section_type = 'heading';
                        section.level = parseInt(RegExp.$1);
                        section.text = (node.textContent || '').trim();
                    } else if (tag === 'P') {
                        const text = (node.textContent || '').trim();
                        if (text.length > 0) {
                            section.section_type = 'paragraph';
                            section.text = text;
                        }
                    } else if (tag === 'UL' || tag === 'OL') {
                        section.section_type = 'list';
                        const items = node.querySelectorAll('li');
                        items.forEach(li => {
                            const t = (li.textContent || '').trim();
                            if (t) section.children.push(t);
                        });
                    } else if (tag === 'BLOCKQUOTE') {
                        section.section_type = 'blockquote';
                        section.text = (node.textContent || '').trim();
                    } else if (tag === 'PRE') {
                        section.section_type = 'code';
                        section.text = (node.textContent || '').trim();
                    }

                    if (section.section_type) {
                        result.push(section);
                    }
                }
                return result;
            }""")
            return sections if sections else []
        except Exception as e:
            logger.debug("页面结构提取失败: %s", e)
            return []

    def _extract_images(self) -> List[dict]:
        """提取页面所有图片"""
        if not self._page:
            return []

        try:
            images = self._page.evaluate("""() => {
                const imgs = document.querySelectorAll('.app-main img, .main-container img, main img, body img');
                const seen = new Set();
                const result = [];
                imgs.forEach(img => {
                    const src = img.src || img.getAttribute('data-src') || '';
                    if (!src || seen.has(src) || src.startsWith('data:image/svg')) return;
                    // 跳过 base64 小图标
                    if (src.startsWith('data:') && src.length < 500) return;
                    seen.add(src);
                    result.push({
                        src: src,
                        alt: img.alt || '',
                        width: img.naturalWidth ? String(img.naturalWidth) : (img.width || ''),
                        height: img.naturalHeight ? String(img.naturalHeight) : (img.height || ''),
                        is_svg: src.endsWith('.svg') || src.includes('svg')
                    });
                });
                return result;
            }""")
            return images if images else []
        except Exception as e:
            logger.debug("图片提取失败: %s", e)
            return []

    def _extract_links(self) -> List[dict]:
        """提取页面所有链接"""
        if not self._page:
            return []

        try:
            links = self._page.evaluate("""(host) => {
                const anchors = document.querySelectorAll('.app-main a, .main-container a, main a, body a');
                const seen = new Set();
                const result = [];
                anchors.forEach(a => {
                    const href = a.href || '';
                    const text = (a.textContent || '').trim();
                    if (!href || href === '#' || href.startsWith('javascript:') || seen.has(href)) return;
                    if (text.length === 0 && !a.querySelector('img')) return;
                    seen.add(href);

                    let is_internal = false;
                    let link_type = 'external';
                    try {
                        if (href.startsWith(host)) {
                            is_internal = true;
                            if (href.includes('/dev-api/') || href.includes('/api/')) {
                                link_type = 'api';
                            } else if (/\\.(pdf|doc|docx|xls|xlsx|zip|rar)$/i.test(href)) {
                                link_type = 'file';
                            } else {
                                link_type = 'page';
                            }
                        }
                    } catch(e) {}

                    result.push({
                        href: href,
                        text: text.slice(0, 200),
                        is_internal: is_internal,
                        link_type: link_type
                    });
                });
                return result;
            }""", self._host)
            return links if links else []
        except Exception as e:
            logger.debug("链接提取失败: %s", e)
            return []

    def _extract_tables(self) -> List[dict]:
        """提取页面上所有 HTML 表格，智能合并 Element UI 分离的表头/表体"""
        if not self._page:
            return []

        try:
            tables = self._page.evaluate("""() => {
                const result = [];
                const seenEls = new Set();

                // 第一步：处理普通表格（非 Element UI 包装的）
                const regularTables = document.querySelectorAll(
                    '.app-main table:not(.el-table__header-wrapper table):not(.el-table__body-wrapper table):not(.el-table__footer-wrapper table), '
                    + '.main-container table:not(.el-table__header-wrapper table):not(.el-table__body-wrapper table):not(.el-table__footer-wrapper table), '
                    + 'main table:not(.el-table__header-wrapper table):not(.el-table__body-wrapper table):not(.el-table__footer-wrapper table)'
                );
                regularTables.forEach(tbl => {
                    if (tbl.closest('.el-table__column-resize-proxy')) return;
                    if (seenEls.has(tbl)) return;
                    seenEls.add(tbl);

                    const headers = [];
                    const thElements = tbl.querySelectorAll('thead th, thead td');
                    thElements.forEach(th => {
                        const txt = (th.textContent || '').trim();
                        if (txt) headers.push(txt);
                    });

                    const rows = [];
                    const bodyRows = tbl.querySelectorAll('tbody tr');
                    bodyRows.forEach(tr => {
                        const row = [];
                        tr.querySelectorAll('td, th').forEach(td => {
                            row.push((td.textContent || '').trim());
                        });
                        if (row.length > 0 && row.some(c => c !== '')) rows.push(row);
                    });

                    // 如果 tbody 为空，尝试从所有 tr 提取（跳过表头行）
                    if (rows.length === 0) {
                        const allTr = tbl.querySelectorAll('tr');
                        const dataStart = headers.length > 0 ? 1 : 0;
                        allTr.forEach((tr, ri) => {
                            if (ri < dataStart) return;
                            const row = [];
                            tr.querySelectorAll('td, th').forEach(td => {
                                row.push((td.textContent || '').trim());
                            });
                            if (row.length > 0 && row.some(c => c !== '')) rows.push(row);
                        });
                    }

                    // 跳过空表格
                    if (rows.length > 0 || headers.length > 0) {
                        let caption = '';
                        const capEl = tbl.querySelector('caption');
                        if (capEl) caption = (capEl.textContent || '').trim();

                        result.push({
                            caption: caption,
                            headers: headers,
                            rows: rows,
                            row_count: rows.length,
                            col_count: headers.length || (rows.length > 0 ? rows[0].length : 0)
                        });
                    }
                });

                // 第二步：处理 Element UI 表格（表头+表体分离）
                const elTables = document.querySelectorAll('.el-table');
                elTables.forEach(elTable => {
                    // 跳过隐藏的/用于尺寸计算的 el-table
                    if (elTable.offsetParent === null && elTable.offsetWidth === 0 && elTable.offsetHeight === 0) return;

                    const headerWrapper = elTable.querySelector('.el-table__header-wrapper table');
                    const bodyWrapper = elTable.querySelector('.el-table__body-wrapper table');

                    if (!bodyWrapper) return;  // 没有表体忽略

                    // 提取表头（从 headerWrapper 或 bodyWrapper 的 thead）
                    const headers = [];
                    let headerSource = headerWrapper;
                    if (!headerSource) headerSource = bodyWrapper;
                    const thElements = headerSource.querySelectorAll('thead th, thead td');
                    thElements.forEach(th => {
                        const txt = (th.textContent || '').trim();
                        if (txt) headers.push(txt);
                    });

                    // 提取数据行（只从 bodyWrapper 的 tbody）
                    const rows = [];
                    const bodyTrs = bodyWrapper.querySelectorAll('tbody tr');
                    bodyTrs.forEach(tr => {
                        const row = [];
                        tr.querySelectorAll('td').forEach(td => {
                            row.push((td.textContent || '').trim());
                        });
                        // 过滤空行
                        if (row.length > 0 && row.some(c => c !== '')) rows.push(row);
                    });

                    // 过滤掉全空表格
                    if (rows.length === 0) return;

                    let caption = '';
                    const capEl = bodyWrapper.querySelector('caption');
                    if (capEl) caption = (capEl.textContent || '').trim();

                    result.push({
                        caption: caption,
                        headers: headers,
                        rows: rows,
                        row_count: rows.length,
                        col_count: headers.length || (rows.length > 0 ? rows[0].length : 0)
                    });
                });

                return result;
            }""")
            return tables if tables else []
        except Exception as e:
            logger.debug("表格提取失败: %s", e)
            return []

    def _extract_forms(self) -> List[dict]:
        """提取页面上所有表单字段"""
        if not self._page:
            return []

        try:
            forms = self._page.evaluate("""() => {
                const forms = document.querySelectorAll('.app-main form, .main-container form, main form, .el-form');
                const result = [];
                forms.forEach(form => {
                    const fields = [];
                    // Element UI 表单项
                    form.querySelectorAll('.el-form-item').forEach(item => {
                        const label = item.querySelector('.el-form-item__label');
                        const input = item.querySelector('input, textarea, select');
                        fields.push({
                            label: label ? (label.textContent || '').trim() : '',
                            name: input ? (input.name || input.getAttribute('data-field') || '') : '',
                            type: input ? (input.type || input.tagName.toLowerCase()) : '',
                            value: input ? (input.value || '') : '',
                            placeholder: input ? (input.placeholder || '') : ''
                        });
                    });

                    if (fields.length > 0) {
                        result.push({
                            form_id: form.id || form.getAttribute('name') || '',
                            form_action: form.action || '',
                            fields: fields
                        });
                    }
                });
                return result;
            }""")
            return forms if forms else []
        except Exception as e:
            logger.debug("表单提取失败: %s", e)
            return []


# ═══════════════ 便捷函数（供 Web 控制台调用） ═══════════════

def _filter_tables_fields(tables: List[dict], fields: List[str]) -> List[dict]:
    """根据选中的字段名筛选表格列

    Args:
        tables: [{"headers": [...], "rows": [[...], ...]}, ...]
        fields: 需要保留的字段名列表

    Returns:
        筛选后的表格列表（不改变原始数据）
    """
    if not fields or not tables:
        return tables

    field_set = set(f.strip() for f in fields if f.strip())
    if not field_set:
        return tables

    filtered = []
    for t in tables:
        headers = t.get("headers", [])
        rows = t.get("rows", [])

        # 找出匹配的列索引
        keep_indices = []
        for idx, h in enumerate(headers):
            if h.strip() in field_set:
                keep_indices.append(idx)

        if not keep_indices:
            # 没有匹配的列，跳过这个表格
            continue

        # 筛选表头和行
        new_headers = [headers[i] for i in keep_indices]
        new_rows = []
        for row in rows:
            new_row = [row[i] if i < len(row) else "" for i in keep_indices]
            new_rows.append(new_row)

        new_t = dict(t)
        new_t["headers"] = new_headers
        new_t["rows"] = new_rows
        new_t["row_count"] = len(new_rows)
        new_t["col_count"] = len(new_headers)
        filtered.append(new_t)

    return filtered


def scrape_single_page(host: str, username: str, password: str,
                       route_or_url: str, headless: bool = True,
                       fields: List[str] = None) -> dict:
    """
    便捷函数：登录 + 爬取单个页面

    Args:
        fields: 可选，只保留指定的表格字段列

    返回 dict 格式的 PageContent（不含 base64 截图减少体积）
    """
    scraper = PageScraper(headless=headless)
    try:
        if not scraper.login(host, username, password):
            return {"error": "登录失败", "url": route_or_url}

        # 判断是完整 URL 还是 Vue 路由
        if route_or_url.startswith("http://") or route_or_url.startswith("https://"):
            content = scraper.scrape_page(route_or_url)
        else:
            content = scraper.scrape_by_route(route_or_url)

        result = content.to_dict(include_screenshot=False)

        # 字段过滤
        if fields:
            result["tables"] = _filter_tables_fields(result.get("tables", []), fields)

        return result
    except Exception as e:
        logger.error("单页爬取异常: %s", e)
        return {"error": str(e), "url": route_or_url}
    finally:
        scraper.close()


def scrape_with_screenshot(host: str, username: str, password: str,
                           route_or_url: str, full_page: bool = False) -> dict:
    """
    便捷函数：登录 + 爬取 + 截图

    返回 dict 格式的 PageContent（含 base64 截图）
    """
    scraper = PageScraper(headless=True)
    try:
        if not scraper.login(host, username, password):
            return {"error": "登录失败", "url": route_or_url}

        if route_or_url.startswith("http://") or route_or_url.startswith("https://"):
            content = scraper.scrape_page(route_or_url)
        else:
            content = scraper.scrape_by_route(route_or_url)

        if full_page:
            content.screenshot_base64 = scraper.screenshot_full()
        else:
            content.screenshot_base64 = scraper.screenshot()

        return content.to_dict(include_screenshot=True)
    except Exception as e:
        logger.error("带截图的爬取异常: %s", e)
        return {"error": str(e), "url": route_or_url}
    finally:
        scraper.close()


def scrape_batch(host: str, username: str, password: str,
                 routes: List[str] = None, headless: bool = True) -> List[dict]:
    """
    便捷函数：批量爬取

    Args:
        host: 平台地址
        username: 用户名
        password: 密码
        routes: 路由列表，为空则使用全部默认路由。
                支持路由路径（如 /elderly/overview）和完整 URL。
        headless: 是否无头模式

    Returns:
        [dict, ...] 每个 dict 是 PageContent.to_dict()
    """
    scraper = PageScraper(headless=headless)
    results = []
    try:
        if not scraper.login(host, username, password):
            return [{"error": "登录失败", "url": host}]

        if routes is None:
            routes = [r["route"] for r in get_routes()]

        for route in routes:
            try:
                # 支持完整 URL 和路由路径两种形式
                if route.startswith("http://") or route.startswith("https://"):
                    content = scraper.scrape_page(route)
                else:
                    content = scraper.scrape_by_route(route)
                results.append(content.to_dict(include_screenshot=False))
            except Exception as e:
                logger.error("采集 %s 异常: %s", route, e)
                results.append({"error": str(e), "url": route, "route": route})

        return results
    except Exception as e:
        logger.error("批量爬取异常: %s", e)
        return results + [{"error": str(e)}]
    finally:
        scraper.close()


def scrape_paginated(host: str, username: str, password: str,
                     route_or_url: str, page_start: int = 1,
                     page_end: int = None, page_size: int = None,
                     headless: bool = True, fields: List[str] = None) -> dict:
    """
    便捷函数：登录 + 分页爬取

    Args:
        host: 平台地址
        username: 用户名
        password: 密码
        route_or_url: 页面路由或完整 URL
        page_start: 起始页码（从 1 开始）
        page_end: 结束页码（None = 自动全部）
        page_size: 每页条数（None = 保持默认）
        headless: 是否无头模式
        fields: 可选，只保留指定的表格字段列

    Returns:
        {pages, merged_tables, merged_links, ...} 见 scrape_with_pagination
    """
    scraper = PageScraper(headless=headless)
    try:
        if not scraper.login(host, username, password):
            return {"error": "登录失败", "total_pages_scraped": 0}

        result = scraper.scrape_with_pagination(
            route_or_url=route_or_url,
            page_start=page_start,
            page_end=page_end,
            page_size=page_size,
        )

        # 字段过滤：应用到每页的表格和合并表格
        if fields:
            result["merged_tables"] = _filter_tables_fields(
                result.get("merged_tables", []), fields
            )
            for page in result.get("pages", []):
                page["tables"] = _filter_tables_fields(
                    page.get("tables", []), fields
                )

        return result
    except Exception as e:
        logger.error("分页爬取异常: %s", e)
        return {"error": str(e), "total_pages_scraped": 0}
    finally:
        scraper.close()
