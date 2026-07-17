# -*- coding: utf-8 -*-
"""扫描目标 Vue 平台的 JS 文件，提取 API 端点"""
import urllib.request
import re
import sys

BASE = "http://192.168.18.143:1024"

def fetch(path):
    url = BASE + path
    try:
        resp = urllib.request.urlopen(url, timeout=30)
        return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [ERROR] {path}: {e}")
        return ""

# ---- 1. 下载 JS 文件 ----
print("=" * 60)
print("1. 下载 JS 文件")
vendor = fetch("/static/js/chunk-vendors.js")
app = fetch("/static/js/app.js")
print(f"   vendor: {len(vendor)} 字符")
print(f"   app:    {len(app)} 字符")

# ---- 2. 从 vendor 中搜 axios 配置 ----
print("\n" + "=" * 60)
print("2. Vendor 中的 API 配置线索")
for keyword in ["baseURL", "VUE_APP", "prod-api", "dev-api", "/api", "process.env"]:
    matches = [m for m in re.finditer(re.escape(keyword), vendor)]
    if matches:
        # 只取前5个匹配的上下文
        for m in matches[:5]:
            start = max(0, m.start() - 50)
            end = min(len(vendor), m.end() + 80)
            snippet = vendor[start:end].replace("\n", " ")
            print(f"   [{keyword}] ...{snippet}...")
    else:
        print(f"   [{keyword}] 未找到")

# ---- 3. 从 app.js 中搜 API 路径 ----
print("\n" + "=" * 60)
print("3. App.js 中的 API 路径")
# 搜索常见的 API 路径模式
combined = vendor + " " + app

# 找所有 /xxx/xxx 路径（不包括静态资源）
all_paths = re.findall(r'["\x27](/(?:[a-zA-Z][\w/-]*))["\x27]', combined)
api_candidates = set()
for p in all_paths:
    p = p.strip()
    if p.startswith("/static/"):
        continue
    if len(p) > 3 and "/" in p[1:]:
        api_candidates.add(p)

# 优先显示像 API 的
print("   可能的 API 端点:")
for p in sorted(api_candidates):
    # 过滤明显不是 API 的
    skip_kw = [".js", ".css", ".png", ".jpg", ".svg", ".woff", ".ttf", ".ico", "node_modules"]
    if any(k in p for k in skip_kw):
        continue
    print(f"     {p}")

# ---- 4. 搜索特定关键词 ----
print("\n" + "=" * 60)
print("4. 搜索 axios/request 调用模式")
# 找 .get( / .post( 后面的路径
http_calls = re.findall(r'\.(?:get|post|put|delete|request)\s*\(\s*["\x27]([^"\x27]+)["\x27]', combined)
print("   直接 HTTP 调用:")
for c in set(http_calls)[:30]:
    print(f"     {c}")

# 找带变量的模板路径
template_paths = re.findall(r'["\x27](/[\w/]+\$\{[\w.]+\}[\w/]*)["\x27]', combined)
template_paths += re.findall(r'["\x27](/[\w/]+:[^/"\x27]+)["\x27]', combined)
print("\n   模板化路径 (含变量):")
for t in set(template_paths)[:20]:
    print(f"     {t}")

# ---- 5. 尝试直接探测常见的后端 API 路径 ----
print("\n" + "=" * 60)
print("5. 主动探测常见 API 路径")
common_paths = [
    "/prod-api/", "/dev-api/", "/api/", "/api/v1/",
    "/prod-api/system/menu", "/prod-api/login",
    "/api/user/login", "/api/user/info",
    "/prod-api/elderly/list", "/prod-api/institution/list",
    "/api/elderly/page", "/api/org/list",
    "/prod-api/system/dict/data",
    "/swagger-ui.html", "/doc.html", "/v2/api-docs",
]
for path in common_paths:
    try:
        req = urllib.request.Request(BASE + path, method="GET")
        resp = urllib.request.urlopen(req, timeout=5)
        data = resp.read().decode("utf-8", errors="replace")[:200]
        print(f"   [200] {path} -> {data[:80].strip()}")
    except urllib.error.HTTPError as e:
        if e.code in [401, 403]:
            print(f"   [{e.code}] {path} -> 需要认证（说明路径存在！）")
        elif e.code == 404:
            pass  # 忽略
        else:
            print(f"   [{e.code}] {path}")
    except Exception:
        pass
