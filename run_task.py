#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
康养数据采集任务 — 命令行执行接口

用法:
  # 列出所有任务
  python run_task.py --list

  # 执行指定任务
  python run_task.py --task <任务名称或ID> --username admin --password admin123

  # 执行并指定输出格式
  python run_task.py --task my_task --output json --output-file result.json --pretty

  # 查看任务详情
  python run_task.py --task my_task --info

  # 导出/导入任务
  python run_task.py --task my_task --export
  python run_task.py --import task_backup.json

参数:
  --task TASK        任务名称或 ID（必需，除非 --list）
  --list             列出所有可用任务
  --info             查看任务配置详情
  --username USER    登录用户名（覆盖任务默认值）
  --password PASS    登录密码
  --host HOST        平台地址（覆盖任务默认值）
  --output FORMAT    输出格式: csv / json / xlsx / txt（默认 json）
  --output-file FILE 输出文件路径（默认 stdout）
  --pretty           美化 JSON 输出
  --max-rows N       最大数据行数（0=不限制，默认 5000）
  --headless         无头模式（默认）
  --no-headless      显示浏览器窗口
  --export           导出任务为 JSON
  --import FILE      从文件导入任务
  --delete           删除指定任务（需确认）
  --help             显示帮助
"""
import argparse
import json
import os
import sys

# 把项目根目录加入 path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from kangyang.task_manager import (
    list_tasks, get_task, mark_task_run, export_task, import_task, delete_task
)
from kangyang.visual_collector import execute_collection


def format_output(data: dict, fmt: str, pretty: bool = False) -> str:
    """格式化输出"""
    if fmt == "json":
        indent = 2 if pretty else None
        return json.dumps(data, ensure_ascii=False, indent=indent, default=str)
    elif fmt == "csv":
        return _to_csv(data["data"])
    elif fmt == "txt":
        return _to_txt(data)
    elif fmt == "xlsx":
        return "[xlsx 格式请在交互式环境使用]"  # xlsx 需要二进制写入
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _to_csv(result_data: dict) -> str:
    """将采集数据转为 CSV 字符串"""
    import csv
    import io

    output = io.StringIO()
    output.write("\ufeff")  # UTF-8 BOM
    writer = csv.writer(output)

    columns = result_data.get("columns", [])
    rows = result_data.get("rows", [])

    if columns:
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row.get(col, "") for col in columns])

    return output.getvalue()


def _to_txt(data: dict) -> str:
    """将采集数据转为可读文本"""
    lines = []
    result_data = data.get("data", {})
    columns = result_data.get("columns", [])
    rows = result_data.get("rows", [])
    total = result_data.get("total", 0)

    lines.append("=" * 60)
    lines.append("  康养平台可视化采集结果")
    lines.append(f"  时间: {_now()}")
    lines.append(f"  总记录: {total} 条, {len(columns)} 个字段")
    lines.append("=" * 60)
    lines.append("")

    if columns:
        lines.append(" | ".join(columns))
        lines.append("-" * 50)
        for row in rows[:500]:
            lines.append(" | ".join([str(row.get(col, ""))[:80] for col in columns]))
        if len(rows) > 500:
            lines.append(f"... 还有 {len(rows) - 500} 行未显示")

    errors = data.get("errors", [])
    if errors:
        lines.append("")
        lines.append("─" * 40)
        lines.append("错误/警告:")
        for e in errors:
            lines.append(f"  [{e.get('field', '')}] {e.get('error', '')}")

    return "\n".join(lines)


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def find_task(task_ref: str):
    """通过名称或 ID 查找任务"""
    # 先尝试直接按 ID 查找
    task = get_task(task_ref)
    if task:
        return task

    # 按名称模糊匹配
    tasks = list_tasks()
    task_ref_lower = task_ref.lower()
    for t in tasks:
        if t["name"].lower() == task_ref_lower:
            return get_task(t["task_id"])
        if task_ref_lower in t["name"].lower():
            return get_task(t["task_id"])
        if t["task_id"].startswith(task_ref):
            return get_task(t["task_id"])

    return None


def main():
    parser = argparse.ArgumentParser(
        description="康养数据采集任务 - 命令行执行接口",
        add_help=False,
    )
    parser.add_argument("--task", type=str, help="任务名称或 ID")
    parser.add_argument("--list", action="store_true", help="列出所有任务")
    parser.add_argument("--info", action="store_true", help="查看任务详情")
    parser.add_argument("--username", type=str, default="", help="登录用户名")
    parser.add_argument("--password", type=str, default="", help="登录密码")
    parser.add_argument("--host", type=str, default="", help="平台地址（覆盖任务默认）")
    parser.add_argument("--output", type=str, default="json", help="输出格式: csv/json/txt")
    parser.add_argument("--output-file", type=str, default="", help="输出文件路径")
    parser.add_argument("--pretty", action="store_true", help="美化 JSON")
    parser.add_argument("--max-rows", type=int, default=5000, help="最大数据行数")
    parser.add_argument("--no-headless", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--export", action="store_true", help="导出任务为 JSON")
    parser.add_argument("--import", dest="import_file", type=str, help="从文件导入任务")
    parser.add_argument("--delete", action="store_true", help="删除任务")
    parser.add_argument("--help", action="store_true", help="显示帮助")

    args = parser.parse_args()

    if args.help or (not args.list and not args.task and not args.import_file):
        parser.print_help()
        return 0

    # ── 列出任务 ──
    if args.list:
        tasks = list_tasks()
        if not tasks:
            print("（没有已保存的采集任务）")
            return 0
        print(f"\n{'ID':<14} {'名称':<25} {'规则':>5} {'执行':>5} {'更新时间'}")
        print("-" * 80)
        for t in tasks:
            print(f"{t['task_id']:<14} {t['name'][:23]:<25} {t['rules_count']:>5} {t['total_runs']:>5} {t.get('updated_at','')[:19]}")
        print(f"\n共 {len(tasks)} 个任务")
        return 0

    # ── 导入任务 ──
    if args.import_file:
        try:
            with open(args.import_file, "r", encoding="utf-8") as f:
                json_str = f.read()
            task = import_task(json_str)
            if task:
                print(f"✅ 任务已导入: {task.name} (ID: {task.task_id})")
            else:
                print("❌ 导入失败：JSON 格式无效", file=sys.stderr)
                return 1
        except FileNotFoundError:
            print(f"❌ 文件不存在: {args.import_file}", file=sys.stderr)
            return 1
        return 0

    # ── 查找任务 ──
    task = find_task(args.task)
    if not task:
        print(f"❌ 未找到任务: {args.task}", file=sys.stderr)
        print("使用 --list 查看所有可用任务", file=sys.stderr)
        return 1

    # ── 导出 ──
    if args.export:
        json_str = export_task(task.task_id)
        if json_str:
            print(json_str)
        else:
            print("❌ 导出失败", file=sys.stderr)
            return 1
        return 0

    # ── 删除 ──
    if args.delete:
        confirm = input(f"确认删除任务 '{task.name}'? [y/N] ")
        if confirm.lower() == "y":
            delete_task(task.task_id)
            print(f"✅ 已删除: {task.name}")
        else:
            print("已取消")
        return 0

    # ── 查看详情 ──
    if args.info:
        d = task.to_dict()
        print(json.dumps(d, ensure_ascii=False, indent=2, default=str))
        return 0

    # ── 执行采集 ──
    host = args.host or task.login_host
    username = args.username
    password = args.password

    if not host:
        print("❌ 未配置平台地址（--host 或任务默认值）", file=sys.stderr)
        return 1

    print(f"🚀 执行采集任务: {task.name}")
    print(f"   目标: {task.target_url or task.route}")
    print(f"   规则: {len(task.rules)} 条")
    print(f"   最大行数: {args.max_rows if args.max_rows > 0 else '不限制'}")
    print()

    result = execute_collection(
        task=task,
        username=username,
        password=password,
        headless=not args.no_headless,
        max_rows=args.max_rows,
    )

    if result["success"]:
        mark_task_run(task.task_id)

        output = format_output(result, args.output, args.pretty)

        if args.output_file:
            with open(args.output_file, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"✅ 结果已保存到: {args.output_file}")
        else:
            print(output)

        data = result["data"]
        errors = result.get("errors", [])
        print(f"\n📊 采集完成: {data.get('total', 0)} 条记录, {len(data.get('columns', []))} 个字段, {data.get('pages', 1)} 页")
        if errors:
            print(f"⚠️  {len(errors)} 个警告/错误")
        return 0
    else:
        error = result.get("error", "未知错误")
        print(f"❌ 采集失败: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
