# -*- coding: utf-8 -*-
"""
Prompt 构建器

核心 Prompt 结构：
  System: 角色与任务说明
  1. Schema 字段定义
  2. Few-shot 示例
  3. 当前待解析的 HTML（截断后）
"""

from kangyang.llm.config_loader import get_task_config


class PromptBuilder:
    """组装发送给 LLM 的完整 Prompt"""

    SYSTEM_TEMPLATE = """你是一个专业的数据提取助手。你的任务是从给定的网页HTML内容中，提取指定字段的结构化信息。

规则：
1. 仔细阅读HTML内容，理解页面结构，找出目标字段的值
2. 严格按照字段定义的类型和格式要求提取
3. 只返回合法的JSON对象，不要包含任何markdown标记或解释文字
4. 如果某个字段在页面中找不到对应信息，必填字段填空字符串""，可选字段也填空字符串""
5. 日期字段统一转换为 YYYY-MM-DD 格式"""

    USER_TEMPLATE = """{schema_text}

{fewshot_text}

══════════ 以下是待提取的网页内容 ══════════

{html}

══════════ 请提取并返回 JSON ══════════"""

    def __init__(self):
        self._task_config = get_task_config()
        self._html_max_len = self._task_config.get("html_max_length", 30000)

    def build(
        self,
        html: str,
        schema_text: str,
        fewshot_text: str = "",
    ) -> list:
        """
        构建发给 LLM 的 messages 列表
        
        Args:
            html: 网页原始 HTML
            schema_text: Schema 定义文本
            fewshot_text: Few-shot 示例文本
        
        Returns:
            LangChain 兼容的 messages 列表
        """
        # 截断过长 HTML（避免超出 token 限制）
        truncated_html = self._truncate_html(html)

        user_content = self.USER_TEMPLATE.format(
            schema_text=schema_text,
            fewshot_text=fewshot_text or "（无示例）",
            html=truncated_html,
        )

        return [
            ("system", self.SYSTEM_TEMPLATE),
            ("user", user_content),
        ]

    def build_retry_prompt(
        self,
        html: str,
        schema_text: str,
        fewshot_text: str,
        previous_error: str,
    ) -> list:
        """
        构建重试 Prompt —— 在原有基础上附加错误提示，引导 LLM 修正

        Args:
            previous_error: 上一次解析失败的原因描述
        """
        user_content = self.USER_TEMPLATE.format(
            schema_text=schema_text,
            fewshot_text=fewshot_text or "（无示例）",
            html=self._truncate_html(html),
        )

        # 附加错误修正指令
        retry_instruction = f"""注意：上一次提取的结果存在问题，请修正：
{previous_error}

请务必确保：
1. 所有必填字段都已填写
2. 字段类型和格式正确
3. JSON 结构完整有效"""

        return [
            ("system", self.SYSTEM_TEMPLATE),
            ("user", user_content),
            ("user", retry_instruction),
        ]

    def _truncate_html(self, html: str) -> str:
        """智能截断 HTML：优先保留 <head> 和可见文本密集区域"""
        if len(html) <= self._html_max_len:
            return html

        # 策略：头部 20% + 尾部 80%（因为重要信息通常在 body 中）
        head_part = html[: int(self._html_max_len * 0.2)]
        tail_part = html[-int(self._html_max_len * 0.8):]

        return (
            f"{head_part}\n\n"
            f"<!-- 中间部分已截断，共省略 {len(html) - self._html_max_len} 字符 -->\n\n"
            f"{tail_part}"
        )
