# -*- coding: utf-8 -*-
"""Few-shot 示例管理器 —— 提供稳定格式的示例对"""

import os
import json
from kangyang.llm.config_loader import get_config_dir


class FewshotManager:
    """管理 Few-shot 示例的加载与 Prompt 嵌入"""

    def __init__(self):
        self._examples = {}

    def load(self, fewshot_name: str) -> dict:
        """加载指定名称的 Few-shot 示例文件"""
        if fewshot_name in self._examples:
            return self._examples[fewshot_name]

        fewshot_path = os.path.join(get_config_dir(), "fewshots", fewshot_name)
        with open(fewshot_path, "r", encoding="utf-8") as f:
            examples = json.load(f)

        self._examples[fewshot_name] = examples
        return examples

    def get_examples(self, fewshot_name: str) -> list:
        """获取示例列表（每个示例为 dict）"""
        return self.load(fewshot_name).get("examples", [])

    def build_fewshot_text(self, fewshot_name: str, max_tokens_estimate: int = 3000) -> str:
        """
        将 Few-shot 示例转为 Prompt 中的示例文本

        每个示例格式：
          示例 N：
          网页片段：
          ...
          正确输出：
          {...}
        """
        examples = self.get_examples(fewshot_name)
        lines = ["以下是几个正确提取的示例，请参照这些示例的格式输出：", ""]

        for i, ex in enumerate(examples, 1):
            lines.append(f"--- 示例 {i} ---")
            # 截断过长的 HTML 片段
            snippet = ex.get("html_snippet", "")
            if len(snippet) > 2000:
                snippet = snippet[:2000] + "\n... (内容过长，已截断)"
            lines.append(f"网页片段：\n{snippet}")
            lines.append(f"正确输出：\n{json.dumps(ex['output'], ensure_ascii=False, indent=2)}")
            lines.append("")

        return "\n".join(lines)


# 全局单例
fewshot_manager = FewshotManager()
