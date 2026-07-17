"""简单的文件服务器 —— 从 output 目录提供 dashboard 和数据"""
import http.server
import os
import sys

PORT = 8765
DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

os.chdir(DIR)
print(f"Serving from: {DIR}")
print(f"Dashboard: http://localhost:{PORT}/dashboard.html")

handler = http.server.SimpleHTTPRequestHandler
server = http.server.HTTPServer(("0.0.0.0", PORT), handler)
try:
    server.serve_forever()
except KeyboardInterrupt:
    server.shutdown()
