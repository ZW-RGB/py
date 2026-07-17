# -*- coding: utf-8 -*-
"""
字段校验器

职责：
  1. 校验 JSON 字段完整性（必填字段是否齐全）
  2. 校验数据类型是否正确（string / date / list / int）
  3. 记录校验失败的详细信息供 Pipeline 决策
"""

import re
import logging
from datetime import datetime

from kangyang.llm.schema import schema_manager
from kangyang.llm.config_loader import get_validation_config

logger = logging.getLogger(__name__)


class FieldValidator:
    """Pipeline 层字段校验器"""

    def __init__(self):
        self.validation_config = get_validation_config()
        self.strict_mode = self.validation_config.get("strict_mode", False)
        self.global_required = self.validation_config.get("global_required", [])

    def validate(self, data: dict, schema_name: str) -> tuple:
        """
        校验解析数据

        Args:
            data: LLM 解析返回的 dict
            schema_name: Schema 文件名

        Returns:
            (is_valid: bool, errors: list[str])
        """
        fields = schema_manager.get_fields(schema_name)
        errors = []

        # 1. 必填字段检查
        for field_def in fields:
            field_name = field_def["name"]
            is_required = field_def.get("required", False)

            # 全局必填字段也纳入检查
            if field_name in self.global_required:
                is_required = True

            if is_required:
                value = data.get(field_name)
                if not self._field_present(value):
                    errors.append(f"必填字段缺失或为空: '{field_name}'")

        # 2. 类型检查
        for field_def in fields:
            field_name = field_def["name"]
            expected_type = field_def.get("type", "string")
            value = data.get(field_name)

            # 跳过空值（由必填检查处理，或本身可选）
            if not self._field_present(value):
                continue

            type_check_result = self._check_type(value, expected_type, field_name)
            if type_check_result:
                errors.append(type_check_result)

        # 3. 数据清洗：标准化日期格式、去除字符串首尾空格
        for field_def in fields:
            field_name = field_def["name"]
            expected_type = field_def.get("type", "string")
            value = data.get(field_name)

            if expected_type == "date" and isinstance(value, str) and value.strip():
                data[field_name] = self._normalize_date(value)
            elif expected_type == "string" and isinstance(value, str):
                data[field_name] = value.strip()

        return len(errors) == 0, errors

    @staticmethod
    def _field_present(value) -> bool:
        """判断字段是否有有效值"""
        if value is None:
            return False
        if isinstance(value, str) and value.strip() == "":
            return False
        if isinstance(value, (list, dict)) and len(value) == 0:
            return False
        return True

    @staticmethod
    def _check_type(value, expected_type: str, field_name: str) -> str:
        """检查单个字段类型，返回错误信息或空字符串"""
        if expected_type == "string":
            if not isinstance(value, str):
                return f"字段 '{field_name}' 需要字符串类型，实际: {type(value).__name__}"
        elif expected_type == "int":
            if not isinstance(value, int) and not (isinstance(value, str) and value.isdigit()):
                return f"字段 '{field_name}' 需要整数类型，实际值: {value}"
        elif expected_type == "float":
            if not isinstance(value, (int, float)):
                return f"字段 '{field_name}' 需要数字类型，实际: {type(value).__name__}"
        elif expected_type == "list":
            if not isinstance(value, list):
                return f"字段 '{field_name}' 需要列表类型，实际: {type(value).__name__}"
        elif expected_type == "date":
            if not isinstance(value, str):
                return f"字段 '{field_name}' 应该是日期字符串"
            if not re.match(r"\d{4}[./-]\d{1,2}[./-]\d{1,2}", value.strip()):
                return f"字段 '{field_name}' 日期格式不正确: {value}"
        elif expected_type == "bool":
            if not isinstance(value, bool):
                return f"字段 '{field_name}' 需要布尔类型，实际: {type(value).__name__}"

        return ""

    @staticmethod
    def _normalize_date(value: str) -> str:
        """统一日期格式为 YYYY-MM-DD"""
        value = value.strip()
        # 尝试解析常见格式
        for fmt in [
            "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
            "%Y年%m月%d日", "%m/%d/%Y", "%d/%m/%Y",
        ]:
            try:
                dt = datetime.strptime(value, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return value  # 无法解析则保持原样
