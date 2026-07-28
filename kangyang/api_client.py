# -*- coding: utf-8 -*-
"""
若依 RuoYi-Vue API 客户端
处理登录认证、Token 管理、分页数据拉取
"""
import time
import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)


class RuoYiApiClient:
    """若依框架 API 客户端 —— 自动登录 + Token 管理"""

    def __init__(self, base_url, username, password):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.token = None
        self.token_expire_at = 0
        self.last_error = ""  # 上次登录失败的具体原因
        self.last_data_error = ""  # 最近一次数据接口的后端错误（区别于"无数据"）

    # ── 认证 ─────────────────────────────────────────

    def login(self):
        """登录获取 Token，返回 (成功, 错误原因)"""
        self.last_error = ""
        url = f"{self.base_url}/dev-api/login"
        logger.info(f"正在登录: {url} (用户: {self.username})")
        data = json.dumps({
            "username": self.username,
            "password": self.password,
            "code": "",
            "uuid": ""
        }).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json"
        })
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            raw_body = resp.read().decode("utf-8")
            result = json.loads(raw_body)
            logger.debug(f"登录响应: code={result.get('code')}, msg={result.get('msg')}")

            if result.get("code") == 200 and result.get("token"):
                self.token = result["token"]
                self.token_expire_at = time.time() + 3600
                logger.info(f"登录成功，Token: {self.token[:30]}...")
                return True

            # 登录失败，记录服务端返回的原因
            self.last_error = result.get("msg", "服务端未返回具体错误信息")
            logger.error(f"登录失败: {self.last_error}")
            return False

        except urllib.error.HTTPError as e:
            self.last_error = f"HTTP {e.code}: 服务器拒绝请求"
            logger.error(f"登录 HTTP 错误: {e}")
            return False
        except urllib.error.URLError as e:
            self.last_error = f"网络错误: 无法连接到 {self.base_url}（请检查地址是否正确、服务器是否运行）"
            logger.error(f"登录连接错误: {e}")
            return False
        except json.JSONDecodeError as e:
            self.last_error = f"服务器返回格式异常，非 JSON 响应"
            logger.error(f"登录响应解析失败: {e}")
            return False
        except Exception as e:
            self.last_error = f"未知异常: {e}"
            logger.error(f"登录异常: {e}")
            return False

    def ensure_auth(self):
        """确保已登录，过期自动刷新"""
        if self.token and time.time() < self.token_expire_at:
            return True
        return self.login()

    # ── API 调用 ─────────────────────────────────────

    def _request(self, method, path, params=None, data=None, retry=1):
        """发送带认证的 API 请求"""
        if not self.ensure_auth():
            return None

        url = self.base_url + path
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            url += "?" + query

        body = json.dumps(data).encode("utf-8") if data else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            resp = urllib.request.urlopen(req, timeout=30)
            return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry > 0:
                logger.warning("Token 过期，重新登录...")
                self.token = None
                return self._request(method, path, params, data, retry - 1)
            logger.error(f"HTTP {e.code}: {path}")
            return None
        except Exception as e:
            logger.error(f"请求异常 {path}: {e}")
            return None

    def get_list(self, path, page_size=100, max_pages=50):
        """分页拉取某个 list 接口的全部数据

        返回所有行组成的列表。

        弹性容错（关键）：若某页因后端坏数据（如生日字段序列化抛
        java.sql.SQLException: HOUR_OF_DAY: 0 -> 1）整体报错，不再直接放弃返回空，
        而是把该页拆成更小的分页逐个重试，跳过触发崩溃的坏行，继续抓取后续页。
        这样即使接口存在个别坏记录，也能取回其余绝大多数数据，而不是误报"无数据"。
        """
        all_rows = []
        self.last_data_error = ""
        for page in range(1, max_pages + 1):
            result = self._request("GET", path, params={
                "pageNum": page,
                "pageSize": page_size
            })
            if result is None:
                # 网络/鉴权失败（非后端业务错误）：停止并标记
                self.last_data_error = "请求失败（网络/鉴权），已停止翻页"
                logger.error(f"  {path} 第{page}页请求失败，停止")
                break

            code = result.get("code")
            if code == 200:
                rows = result.get("rows", []) or []
                if not rows:
                    break  # 已到末页（RuoYi 越界返回空）
                all_rows.extend(rows)
                total = result.get("total", 0)
                logger.debug(f"  {path} 第{page}页: {len(rows)}条, 累计{len(all_rows)}/{total}")
                if total and len(all_rows) >= total:
                    break
            else:
                # 后端业务报错：拆小分页跳过坏行，再继续后续页
                msg = result.get("msg", "")
                self.last_data_error = f"后端错误(code={code}): {str(msg)[:200]}"
                logger.warning(f"  {path} 第{page}页后端报错，缩小分页跳过坏行: {str(msg)[:120]}")
                recovered, chunk_skipped = self._recover_page(path, page, page_size)
                all_rows.extend(recovered)
                if chunk_skipped:
                    logger.warning(f"  {path} 因后端坏数据跳过 {chunk_skipped} 行")
                # 无法获知 total，继续下一页；若已到末页，下页 rows 为空会 break

        return all_rows

    def probe_endpoint(self, path, timeout=8):
        """轻量探测某 list 接口是否存在（灵活层兜底用）。

        仅发起单次最小分页请求（pageNum=1, pageSize=1），不触发坏行恢复逻辑，
        避免因个别坏数据误判接口不存在。

        返回 dict：
            {"exists": bool, "code": int|None, "msg": str, "row_count": int}
        - exists=True 当且仅当后端返回 code==200（接口存在；空表也算存在）。
        - 其余情况视为「未确认」（路由不存在 / 后端业务报错 / 网络异常），
          调用方应继续尝试其它候选路径，不要据此判定「无数据」。
        """
        if not self.ensure_auth():
            return {"exists": False, "code": None, "msg": "未登录", "row_count": 0}

        url = self.base_url + path
        query = "pageNum=1&pageSize=1"
        url += "?" + query
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 路由不存在通常 404；其余状态码视为未确认
            return {"exists": False, "code": e.code, "msg": f"HTTP {e.code}", "row_count": 0}
        except Exception as e:
            return {"exists": False, "code": None, "msg": f"探测异常: {e}", "row_count": 0}

        code = result.get("code")
        if code == 200:
            rows = result.get("rows", []) or []
            return {"exists": True, "code": 200, "msg": "", "row_count": len(rows)}
        # code != 200：接口存在但返回业务错误（如坏数据），或未授权等，均视为未确认
        return {"exists": False, "code": code, "msg": str(result.get("msg", ""))[:200], "row_count": 0}

    def _recover_page(self, path, page, page_size):
        """某页整体报错时，按更小分页逐个请求，跳过触发崩溃的坏行。

        策略：先以 10 行/页 重试该页各分段，能成功的直接收下；把仍报错的 10 行分段
        记为失败，再逐行(pageSize=1)精确重试，跳过那一行坏数据。
        返回 (rows, skipped)：恢复出的行 + 被跳过(无法恢复)的行数。
        """
        rows = []
        skipped = 0
        base = (page - 1) * page_size  # 该页首行在全局的序号(从 0 计)
        chunk = 10
        num_chunks = (page_size + chunk - 1) // chunk
        failed_chunks = []
        for k in range(num_chunks):
            start_row = base + k * chunk
            pn = start_row // chunk + 1  # RuoYi pageNum 从 1 开始
            sub = self._request("GET", path, params={
                "pageNum": pn,
                "pageSize": chunk
            })
            if sub is None:
                skipped += chunk
                continue
            if sub.get("code") == 200:
                rows.extend(sub.get("rows", []) or [])
            else:
                failed_chunks.append(k)
        # 对失败的 10 行分段逐行精确重试，跳过坏行
        for k in failed_chunks:
            start_row = base + k * chunk
            for j in range(chunk):
                pn = start_row + j + 1  # 单行: 全局 0-based 行号 + 1
                one = self._request("GET", path, params={
                    "pageNum": pn,
                    "pageSize": 1
                })
                if one is not None and one.get("code") == 200:
                    rows.extend(one.get("rows", []) or [])
                else:
                    skipped += 1
        return rows, skipped
