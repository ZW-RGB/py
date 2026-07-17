# -*- coding: utf-8 -*-
"""深度扫描：找到 RuoYi 框架的 API 代理前缀和实际请求路径"""
import urllib.request
import re

BASE = "http://192.168.18.143:1024"

def fetch(path):
    try:
        resp = urllib.request.urlopen(BASE + path, timeout=30)
        return resp.read().decode("utf-8", errors="replace")
    except:
        return ""

app = fetch("/static/js/app.js")
vendor = fetch("/static/js/chunk-vendors.js")
combined = vendor + " " + app

print("=" * 60)
print("0. 确认框架版本")
# RuoYi 版本特征
for kw in ["ruoyi", "RuoYi", "若依", "ry_", "vue.config", "proxy", "REACT_APP", "VUE_APP"]:
    matches = list(re.finditer(re.escape(kw), combined, re.I))
    if matches:
        print(f"  [{kw}] 找到 {len(matches)} 处")

print("\n" + "=" * 60)
print("1. 搜索代理前缀 (dev-api / prod-api / api)")
for prefix in ["dev-api", "prod-api", "/api", "basePath", "BASE_API", "base_api"]:
    count = len(re.findall(re.escape(prefix), combined, re.I))
    print(f"  [{prefix}]: {count} 处")

print("\n" + "=" * 60)
print("2. 搜索 request 工具类中的 baseURL")
# RuoYi 的 request.js 模式
patterns = [
    r'baseURL\s*:\s*["\x27]([^"\x27]+)["\x27]',
    r'baseURL\s*=\s*["\x27]([^"\x27]+)["\x27]',
    r'process\.env\.\w+_BASE_API',
    r'import\.meta\.env\.\w+',
]
for p in patterns:
    matches = re.findall(p, combined)
    if matches:
        for m in set(matches[:10]):
            print(f"  {p} -> {m}")

print("\n" + "=" * 60)
print("3. 搜索 axios 实际调用的 URL 模式")
# 在 app.js 中找 request({url:"/xxx"}) 模式
api_calls = re.findall(r'url\s*:\s*["\x27]([^"\x27]+)["\x27]', app)
unique = list(set(api_calls))
print(f"  找到 {len(unique)} 个独特的 url 模式")
for u in sorted(unique)[:50]:
    print(f"    {u}")

print("\n" + "=" * 60)
print("4. 主动探测 API 端点（带常见前缀）")
prefixes = ["", "/dev-api", "/prod-api", "/api"]
test_endpoints = [
    "/system/user/list?pageNum=1&pageSize=1",
    "/elderlyCare/EnrollmentProcessing/list?pageNum=1&pageSize=1",
    "/basicinformation/community/list?pageNum=1&pageSize=1",
    "/oldCare/oldCare/list?pageNum=1&pageSize=1",
    "/enrollmentApply/enrollmentApply/list?pageNum=1&pageSize=1",
    "/captchaImage",
    "/login",
]

for prefix in prefixes:
    for ep in test_endpoints:
        path = prefix + ep
        try:
            req = urllib.request.Request(BASE + path)
            resp = urllib.request.urlopen(req, timeout=5)
            data = resp.read().decode("utf-8", errors="replace")[:150].strip()
            print(f"  [200] {path}")
            print(f"        -> {data}")
        except urllib.error.HTTPError as e:
            code = e.code
            if code in [200, 401, 403, 405, 500]:
                body = e.read().decode("utf-8", errors="replace")[:100]
                print(f"  [{code}] {path} -> {body}")
            elif code == 404:
                pass  # 忽略
            else:
                print(f"  [{code}] {path}")
        except Exception as e:
            pass
