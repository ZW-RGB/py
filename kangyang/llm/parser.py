# -*- coding: utf-8 -*-
"""
LLM 解析器 —— 整个系统的核心

工作流程：
  HTML 原文 + Schema 定义 + Few-shot 示例
    → PromptBuilder 组装 Prompt
    → LangChain 调用 Qwen2.5 API
    → 解析 JSON 响应
    → 字段缺失时自动重构 Prompt 并重试（最多 2 次）
    → 仍失败则标记异常

Mock 模式（环境变量 LLM_MOCK=1 或 use_mock=True）：
  跳过真实 API 调用，用基于 HTML 文本的规则解析兜底，
  用于本地测试/演示/CI 环境。
"""

import os
import json
import re
import logging
from datetime import datetime

from kangyang.llm.config_loader import get_llm_config, get_task_config
from kangyang.llm.schema import schema_manager
from kangyang.llm.fewshot import fewshot_manager
from kangyang.llm.prompt import PromptBuilder

logger = logging.getLogger(__name__)


class MockLLM:
    """
    Mock LLM —— 不调用真实 API，用简单规则从 HTML 中提取字段
    仅用于演示/测试，体现数据流完整通路
    """

    def parse_html(self, html: str) -> dict:
        """从 HTML 中用规则提取基本字段"""
        from html.parser import HTMLParser

        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.texts = []
                self._skip = False

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "noscript"):
                    self._skip = True

            def handle_endtag(self, tag):
                if tag in ("script", "style", "noscript"):
                    self._skip = False

            def handle_data(self, data):
                if not self._skip:
                    text = data.strip()
                    if text:
                        self.texts.append(text)

        extractor = TextExtractor()
        extractor.feed(html)
        full_text = " ".join(extractor.texts)

        # 提取标题：优先 <title>，其次 <h1>
        title = ""
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
        if title_match:
            title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
        if not title:
            h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL | re.IGNORECASE)
            if h1_match:
                title = re.sub(r"<[^>]+>", "", h1_match.group(1)).strip()
        if not title:
            # 截取第一行有意义的文本
            for t in extractor.texts:
                if len(t) > 5:
                    title = t[:80]
                    break

        # 提取日期：匹配常见日期模式
        publish_date = ""
        date_patterns = [
            r"(\d{4})[年\-/](\d{1,2})[月\-/](\d{1,2})[日号]?",
            r"(\d{4})\.(\d{1,2})\.(\d{1,2})",
        ]
        for pat in date_patterns:
            m = re.search(pat, full_text)
            if m:
                y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
                publish_date = f"{y}-{mo}-{d}"
                break

        # 提取文号：匹配常见公文文号格式
        doc_number = ""
        doc_match = re.search(r"[（(][^）)]{2,30}[〔\[]\d{4}[〕\]]\d+号[）)]", full_text)
        if doc_match:
            doc_number = doc_match.group(0)

        # 提取摘要：取正文前 200 字
        summary = full_text[:200].strip() if full_text else ""

        # URL 直接使用（pipeline 层会注入）
        return {
            "title": title,
            "publish_date": publish_date,
            "doc_number": doc_number,
            "issuing_authority": "",
            "category": "康养政策",
            "summary": summary,
            "keywords": [],
            "source_url": "",
        }


class LLMParser:
    """
    大模型语义解析器

    用法:
        # 真实模式
        parser = LLMParser()
        # Mock 模式（无 API 时）
        parser = LLMParser(use_mock=True)

        result = parser.parse(html="<html>...</html>", schema_name="policy.json")
        # result = {"status": "success", "data": {...}, "retry_count": 0, ...}
    """

    MAX_RETRIES = 2  # 字段缺失时最多重试次数

    def __init__(self, use_mock: bool = False):
        llm_config = get_llm_config()
        task_config = get_task_config()

        # 检查是否使用 Mock
        self.use_mock = use_mock or os.environ.get("LLM_MOCK", "0") == "1"

        if self.use_mock:
            self.llm = None
            self._mock = MockLLM()
            logger.info("LLM 解析器：使用 Mock 模式（规则提取，仅供测试）")
        else:
            from langchain_openai import ChatOpenAI
            from langchain_core.messages import SystemMessage, HumanMessage
            self._SystemMessage = SystemMessage
            self._HumanMessage = HumanMessage

            # 初始化 LangChain ChatOpenAI 客户端（Qwen2.5 使用 OpenAI 兼容接口）
            self.llm = ChatOpenAI(
                base_url=llm_config.get("api_base", "http://localhost:8000/v1"),
                api_key=llm_config.get("api_key", "EMPTY"),
                model=llm_config.get("model", "qwen2.5-7b-instruct"),
                temperature=llm_config.get("temperature", 0.1),
                max_tokens=llm_config.get("max_tokens", 4096),
                timeout=llm_config.get("request_timeout", 120),
            )
            logger.info(
                f"LLM 解析器：使用真实 API {llm_config.get('api_base')} / {llm_config.get('model')}"
            )

        self.max_retries = llm_config.get("max_retries", self.MAX_RETRIES)
        self.prompt_builder = PromptBuilder()

        # 默认 schema 和 fewshot
        self.default_schema = task_config.get("default_schema", "policy.json")
        self.default_fewshot = task_config.get("default_fewshot", "policy_fewshot.json")

    def parse(self, html: str, schema_name: str = None, fewshot_name: str = None) -> dict:
        """
        解析 HTML，返回结构化数据

        Returns:
            {
                "status": "success" | "failed",
                "data": {...},
                "error": "",
                "retry_count": 0,
                "raw_response": ""
            }
        """
        schema_name = schema_name or self.default_schema
        fewshot_name = fewshot_name or self.default_fewshot

        # ---- Mock 模式 ----
        if self.use_mock:
            data = self._mock.parse_html(html)
            return {
                "status": "success",
                "data": data,
                "error": "",
                "retry_count": 0,
                "raw_response": "[MOCK] 规则提取，未调用真实 LLM",
            }

        # ---- 真实 LLM 模式 ----
        schema_text = schema_manager.build_schema_prompt_text(schema_name)
        fewshot_text = fewshot_manager.build_fewshot_text(fewshot_name)

        retry_count = 0
        last_error = None
        raw_response = ""

        while retry_count <= self.max_retries:
            try:
                if retry_count == 0:
                    messages = self.prompt_builder.build(html, schema_text, fewshot_text)
                else:
                    messages = self.prompt_builder.build_retry_prompt(
                        html, schema_text, fewshot_text, last_error
                    )

                raw_response = self._call_llm(messages)
                data = self._extract_json(raw_response)
                valid, errors = self._validate_against_schema(data, schema_name)

                if valid:
                    return {
                        "status": "success",
                        "data": data,
                        "error": "",
                        "retry_count": retry_count,
                        "raw_response": raw_response,
                    }
                else:
                    last_error = "; ".join(errors)
                    logger.warning(f"字段校验不通过（第{retry_count + 1}次尝试）: {last_error}")

            except Exception as e:
                last_error = f"LLM 调用异常: {str(e)}"
                logger.error(f"LLM 调用失败（第{retry_count + 1}次尝试）: {e}")

            retry_count += 1

        return {
            "status": "failed",
            "data": {},
            "error": last_error or "未知错误",
            "retry_count": retry_count - 1,
            "raw_response": raw_response,
        }

    def _call_llm(self, messages: list) -> str:
        lc_messages = []
        for role, content in messages:
            if role == "system":
                lc_messages.append(self._SystemMessage(content=content))
            elif role == "user":
                lc_messages.append(self._HumanMessage(content=content))

        response = self.llm.invoke(lc_messages)
        return response.content

    def _extract_json(self, text: str) -> dict:
        text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        code_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if code_block_match:
            try:
                return json.loads(code_block_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        raise ValueError(f"无法从 LLM 响应中提取有效 JSON: {text[:500]}")

    def _validate_against_schema(self, data: dict, schema_name: str) -> tuple:
        required_fields = schema_manager.get_required_fields(schema_name)
        all_fields = {f["name"]: f for f in schema_manager.get_fields(schema_name)}
        errors = []

        for field_name in required_fields:
            value = data.get(field_name, "")
            if value is None or (isinstance(value, str) and value.strip() == ""):
                errors.append(f"必填字段 '{field_name}' 缺失或为空")

        for field_name in all_fields:
            expected_type = all_fields[field_name].get("type", "string")
            value = data.get(field_name)
            if value is None or value == "":
                continue
            if expected_type == "date":
                if not isinstance(value, str) or not self._is_date_like(value):
                    errors.append(f"字段 '{field_name}' 需要日期格式，当前值: {value}")
            elif expected_type == "list":
                if not isinstance(value, list):
                    errors.append(f"字段 '{field_name}' 需要列表类型，当前值: {type(value).__name__}")

        return len(errors) == 0, errors

    @staticmethod
    def _is_date_like(value: str) -> bool:
        return bool(re.match(r"\d{4}[./-]\d{1,2}[./-]\d{1,2}", value.strip()))
