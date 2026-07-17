# -*- coding: utf-8 -*-
"""
采集任务管理器 — 任务的持久化存储与 CRUD 操作

存储: JSON 文件 → output/collector_tasks/
"""
import json
import os
import shutil
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from kangyang.visual_collector import CollectionTask

# 任务存储目录
_TASKS_DIR = None


def _get_tasks_dir() -> str:
    global _TASKS_DIR
    if _TASKS_DIR is None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _TASKS_DIR = os.path.join(project_root, "output", "collector_tasks")
    os.makedirs(_TASKS_DIR, exist_ok=True)
    return _TASKS_DIR


def _task_path(task_id: str) -> str:
    return os.path.join(_get_tasks_dir(), f"{task_id}.json")


# ═══════════════════════════════════════════════════
# CRUD 操作
# ═══════════════════════════════════════════════════


def create_task(
    name: str,
    target_url: str,
    description: str = "",
    route: str = "",
    login_required: bool = True,
    login_host: str = "",
    rules: list = None,
    pagination: dict = None,
    export_format: str = "csv",
) -> CollectionTask:
    """创建新的采集任务"""
    now = datetime.now().isoformat()
    task = CollectionTask(
        task_id=uuid.uuid4().hex[:12],
        name=name,
        description=description,
        target_url=target_url,
        route=route,
        login_required=login_required,
        login_host=login_host,
        rules=rules or [],
        pagination=pagination or {"enabled": False},
        export_format=export_format,
        created_at=now,
        updated_at=now,
    )
    _save_task(task)
    return task


def get_task(task_id: str) -> Optional[CollectionTask]:
    """获取单个任务"""
    path = _task_path(task_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return CollectionTask.from_dict(data)
    except (json.JSONDecodeError, KeyError):
        return None


def list_tasks() -> List[Dict]:
    """列出所有任务（摘要）"""
    tasks = []
    tasks_dir = _get_tasks_dir()
    if not os.path.exists(tasks_dir):
        return tasks

    for fname in sorted(os.listdir(tasks_dir), reverse=True):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(tasks_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            tasks.append({
                "task_id": data.get("task_id", ""),
                "name": data.get("name", ""),
                "description": data.get("description", ""),
                "target_url": data.get("target_url", ""),
                "route": data.get("route", ""),
                "rules_count": len(data.get("rules", [])),
                "export_format": data.get("export_format", "csv"),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
                "last_run": data.get("last_run"),
                "total_runs": data.get("total_runs", 0),
            })
        except (json.JSONDecodeError, KeyError):
            continue
    return tasks


def update_task(task_id: str, updates: dict) -> Optional[CollectionTask]:
    """更新任务"""
    task = get_task(task_id)
    if not task:
        return None

    # 更新简单字段
    simple_fields = {"name", "description", "target_url", "route",
                     "login_required", "login_host", "export_format"}
    for k, v in updates.items():
        if k in simple_fields:
            setattr(task, k, v)
        elif k == "rules":
            from kangyang.visual_collector import ExtractionRule
            task.rules = [
                ExtractionRule(
                    field_name=r["field_name"],
                    css_selector=r["css_selector"],
                    xpath=r["xpath"],
                    extract_type=r.get("extract_type", "text"),
                    attribute_name=r.get("attribute_name", ""),
                    sample_value=r.get("sample_value", ""),
                    is_list=r.get("is_list", False),
                    parent_index=r.get("parent_index", 0),
                )
                for r in v
            ]
        elif k == "pagination":
            task.pagination = v

    task.updated_at = datetime.now().isoformat()
    _save_task(task)
    return task


def delete_task(task_id: str) -> bool:
    """删除任务"""
    path = _task_path(task_id)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def mark_task_run(task_id: str):
    """标记任务已执行（更新 last_run 和 total_runs）"""
    path = _task_path(task_id)
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["last_run"] = datetime.now().isoformat()
        data["total_runs"] = data.get("total_runs", 0) + 1
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def duplicate_task(task_id: str, new_name: str = "") -> Optional[CollectionTask]:
    """复制任务"""
    task = get_task(task_id)
    if not task:
        return None
    new_task = create_task(
        name=new_name or f"{task.name} (副本)",
        target_url=task.target_url,
        description=task.description,
        route=task.route,
        login_required=task.login_required,
        login_host=task.login_host,
        rules=[r.__dict__ for r in task.rules],
        pagination=dict(task.pagination),
        export_format=task.export_format,
    )
    return new_task


def export_task(task_id: str) -> Optional[str]:
    """导出任务为 JSON 字符串（用于分享/备份）"""
    path = _task_path(task_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def import_task(json_str: str) -> Optional[CollectionTask]:
    """从 JSON 字符串导入任务"""
    try:
        data = json.loads(json_str)
        # 生成新的 task_id
        data["task_id"] = uuid.uuid4().hex[:12]
        data["created_at"] = datetime.now().isoformat()
        data["updated_at"] = data["created_at"]
        data["last_run"] = None
        data["total_runs"] = 0
        task = CollectionTask.from_dict(data)
        _save_task(task)
        return task
    except (json.JSONDecodeError, KeyError):
        return None


# ═══════════════════════════════════════════════════
# 内部辅助
# ═══════════════════════════════════════════════════


def _save_task(task: CollectionTask):
    """保存任务到磁盘"""
    path = _task_path(task.task_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(task.to_dict(), f, ensure_ascii=False, indent=2)


def get_tasks_dir_path() -> str:
    return _get_tasks_dir()
