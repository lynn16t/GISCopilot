"""
================================================================================
Agent Output Parser
================================================================================

用正则和启发式规则判断 LLM 输出的类型。
不依赖 LLM 自己打标签。

支持的输出类型：
  - CODE: Python 代码块
  - PLAN: 结构化执行计划（JSON）
  - QUESTION: AI 提问/请求澄清
  - KNOWLEDGE_UPDATE: 建议更新知识库
  - CHAT: 普通对话
"""

from enum import Enum
import json
import re
from typing import Optional, Dict, Any


class OutputType(Enum):
    """LLM 输出的类型。"""
    CODE = "code"
    PLAN = "plan"
    QUESTION = "question"
    KNOWLEDGE_UPDATE = "knowledge_update"
    CHAT = "chat"


class ParsedOutput:
    """解析后的 LLM 输出。"""

    def __init__(self, output_type: OutputType, raw_response: str, content: Any = None):
        """
        Args:
            output_type: 输出类型
            raw_response: LLM 的原始回复
            content: 根据类型不同的结构化内容：
                CODE → 提取出的 python 代码字符串
                PLAN → 解析出的 dict (structured plan)
                QUESTION → 原始文本
                KNOWLEDGE_UPDATE → 建议写入的知识条目文本
                CHAT → 原始文本
        """
        self.output_type = output_type
        self.raw_response = raw_response
        self.content = content


class AgentOutputParser:
    """
    用正则和启发式规则判断 LLM 输出的类型。
    不依赖 LLM 自己打标签。
    """

    def parse(self, response: str) -> ParsedOutput:
        """
        判断 LLM 回复属于哪种类型并提取内容。

        判断优先级（从高到低）：
          1. CODE：包含 ```python 代码块
          2. PLAN：包含 "steps" + "tool_id" 的 JSON 结构
          3. KNOWLEDGE_UPDATE：包含知识库相关关键词 + 建议语气
          4. QUESTION：以问号结尾 或 包含明确提问模式
          5. CHAT：以上都不是，视为普通聊天

        优先级的设计依据：
          - CODE 最高优先级：因为代码块内可能包含 JSON 或问号，
            但整体意图是执行代码
          - PLAN 高于 KNOWLEDGE_UPDATE：因为 plan 的 JSON 结构
            特征非常明确（"steps" + "tool_id"），不会误判
          - QUESTION 低于 KNOWLEDGE_UPDATE：因为 knowledge_update
            也可能带问号（"要不要加到知识库？"），但应该走知识库流程
          - CHAT 是兜底

        Args:
            response: LLM 的原始回复

        Returns:
            ParsedOutput 对象
        """

        # --- 1. CODE 检测 ---
        if self._is_code(response):
            code = self._extract_code(response)
            return ParsedOutput(OutputType.CODE, response, content=code)

        # --- 2. PLAN 检测（包含 plan update）---
        plan = self._try_parse_plan(response)
        if plan is not None:
            return ParsedOutput(OutputType.PLAN, response, content=plan)

        # --- 3. KNOWLEDGE_UPDATE 检测 ---
        if self._is_knowledge_update(response):
            knowledge_text = self._extract_knowledge_suggestion(response)
            return ParsedOutput(OutputType.KNOWLEDGE_UPDATE, response, content=knowledge_text)

        # --- 4. QUESTION 检测 ---
        if self._is_question(response):
            return ParsedOutput(OutputType.QUESTION, response, content=response)

        # --- 5. 兜底：CHAT ---
        return ParsedOutput(OutputType.CHAT, response, content=response)

    # ------------------------------------------------------------------
    # CODE 检测
    # ------------------------------------------------------------------

    def _is_code(self, response: str) -> bool:
        """检测是否包含 Python 代码块。"""
        response_lower = response.lower()
        # 检查是否包含 ```python 或者简单的 ``` 代码块
        # 简单的 ``` 也可能是代码块，需要进一步判断内容
        has_python_block = '```python' in response_lower
        has_code_block = '```' in response

        # 如果有 ```python 标记，肯定是代码
        if has_python_block:
            return True

        # 如果有 ``` 标记，检查内容是否像代码
        # （包含 import, def, class, = 等关键字）
        if has_code_block:
            # 提取代码块内容
            pattern = r'```\s*(.*?)\s*```'
            matches = re.findall(pattern, response, re.DOTALL)
            if matches:
                code_content = matches[0]
                # 检查是否包含代码特征
                code_indicators = ['import ', 'def ', 'class ', ' = ', 'processing.run']
                if any(indicator in code_content for indicator in code_indicators):
                    return True

        return False

    def _extract_code(self, response: str) -> str:
        """
        从回复中提取 Python 代码。

        Returns:
            提取出的代码字符串
        """
        # 首先尝试匹配带 python 标记的代码块
        pattern = r'```python\s*(.*?)\s*```'
        matches = re.findall(pattern, response, re.DOTALL | re.IGNORECASE)
        if matches:
            return matches[0].strip()

        # 退路：尝试不带语言标记的代码块
        pattern2 = r'```\s*(.*?)\s*```'
        matches2 = re.findall(pattern2, response, re.DOTALL)
        if matches2:
            return matches2[0].strip()

        # 最后退路：返回整个回复
        return response.strip()

    # ------------------------------------------------------------------
    # PLAN 检测
    # ------------------------------------------------------------------

    def _try_parse_plan(self, response: str) -> Optional[Dict]:
        """
        尝试从回复中解析结构化 plan JSON。

        判断条件：回复中同时包含 "steps" 和 "tool_id"，
        并且能成功解析为 JSON。

        这个条件足够严格，不会把普通包含这些词的文本误判为 plan。

        plan update（用户说"改参数"后 AI 输出的修改版）和首次 plan
        用完全相同的 JSON 结构，所以不需要区分。

        Returns:
            解析出的 plan dict，如果解析失败返回 None
        """
        if '"steps"' not in response or '"tool_id"' not in response:
            return None

        # 尝试提取 JSON 块
        text = response.strip()

        # 清理 markdown 代码块标记
        json_match = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
        if json_match:
            text = json_match.group(1)
        else:
            # 尝试找到 { 开头 } 结尾的 JSON
            brace_match = re.search(r'\{.*\}', text, re.DOTALL)
            if brace_match:
                text = brace_match.group(0)

        try:
            plan = json.loads(text.strip())
            # 验证是否是有效的 plan 结构
            if "steps" in plan and isinstance(plan["steps"], list):
                if len(plan["steps"]) > 0 and "tool_id" in plan["steps"][0]:
                    return plan
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            pass

        return None

    # ------------------------------------------------------------------
    # KNOWLEDGE_UPDATE 检测
    # ------------------------------------------------------------------

    def _is_knowledge_update(self, response: str) -> bool:
        """
        检测 AI 是否建议更新知识库。

        特征：同时满足以下条件
          - 包含知识库相关关键词
          - 包含建议/疑问语气

        Returns:
            True 如果检测到知识库更新建议
        """
        response_lower = response.lower()

        # 知识库相关关键词（中英文）
        knowledge_keywords = [
            '知识库', 'knowledge', '项目知识', 'project knowledge',
            '数据字典', 'data dictionary', '添加到笔记', 'add to notes',
            '记录下来', '保存这个规则'
        ]

        # 建议/疑问语气
        suggestion_patterns = [
            '要不要', '是否需要', '建议添加', '建议记录',
            'would you like', 'shall i add', 'should i save',
            'do you want'
        ]

        has_keyword = any(kw in response_lower for kw in knowledge_keywords)
        has_suggestion = any(sp in response_lower for sp in suggestion_patterns)

        return has_keyword and has_suggestion

    def _extract_knowledge_suggestion(self, response: str) -> str:
        """
        从回复中提取建议写入知识库的内容。

        简单实现：返回整个回复文本。
        用户确认后，由 UI 层决定如何写入知识库。

        Returns:
            建议的知识库内容
        """
        return response

    # ------------------------------------------------------------------
    # QUESTION 检测
    # ------------------------------------------------------------------

    def _is_question(self, response: str) -> bool:
        """
        检测 AI 是否在提问/请求澄清。

        特征（满足任一）：
          - 以中文问号结尾
          - 以英文问号结尾
          - 包含明确的提问模式

        Returns:
            True 如果检测到提问
        """
        stripped = response.strip()

        # 以问号结尾
        if stripped.endswith('?') or stripped.endswith('？'):
            return True

        # 明确的提问模式
        question_patterns = [
            '请问', '请告诉我', '能否提供', '你能说明',
            '请确认', '请指定', '需要你提供',
            'could you', 'can you clarify', 'please specify',
            'what do you mean', 'which one'
        ]
        response_lower = response.lower()
        return any(qp in response_lower for qp in question_patterns)
