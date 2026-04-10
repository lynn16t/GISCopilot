"""
================================================================================
Guard Gate
================================================================================

根据输出类型决定如何处理 LLM 的回复。

本模块不直接操作 UI，而是返回 Action 对象，
由调用方（AgentController 或对话循环）根据 Action 执行。
"""

from typing import Dict, Any
from SpatialAnalysisAgent_OutputParser import OutputType, ParsedOutput


class GuardGate:
    """
    根据输出类型决定如何处理 LLM 的回复。

    本类不直接操作 UI，而是返回 Action 对象，
    由调用方（AgentController 或对话循环）根据 Action 执行。
    """

    class Action:
        """风控门的处理决策。"""

        def __init__(self, action_type: str, **kwargs):
            """
            Args:
                action_type: 处理决策类型，可选值：
                    "show_code"         → 代码发到 CodeEditor，聊天框显示反馈
                    "confirm_plan"      → 展示 plan，等待用户确认
                    "trigger_analysis"  → 用户确认任务，触发完整分析流水线
                    "confirm_knowledge" → 轻量确认，确认后写入知识库
                    "show_message"      → 直接在聊天框显示
                **kwargs: 根据不同 action_type 附带的数据
            """
            self.action_type = action_type
            self.data = kwargs

    def decide(self, parsed: ParsedOutput) -> Action:
        """
        根据解析结果返回处理决策。

        Args:
            parsed: OutputParser 解析后的结果

        Returns:
            Action 对象，包含处理决策和相关数据
        """

        if parsed.output_type == OutputType.CODE:
            return self.Action(
                "show_code",
                code=parsed.content,
                feedback_message="Python code generated. Check the Generated Code tab.",
            )

        elif parsed.output_type == OutputType.PLAN:
            return self.Action(
                "confirm_plan",
                plan=parsed.content,
                plan_text=self._format_plan_for_display(parsed.content),
            )

        elif parsed.output_type == OutputType.GIS_TASK_READY:
            # 用户确认任务，提取精炼描述，准备触发分析流水线
            refined_task = parsed.content
            # 从原始回复中提取 [TASK_CONFIRMED] 之前的确认消息（如有）
            raw = parsed.raw_response
            tag_idx = raw.find('[TASK_CONFIRMED]')
            confirmation_message = raw[:tag_idx].strip() if tag_idx > 0 else ""
            return self.Action(
                "trigger_analysis",
                refined_task=refined_task,
                confirmation_message=confirmation_message,
            )

        elif parsed.output_type == OutputType.KNOWLEDGE_UPDATE:
            return self.Action(
                "confirm_knowledge",
                suggestion=parsed.content,
                confirm_message="Add this to project knowledge?",
            )

        elif parsed.output_type == OutputType.QUESTION:
            return self.Action(
                "show_message",
                message=parsed.content,
                is_question=True,
            )

        else:  # CHAT
            return self.Action(
                "show_message",
                message=parsed.content,
                is_question=False,
            )

    def _format_plan_for_display(self, plan: Dict[str, Any]) -> str:
        """
        将结构化 plan JSON 格式化为用户可读的文本。

        Args:
            plan: 结构化执行计划字典

        Returns:
            格式化后的可读文本
        """
        lines = []
        lines.append("=== Execution Plan ===\n")

        for step in plan.get("steps", []):
            step_num = step.get("step_number", "?")
            operation = step.get("operation", "")
            tool_id = step.get("tool_id", "")
            input_layer = step.get("input_layer", "")
            params = step.get("key_parameters", {})
            output_desc = step.get("output_description", "")

            lines.append(f"Step {step_num}: {operation}")
            lines.append(f"  Tool: {tool_id}")
            if input_layer:
                lines.append(f"  Input: {input_layer}")
            if params:
                for k, v in params.items():
                    lines.append(f"  {k}: {v}")
            if output_desc:
                lines.append(f"  Output: {output_desc}")
            lines.append("")

        return "\n".join(lines)
