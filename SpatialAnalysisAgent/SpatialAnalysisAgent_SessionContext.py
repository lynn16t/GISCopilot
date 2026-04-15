# -*- coding: utf-8 -*-
"""
SessionContext - Phase 3 统一上下文组装
统一管理 LLM 上下文，所有步骤通过 build_messages() 获取完整上下文。

设计原则：
  - 静态内容（规则 + 知识库）和动态内容（数据、plan、结果）分离
  - 静态部分跨调用不变，可命中 prompt cache
  - 所有 LLM 调用通过 build_messages() 获取上下文
"""

import os
import json
from typing import List, Dict, Any, Optional


class SessionContext:
    """会话上下文管理器 - Phase 3 版本"""

    # Token 预算（字符数，约 1:4 对应 token）
    MAX_CONTEXT_CHARS = 24000       # 单次调用硬上限 ~6K tokens
    STATIC_BUDGET = 8000            # 规则 + 知识库
    HISTORY_BUDGET = 8000           # 对话历史
    STEP_BUDGET = 8000              # 步骤指令 + 工具文档

    def __init__(self, knowledge_manager=None):
        """
        初始化会话上下文

        Args:
            knowledge_manager: KnowledgeManager 实例，用于获取项目知识库
        """
        # 五大数据字段
        self.messages: List[Dict[str, str]] = []        # 完整对话历史
        self.data_overview: List[str] = []              # 数据概览原始列表
        self.data_overview_str: str = ""                # 格式化后的数据概览字符串
        self.current_plan: str = ""                     # 最新一份 plan（完整保留）
        self.executed_codes: List[str] = []             # 已执行的代码列表
        self.results: List[str] = []                    # 执行结果列表（完整保留）

        self.task_name: str = ""                        # 任务名称
        self.current_task: str = ""                     # 用户原始任务描述

        # 外部引用
        self.knowledge_manager = knowledge_manager

        # 图层名称列表（用于知识检索）
        self._layer_names: List[str] = []

        # 跨任务保留的用户偏好（由 soft_reset 填充）
        self._preserved_context: str = ""

    # ========================
    # 重置与状态管理
    # ========================

    def reset(self):
        """完全重置（仅用于用户主动清空全部历史时）"""
        self.messages.clear()
        self.data_overview.clear()
        self.data_overview_str = ""
        self.current_plan = ""
        self.executed_codes.clear()
        self.results.clear()
        self.task_name = ""
        self.current_task = ""
        self._layer_names.clear()
        self._preserved_context = ""

    def soft_reset(self):
        """
        智能重置：保留用户约定的规则/偏好，清空技术性状态。

        调用时机：用户发起新 GIS 任务（通过 _run_via_agent / SEND_TASK），
        此时应保留用户在之前对话中约定的细则（如"距离单位用米"、
        "输出放到 D:/output"），但清除旧任务的 plan、code、results。

        保留的内容会作为 system 消息注入新会话的开头。
        """
        # 提取用户约定的偏好/规则
        preserved = self._extract_user_preferences()

        # 清空技术性状态
        self.messages.clear()
        self.data_overview.clear()
        self.data_overview_str = ""
        self.current_plan = ""
        self.executed_codes.clear()
        self.results.clear()
        self.task_name = ""
        self.current_task = ""
        self._layer_names.clear()

        # 把保留的偏好注入新会话开头
        self._preserved_context = preserved
        if preserved:
            self.messages.append({
                "role": "system",
                "content": f"=== User Preferences (from previous conversation) ===\n{preserved}"
            })

    def _extract_user_preferences(self) -> str:
        """
        从对话历史中提取用户约定的规则/偏好。

        策略：收集用户消息中包含偏好关键词的内容，
        以及 assistant 对这些偏好的确认回复。
        """
        if not self.messages:
            return ""

        preference_keywords = [
            # 中文
            '单位', '坐标系', '投影', '输出路径', '输出目录', '保存到',
            '默认', '规则', '总是', '每次都', '记住', '以后',
            '不要', '别用', '优先', '偏好', '习惯',
            # 英文
            'unit', 'crs', 'projection', 'output path', 'save to',
            'default', 'rule', 'always', 'remember', 'prefer',
            'never', "don't use", 'priority',
        ]

        preference_lines = []
        for msg in self.messages:
            if msg["role"] != "user":
                continue
            content_lower = msg["content"].lower()
            if any(kw in content_lower for kw in preference_keywords):
                # 只保留前200字符避免过长
                text = msg["content"][:200]
                preference_lines.append(text)

        if not preference_lines:
            return ""

        # 限制总长度
        combined = "\n".join(preference_lines)
        if len(combined) > 1000:
            combined = combined[:1000] + "..."
        return combined

    # ========================
    # 数据设置方法
    # ========================

    def set_task(self, task: str):
        """记录用户原始任务（不添加消息，由调用方统一添加）"""
        self.current_task = task

    def set_data_overview(self, data_overview: List[str] | str):
        """
        记录数据概览，缓存格式化字符串

        Args:
            data_overview: 数据概览列表或字符串
        """
        if isinstance(data_overview, str):
            self.data_overview_str = data_overview
            self.data_overview = data_overview.split('\n') if data_overview else []
        else:
            self.data_overview = data_overview
            self.data_overview_str = '\n'.join(
                [f"{idx + 1}. {line}" for idx, line in enumerate(data_overview)]
            )

        # 提取图层名称用于知识检索
        self._layer_names = []
        for item in self.data_overview:
            if item.strip():
                layer_name = os.path.splitext(os.path.basename(item))[0]
                self._layer_names.append(layer_name)

    def has_data_overview(self) -> bool:
        """检查是否已有数据概览"""
        return bool(self.data_overview_str)

    def set_plan(self, plan_text: str):
        """
        覆盖 current_plan（只保留最新一份）
        同时追加到 messages 以供历史回溯
        """
        self.current_plan = plan_text
        self.add_message("assistant", f"[Plan]\n{plan_text}")

    def add_executed_code(self, code: str):
        """追加到 executed_codes 列表，追加到 messages"""
        self.executed_codes.append(code)
        self.add_message("assistant", f"[Executed code]\n```python\n{code}\n```")

    def add_result(self, result: str):
        """追加到 results 列表（完整保留），追加到 messages"""
        self.results.append(result)
        self.add_message("assistant", f"[Result]\n{result}")

    def add_message(self, role: str, content: str):
        """追加一条消息到对话历史"""
        self.messages.append({
            "role": role,
            "content": content
        })

    # ========================
    # 核心方法：构建 messages
    # ========================

    def build_messages(
        self,
        step: str,
        step_instruction: str,
        step_role: str = ""
    ) -> List[Dict[str, str]]:
        """
        组装完整 messages 列表

        Args:
            step: 当前步骤标识 (query_tuning, tool_selection, code_generation, debug, etc.)
            step_instruction: 当前步骤指令
            step_role: 可选的步骤角色定义

        Returns:
            [
              {"role": "system", "content": 静态部分 + 动态部分},
              ...对话历史（压缩后）...,
              {"role": "user", "content": 当前步骤指令}
            ]
        """
        messages = []

        # 1. 构建 system message
        system_content = self._build_system_message(step, step_role)
        if system_content:
            messages.append({
                "role": "system",
                "content": system_content
            })

        # 2. 添加压缩后的对话历史
        history = self._get_compressed_history()
        messages.extend(history)

        # 3. 添加当前步骤指令
        messages.append({
            "role": "user",
            "content": step_instruction
        })

        return messages

    def _build_system_message(self, step: str, step_role: str = "") -> str:
        """
        组装 system message，分为静态部分和动态部分

        Phase 5: 支持 conversation 步骤，使用 CONVERSATION_SYSTEM_PROMPT

        === 静态部分（跨调用不变，可被 prompt cache 命中）===
        - 对话循环 prompt（仅 conversation 步骤）或步骤角色
        - 通用 GIS/QGIS 编码规则
        - 项目知识库相关条目

        === 动态部分 ===
        - 数据概览
        - 当前 plan（仅在特定步骤注入）
        - 最近执行结果（仅在特定步骤注入）

        两部分用 "---CONTEXT---" 标记分隔
        """
        parts = []

        # === 静态部分 ===
        static_parts = []

        # Phase 5: conversation 步骤使用统一 prompt
        if step == "conversation":
            try:
                from SpatialAnalysisAgent_Constants import CONVERSATION_SYSTEM_PROMPT
                static_parts.append(CONVERSATION_SYSTEM_PROMPT)
            except ImportError:
                # 回退：如果没有新 prompt，使用简单版本
                static_parts.append("You are a spatial analysis assistant.")
        elif step_role:
            # 其他步骤（code_generation, debug 等）仍可用步骤专属指令
            static_parts.append(step_role)

        # 通用 GIS/QGIS 规则
        static_parts.append(self._get_general_rules())

        # 项目知识库
        knowledge = self._get_knowledge_text()
        if knowledge:
            static_parts.append(f"=== Project Knowledge ===\n{knowledge}")

        if static_parts:
            parts.append("\n\n".join(static_parts))

        # === 动态部分 ===
        parts.append("---CONTEXT---")

        dynamic_parts = []

        # 数据概览（code_review 不注入，因为 step_instruction 里已经有 data_path）
        if self.data_overview_str and step != "code_review":
            dynamic_parts.append(f"=== Loaded Data ===\n{self.data_overview_str}")

        # current_plan 注入：conversation / code_generation / code_review / debug 等
        if step in ["conversation", "code_generation", "code_review",
                    "debug", "plan_revision", "chat"] and self.current_plan:
            dynamic_parts.append(f"=== Current Plan ===\n{self.current_plan}")

        if step in ["conversation", "debug", "chat",
                    "plan_revision"] and self.results:
            # 保留最近 3 条结果
            recent_results = self.results[-3:]
            results_text = "\n\n".join(recent_results)
            dynamic_parts.append(f"=== Recent Execution Results ===\n{results_text}")

        if dynamic_parts:
            parts.append("\n\n".join(dynamic_parts))

        return "\n\n".join(parts)

    def _get_general_rules(self) -> str:
        """获取通用 GIS/QGIS 编码规则"""
        return """=== General GIS/QGIS Rules ===
- Always use processing.run() for QGIS algorithms
- Import format: from qgis.core import *, from qgis import processing
- Use proper CRS handling and coordinate transformations
- Handle layer paths as absolute paths
- Check layer validity before processing
- Use proper error handling for GIS operations"""

    def _get_knowledge_text(self) -> str:
        """从 knowledge_manager 获取与当前图层/任务相关的知识文本"""
        if not self.knowledge_manager or not self.knowledge_manager.is_ready:
            return ""

        try:
            knowledge = self.knowledge_manager.get_relevant_knowledge(
                layer_names=self._layer_names,
                query=self.current_task,
                max_chars=6000  # 控制知识库大小
            )
            return knowledge
        except Exception as e:
            print(f"[SessionContext] Warning: Failed to retrieve knowledge: {e}")
            return ""

    def _get_compressed_history(self) -> List[Dict[str, str]]:
        """
        对话历史压缩策略：
        - 第一条消息（原始任务）：始终保留
        - 最近 3 轮交互（6 条消息）：完整保留
        - 中间消息的压缩规则：
          · [Plan] 标记的消息：删除（因为 current_plan 只保留最新的）
          · [Result] 标记的消息：完整保留，不截断
          · [Executed code] 标记的消息：只保留标记 "[Code was executed]"，删除代码体
          · 普通消息：超过 200 字符则截断
        """
        if not self.messages:
            return []

        compressed = []

        # 1. 第一条消息始终保留（原始任务）
        if len(self.messages) > 0:
            compressed.append(self.messages[0])

        # 2. 最近 3 轮 = 6 条消息
        recent_count = min(6, len(self.messages))
        recent_messages = self.messages[-recent_count:] if recent_count > 0 else []

        # 3. 中间消息需要压缩
        middle_messages = self.messages[1:-recent_count] if len(self.messages) > recent_count + 1 else []

        for msg in middle_messages:
            content = msg["content"]

            # [Plan] 消息：删除（旧 plan 无用）
            if content.startswith("[Plan]"):
                continue

            # [Result] 消息：完整保留
            elif content.startswith("[Result]"):
                compressed.append(msg)

            # [Executed code] 消息：只保留标记
            elif content.startswith("[Executed code]"):
                compressed.append({
                    "role": msg["role"],
                    "content": "[Code was executed]"
                })

            # 普通消息：截断
            else:
                if len(content) > 200:
                    compressed.append({
                        "role": msg["role"],
                        "content": content[:200] + "..."
                    })
                else:
                    compressed.append(msg)

        # 4. 添加最近消息（完整保留）
        compressed.extend(recent_messages)

        return compressed

    # ========================
    # 调试与状态查询
    # ========================

    def get_summary(self) -> Dict[str, Any]:
        """返回当前会话状态摘要"""
        return {
            "message_count": len(self.messages),
            "has_data_overview": self.has_data_overview(),
            "has_plan": bool(self.current_plan),
            "execution_count": len(self.executed_codes),
            "result_count": len(self.results),
            "task": self.current_task,
        }

    def __repr__(self):
        summary = self.get_summary()
        return (
            f"SessionContext("
            f"messages={summary['message_count']}, "
            f"executions={summary['execution_count']}, "
            f"results={summary['result_count']})"
        )
