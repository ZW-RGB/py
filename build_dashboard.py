# -*- coding: utf-8 -*-
"""生成康养数据可视化 Web 大盘 —— 按需加载版"""
import json, os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def generate_dashboard():
    results_path = os.path.join(PROJECT_ROOT, "output", "results.json")
    with open(results_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 只嵌入摘要信息，不嵌 records
    summary = []
    for t in data["tables"]:
        summary.append({
            "table_name": t["table_name"],
            "module": t["module"],
            "endpoint": t["endpoint"],
            "total_count": t["total_count"],
            "collected_count": t["collected_count"],
            "success": t["success"],
            "error_msg": t.get("error_msg", ""),
            "columns": t["columns"],
        })
    summary_json = json.dumps(summary, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>智慧康养平台 · 数据采集大盘</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; background:#f0f2f5; color:#333; }}
.header {{ background: linear-gradient(135deg, #1a73e8, #0d47a1); color:#fff; padding:18px 32px; display:flex; justify-content:space-between; align-items:center; }}
.header h1 {{ font-size:22px; font-weight:600; }}
.header .info {{ font-size:13px; opacity:0.85; }}
.stats {{ display:flex; gap:16px; padding:20px 32px; background:#fff; box-shadow:0 1px 4px rgba(0,0,0,0.06); margin-bottom:4px; }}
.stat-card {{ flex:1; background:#f8fafd; border-radius:10px; padding:16px 20px; border-left:4px solid #1a73e8; }}
.stat-card.success {{ border-left-color:#34a853; }}
.stat-card.fail {{ border-left-color:#ea4335; }}
.stat-card .num {{ font-size:32px; font-weight:700; color:#1a73e8; }}
.stat-card.success .num {{ color:#34a853; }}
.stat-card.fail .num {{ color:#ea4335; }}
.stat-card .label {{ font-size:13px; color:#666; margin-top:4px; }}
.layout {{ display:flex; height:calc(100vh - 180px); }}
.sidebar {{ width:240px; background:#fff; overflow-y:auto; border-right:1px solid #e8e8e8; padding:8px 0; flex-shrink:0; }}
.sidebar h3 {{ font-size:12px; color:#999; padding:10px 20px; text-transform:uppercase; letter-spacing:1px; }}
.sidebar-item {{ padding:10px 20px; cursor:pointer; font-size:14px; display:flex; justify-content:space-between; align-items:center; transition:all 0.15s; border-left:3px solid transparent; }}
.sidebar-item:hover {{ background:#f0f7ff; }}
.sidebar-item.active {{ background:#e8f0fe; border-left-color:#1a73e8; color:#1a73e8; font-weight:600; }}
.sidebar-item .badge {{ background:#e8e8e8; color:#666; font-size:11px; padding:2px 8px; border-radius:10px; min-width:38px; text-align:center; }}
.sidebar-item.active .badge {{ background:#1a73e8; color:#fff; }}
.sidebar-item.failed {{ opacity:0.5; }}
.main {{ flex:1; overflow-y:auto; padding:24px 32px; background:#f0f2f5; }}
.panel-title {{ font-size:18px; font-weight:600; margin-bottom:16px; color:#1a73e8; }}
.panel-title small {{ font-size:13px; font-weight:normal; color:#999; }}
.toolbar {{ display:flex; gap:12px; margin-bottom:16px; align-items:center; flex-wrap:wrap; }}
.toolbar input {{ padding:8px 14px; border:1px solid #d9d9d9; border-radius:6px; font-size:13px; width:260px; outline:none; }}
.toolbar input:focus {{ border-color:#1a73e8; box-shadow:0 0 0 2px rgba(26,115,232,0.15); }}
.toolbar select {{ padding:8px 12px; border:1px solid #d9d9d9; border-radius:6px; font-size:13px; outline:none; background:#fff; }}
.toolbar .result-count {{ font-size:13px; color:#666; margin-left:auto; }}
.table-wrap {{ background:#fff; border-radius:10px; overflow:auto; box-shadow:0 1px 4px rgba(0,0,0,0.06); max-height:calc(100vh - 380px); }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
thead {{ position:sticky; top:0; z-index:2; }}
th {{ background:#f5f6f8; padding:10px 14px; text-align:left; font-weight:600; border-bottom:2px solid #e0e0e0; white-space:nowrap; color:#555; font-size:12px; }}
td {{ padding:9px 14px; border-bottom:1px solid #f0f0f0; white-space:nowrap; max-width:350px; overflow:hidden; text-overflow:ellipsis; }}
tr:hover td {{ background:#f8fbff; }}
.pagination {{ display:flex; justify-content:center; align-items:center; gap:8px; padding:20px; }}
.pagination button {{ border:1px solid #d9d9d9; background:#fff; padding:7px 16px; border-radius:6px; cursor:pointer; font-size:13px; transition:all 0.15s; }}
.pagination button:hover:not(:disabled) {{ border-color:#1a73e8; color:#1a73e8; }}
.pagination button:disabled {{ opacity:0.4; cursor:default; }}
.empty-state {{ text-align:center; padding:80px 20px; color:#bbb; }}
.empty-state .icon {{ font-size:56px; margin-bottom:16px; }}
.loading {{ text-align:center; padding:80px; color:#1a73e8; font-size:15px; }}
.endpoint-tag {{ font-size:11px; color:#999; margin-left:8px; font-family:monospace; }}
</style>
</head>
<body>
<div class="header">
  <div><h1>🏥 智慧康养平台 · 数据采集大盘</h1></div>
  <div class="info">平台: {data['base_url']} &nbsp;|&nbsp; 采集时间: <span id="time"></span></div>
</div>
<div class="stats" id="stats"></div>
<div class="layout">
  <div class="sidebar" id="sidebar"></div>
  <div class="main" id="main">
    <h2 class="panel-title">请从左侧选择数据表</h2>
    <div class="empty-state"><div class="icon">📊</div><p>点击左侧数据表，查看详细数据和内容</p></div>
  </div>
</div>

<script>
document.getElementById('time').textContent = new Date().toLocaleString('zh-CN');

const SUMMARY = {summary_json};
let fullData = null;
let currentTableInfo = null;
let currentRecords = [];
let currentPage = 1;
let pageSize = 50;
let searchTerm = '';

// 统计卡片
function renderStats() {{
  const success = SUMMARY.filter(t => t.success).length;
  const fail = SUMMARY.filter(t => !t.success).length;
  const total = SUMMARY.reduce((s, t) => s + t.collected_count, 0);
  document.getElementById('stats').innerHTML = `
    <div class="stat-card"><div class="num">${{SUMMARY.length}}</div><div class="label">数据表</div></div>
    <div class="stat-card success"><div class="num">${{total.toLocaleString()}}</div><div class="label">总记录数</div></div>
    <div class="stat-card success"><div class="num">${{success}}</div><div class="label">采集成功</div></div>
    <div class="stat-card fail"><div class="num">${{fail}}</div><div class="label">采集失败</div></div>
  `;
}}

// 侧边栏
function renderSidebar() {{
  let html = '<h3>数据表列表</h3>';
  SUMMARY.forEach((t, i) => {{
    const cls = (currentTableInfo && currentTableInfo.table_name === t.table_name) ? 'active' : '';
    const failCls = !t.success ? 'failed' : '';
    html += `<div class="sidebar-item ${{cls}} ${{failCls}}" onclick="selectTable(${{i}})">
      <span>${{t.table_name}}</span>
      <span class="badge">${{t.collected_count}}</span>
    </div>`;
  }});
  document.getElementById('sidebar').innerHTML = html;
}}

// 选中表
async function selectTable(idx) {{
  currentTableInfo = SUMMARY[idx];
  currentPage = 1;
  searchTerm = '';
  const si = document.getElementById('search-input');
  if (si) si.value = '';
  renderSidebar();

  if (!currentTableInfo.success) {{
    renderFailedTable();
    return;
  }}

  // 按需加载完整数据
  document.getElementById('main').innerHTML = '<div class="loading">⏳ 加载中...</div>';

  if (!fullData) {{
    try {{
      const resp = await fetch('results.json');
      fullData = await resp.json();
    }} catch(e) {{
      document.getElementById('main').innerHTML = '<div class="empty-state"><div class="icon">❌</div><p>加载数据失败: ' + e.message + '</p></div>';
      return;
    }}
  }}

  // 找到对应表的完整 records
  const table = fullData.tables.find(t => t.table_name === currentTableInfo.table_name);
  currentRecords = (table && table.records) ? table.records : [];
  renderTable();
}}

// 失败表
function renderFailedTable() {{
  document.getElementById('main').innerHTML = `
    <h2 class="panel-title">${{currentTableInfo.table_name}} <small style="color:#ea4335;">采集失败</small></h2>
    <div class="empty-state">
      <div class="icon">⚠️</div>
      <p>${{currentTableInfo.error_msg || '该表无数据或后端接口异常'}}</p>
      <p style="font-size:13px;color:#999;margin-top:8px;"><span class="endpoint-tag">${{currentTableInfo.endpoint}}</span></p>
    </div>`;
}}

// 渲染数据表格
function renderTable() {{
  const t = currentTableInfo;
  const records = currentRecords;

  // 搜索过滤
  let filtered = records;
  if (searchTerm) {{
    const term = searchTerm.toLowerCase();
    filtered = records.filter(r => Object.values(r).some(v => v != null && String(v).toLowerCase().includes(term)));
  }}

  const totalPages = Math.ceil(filtered.length / pageSize) || 1;
  if (currentPage > totalPages) currentPage = totalPages;
  const start = (currentPage - 1) * pageSize;
  const pageData = filtered.slice(start, start + pageSize);

  // 排除全空系统列，优先显示有数据的列
  const skipCols = ['params', 'searchValue'];
  const lowPriorityCols = ['createBy', 'createTime', 'updateBy', 'updateTime', 'remark'];
  let displayCols = t.columns.filter(c => !skipCols.includes(c));
  // 把低频列放到最后，如果全空则不显示
  const mainCols = displayCols.filter(c => !lowPriorityCols.includes(c));
  const sysCols = displayCols.filter(c => lowPriorityCols.includes(c));
  // 只有在有数据时才显示系统列
  const hasSysData = records.length > 0 && sysCols.some(c => records.some(r => r[c] !== null && r[c] !== ''));
  displayCols = hasSysData ? [...mainCols, ...sysCols] : mainCols;

  let html = `<h2 class="panel-title">${{t.table_name}} <small>共 ${{records.length.toLocaleString()}} 条记录</small><span class="endpoint-tag">${{t.endpoint}}</span></h2>
  <div class="toolbar">
    <input id="search-input" type="text" placeholder="🔍 搜索 ${{t.table_name}}..." value="${{searchTerm}}" oninput="onSearch(this.value)">
    <select onchange="onPageSizeChange(this.value)">
      <option value="20" ${{pageSize==20?'selected':''}}>每页 20 条</option>
      <option value="50" ${{pageSize==50?'selected':''}}>每页 50 条</option>
      <option value="100" ${{pageSize==100?'selected':''}}>每页 100 条</option>
      <option value="200" ${{pageSize==200?'selected':''}}>每页 200 条</option>
    </select>
    <span class="result-count">${{searchTerm ? '筛选结果: ' + filtered.length.toLocaleString() + ' / ' : ''}}${{records.length.toLocaleString()}} 条</span>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr>${{displayCols.map(c => `<th>${{c}}</th>`).join('')}}</tr></thead>
      <tbody>${{pageData.map(r => `<tr>${{displayCols.map(c => {{
        const v = r[c];
        if (v === null || v === undefined) return '<td style="color:#ccc">-</td>';
        const sv = String(v);
        if (sv.length > 60) return `<td title="${{sv.replace(/"/g,'&quot;')}}">${{sv.slice(0,58)+'...'}}</td>`;
        return `<td>${{sv}}</td>`;
      }}).join('')}}</tr>`).join('')}}</tbody>
    </table>
  </div>
  <div class="pagination">
    <button onclick="changePage(1)" ${{currentPage===1?'disabled':''}}>首页</button>
    <button onclick="changePage(${{currentPage-1}})" ${{currentPage===1?'disabled':''}}>上一页</button>
    <span style="font-size:13px;color:#666;">第 ${{currentPage}} / ${{totalPages}} 页</span>
    <button onclick="changePage(${{currentPage+1}})" ${{currentPage===totalPages?'disabled':''}}>下一页</button>
    <button onclick="changePage(${{totalPages}})" ${{currentPage===totalPages?'disabled':''}}>末页</button>
  </div>`;
  document.getElementById('main').innerHTML = html;
  document.getElementById('main').scrollTop = 0;
}}

function onSearch(val) {{ searchTerm = val; currentPage = 1; renderTable(); }}
function onPageSizeChange(size) {{ pageSize = parseInt(size); currentPage = 1; renderTable(); }}
function changePage(p) {{ currentPage = p; renderTable(); document.getElementById('main').scrollTop = 0; }}

// 启动
renderStats();
renderSidebar();
// 默认选第一个成功的表
const firstSuccess = SUMMARY.findIndex(t => t.success && t.collected_count > 0);
if (firstSuccess >= 0) selectTable(firstSuccess);
</script>
</body>
</html>"""

    output_path = os.path.join(PROJECT_ROOT, "output", "dashboard.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"[OK] 数据大盘已生成: {output_path}")
    print(f"     文件大小: {size_kb:.0f} KB (不含数据)")
    return output_path


if __name__ == "__main__":
    generate_dashboard()
