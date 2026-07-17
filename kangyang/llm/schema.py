# -*- coding: utf-8 -*-
"""Schema 管理器 —— 加载和解析字段定义"""

import os
import json
from kangyang.llm.config_loader import get_config_dir


class SchemaManager:
    """管理采集字段 Schema 的加载与查询"""

    def __init__(self):
        self._schemas = {}  # 缓存已加载的 schema

    def load(self, schema_name: str) -> dict:
        """加载指定名称的 Schema 定义"""
        if schema_name in self._schemas:
            return self._schemas[schema_name]

        schema_path = os.path.join(get_config_dir(), "schemas", schema_name)
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)

        self._schemas[schema_name] = schema
        return schema

    def get_fields(self, schema_name: str) -> list:
        """获取字段列表"""
        return self.load(schema_name).get("fields", [])

    def get_required_fields(self, schema_name: str) -> list:
        """获取必填字段名列表"""
        return [f["name"] for f in self.get_fields(schema_name) if f.get("required")]

    def get_field_names(self, schema_name: str) -> list:
        """获取所有字段名"""
        return [f["name"] for f in self.get_fields(schema_name)]

    def build_schema_prompt_text(self, schema_name: str) -> str:
        """
        将 Schema 转为 LLM 友好的文本描述，嵌入 Prompt

        示例输出：
          字段列表：
          - title (string, 必填): 政策标题
          - publish_date (date, 必填): 发布日期，格式YYYY-MM-DD
          ...
        """
        fields = self.get_fields(schema_name)
        lines = ["请从网页中提取以下字段，以 JSON 格式返回："]
        for f in fields:
            req_flag = "必填" if f.get("required") else "可选"
            desc = f.get("description", "")
            type_info = f.get("type", "string")
            lines.append(f"  - {f['name']} ({type_info}, {req_flag}): {desc}")

        lines.append("")
        lines.append("要求：")
        lines.append("1. 只返回合法的 JSON 对象，不要包含任何解释性文字")
        lines.append("2. 必填字段如果确实无法找到，请填充空字符串 \"\"")
        lines.append("3. 日期字段统一格式为 YYYY-MM-DD")
        return "\n".join(lines)


# 全局单例
schema_manager = SchemaManager()
