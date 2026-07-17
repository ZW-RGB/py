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

        返回所有行组成的列表
        """
        all_rows = []
        for page in range(1, max_pages + 1):
            result = self._request("GET", path, params={
                "pageNum": page,
                "pageSize": page_size
            })
            if result is None:
                break

            code = result.get("code")
            if code == 200:
                rows = result.get("rows", [])
                total = result.get("total", 0)
                all_rows.extend(rows)
                logger.debug(f"  {path} 第{page}页: {len(rows)}条, 累计{len(all_rows)}/{total}")
                if len(all_rows) >= total:
                    break
            else:
                logger.warning(f"  {path} 错误: {result.get('msg')}")
                break

        return all_rows
