# -*- coding: utf-8 -*-
"""
SessionContext - 会话记忆模块
SpatialAnalysisAgent 的会话状态管理，负责在多轮交互中维护上下文。

职责：
    1. 存储对话历史、数据概览、技术方案、已执行代码和执行结果
    2. 构建发给 LLM 的上下文（get_context）
    3. 在对话历史过长时自动压缩早期历史

设计原则：
    - 纯数据容器，不依赖 GUI、状态机或 helper
    - 所有方法可独立测试
    - 接口稳定，将来接入 AgentController 时无需修改
"""

import time
import copy
from datetime import datetime
from typing import List, Dict, Optional, Any


class LayerSnapshot:
    """QGIS 图层快照，记录某一时刻的图层状态"""

    def __init__(self):
        self.layers: Dict[str, Dict[str, Any]] = {}
        # 格式: { layer_id: { "name": str, "type": str, "source": str, "feature_count": int, "crs": str, "fields": [...] } }

    def capture(self):
        """
        从当前 QGIS 项目捕获所有图层的快照。
        在 QGIS 环境外调用时安全返回空快照。
        """
        try:
            from qgis.core import QgsProject, QgsVectorLayer, QgsRasterLayer
            project = QgsProject.instance()

            for layer_id, layer in project.mapLayers().items():
                layer_info = {
                    "name": layer.name(),
                    "source": layer.source(),
                    "crs": layer.crs().authid() if layer.crs() else "unknown",
                }

                if isinstance(layer, QgsVectorLayer):
                    layer_info["type"] = "vector"
                    layer_info["feature_count"] = layer.featureCount()
                    layer_info["fields"] = [
                        {"name": f.name(), "type": f.typeName()}
                        for f in layer.fields()
                    ]
                elif isinstance(layer, QgsRasterLayer):
                    layer_info["type"] = "raster"
                    layer_info["band_count"] = layer.bandCount()
                    layer_info["width"] = layer.width()
                    layer_info["height"] = layer.height()
                else:
                    layer_info["type"] = "other"

                self.layers[layer_id] = layer_info

        except ImportError:
            # 非 QGIS 环境（单元测试等），安全跳过
            pass
        except Exception as e:
            print(f"[SessionContext] Warning: Failed to capture layer snapshot: {e}")

    def diff(self, previous: 'LayerSnapshot') -> Dict[str, Any]:
        """
        与之前的快照做对比，返回新增和变化的图层信息。

        Returns:
            {
                "added": [ { layer_id, name, type, fields, ... }, ... ],
                "removed": [ { layer_id, name }, ... ],
                "modified": [ { layer_id, name, changes: str }, ... ]
            }
        """
        result = {"added": [], "removed": [], "modified": []}

        prev_ids = set(previous.layers.keys())
        curr_ids = set(self.layers.keys())

        # 新增的图层
        for layer_id in curr_ids - prev_ids:
            result["added"].append({
                "layer_id": layer_id,
                **self.layers[layer_id]
            })

        # 删除的图层
        for layer_id in prev_ids - curr_ids:
            result["removed"].append({
                "layer_id": layer_id,
                "name": previous.layers[layer_id].get("name", "unknown")
            })

        # 可能修改的图层（feature_count 变化等）
        for layer_id in curr_ids & prev_ids:
            curr = self.layers[layer_id]
            prev = previous.layers[layer_id]
            changes = []

            if curr.get("feature_count") != prev.get("feature_count"):
                changes.append(
                    f"feature_count: {prev.get('feature_count')} -> {curr.get('feature_count')}"
                )
            if curr.get("fields") != prev.get("fields"):
                changes.append("fields changed")

            if changes:
                result["modified"].append({
                    "layer_id": layer_id,
                    "name": curr.get("name", "unknown"),
                    "changes": "; ".join(changes)
                })

        return result


class ExecutionRecord:
    """单次代码执行的完整记录"""

    def __init__(self, code: str, success: bool, output: str,
                 error_message: str = "",
                 data_changes: Optional[Dict[str, Any]] = None,
                 timestamp: Optional[str] = None):
        self.code = code
        self.success = success
        self.output = output
        self.error_message = error_message
        self.data_changes = data_changes or {"added": [], "removed": [], "modified": []}
        self.timestamp = timestamp or datetime.now().isoformat()

    def to_context_str(self) -> str:
        """将执行记录转换为可注入 LLM 上下文的字符串"""
        status = "SUCCESS" if self.success else "FAILED"
        parts = [f"[Execution - {status} - {self.timestamp}]"]
        parts.append(f"Code:\n```python\n{self.code}\n```")

        if self.output.strip():
            # 截断过长的输出
            output = self.output.strip()
            if len(output) > 2000:
                output = output[:1000] + "\n... (truncated) ...\n" + output[-500:]
            parts.append(f"Output:\n{output}")

        if self.error_message:
            parts.append(f"Error: {self.error_message}")

        # 数据变化信息
        if self.data_changes.get("added"):
            added_strs = []
            for layer in self.data_changes["added"]:
                layer_desc = f"  - {layer.get('name', 'unknown')} ({layer.get('type', 'unknown')})"
                if layer.get("fields"):
                    field_names = [f["name"] for f in layer["fields"][:10]]  # 最多显示10个字段
                    layer_desc += f", fields: {field_names}"
                if layer.get("feature_count") is not None:
                    layer_desc += f", {layer['feature_count']} features"
                if layer.get("crs"):
                    layer_desc += f", CRS: {layer['crs']}"
                added_strs.append(layer_desc)
            parts.append("New layers created:\n" + "\n".join(added_strs))

        if self.data_changes.get("removed"):
            removed_names = [l.get("name", "unknown") for l in self.data_changes["removed"]]
            parts.append(f"Layers removed: {removed_names}")

        if self.data_changes.get("modified"):
            mod_strs = [
                f"  - {l.get('name', 'unknown')}: {l.get('changes', '')}"
                for l in self.data_changes["modified"]
            ]
            parts.append("Layers modified:\n" + "\n".join(mod_strs))

        return "\n".join(parts)


class SessionContext:
    """
    会话上下文管理器。

    持有单次会话的完整状态，贯穿多轮交互。
    每次 LLM 调用前通过 get_context() 获取需要注入的上下文信息。

    Usage:
        session = SessionContext()

        # 用户发了一条消息
        session.add_message("user", "帮我从南京市数据中提取栖霞区")

        # AI 回复
        session.add_message("assistant", "我计划使用 extractbyattribute 工具...")

        # 存数据概览（只需做一次）
        session.data_overview = "Layer: nanjing_districts, fields: [name, area, population], CRS: EPSG:4326"

        # 存技术方案
        session.current_plan = { "task_breakdown": ..., "selected_tools": ..., "workflow_graph": ... }

        # 执行代码前拍快照
        snapshot_before = session.take_layer_snapshot()

        # ... 执行代码 ...

        # 执行后记录结果（自动拍快照并 diff）
        session.add_result(code="...", success=True, output="Done", snapshot_before=snapshot_before)

        # 获取发给 LLM 的上下文
        context_messages = session.get_context()
    """

    # ========================
    # 配置常量
    # ========================
    MAX_RECENT_ROUNDS = 3          # 完整保留的最近对话轮数（1轮 = 1个user + 1个assistant）
    COMPRESSION_TOKEN_THRESHOLD = 60000  # 触发压缩的 token 阈值（粗略估算）
    CHARS_PER_TOKEN = 4            # 粗略的字符/token 比率（英文约4，中文约1.5）

    def __init__(self):
        # ---- 五大数据字段 ----
        self.messages: List[Dict[str, str]] = []          # 对话历史
        self.data_overview: Optional[str] = None          # 数据概览缓存
        self.current_plan: Optional[Dict[str, Any]] = None  # 当前技术方案
        self.executed_codes: List[str] = []               # 历史执行代码
        self.results: List[ExecutionRecord] = []          # 执行结果（含数据变化快照）

        # ---- 内部状态 ----
        self._compressed_summary: Optional[str] = None    # 早期历史的压缩摘要
        self._compression_point: int = 0                  # 已压缩到的 messages 索引

    # ========================
    # 对话历史管理
    # ========================

    def add_message(self, role: str, content: str):
        """
        添加一条对话消息。

        Args:
            role: "user", "assistant", 或 "system"
            content: 消息内容
        """
        self.messages.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat()
        })

    def get_message_count(self) -> int:
        """返回当前对话消息总数"""
        return len(self.messages)

    # ========================
    # 数据概览管理
    # ========================

    def set_data_overview(self, overview: str):
        """
        设置/更新数据概览缓存。
        当用户加载新数据时调用，避免重复分析。
        """
        self.data_overview = overview

    def has_data_overview(self) -> bool:
        """检查是否已有数据概览缓存"""
        return self.data_overview is not None and len(self.data_overview.strip()) > 0

    # ========================
    # 技术方案管理
    # ========================

    def set_plan(self, plan: Dict[str, Any]):
        """
        设置当前技术方案。
        每次重新规划时覆盖更新。
        """
        self.current_plan = plan

    def clear_plan(self):
        """清除当前技术方案（用户取消或开始新任务时）"""
        self.current_plan = None

    # ========================
    # 执行结果管理（含数据变化快照）
    # ========================

    def take_layer_snapshot(self) -> LayerSnapshot:
        """
        拍摄当前 QGIS 图层快照。
        应在代码执行前调用，用于执行后对比数据变化。
        """
        snapshot = LayerSnapshot()
        snapshot.capture()
        return snapshot

    def add_result(self, code: str, success: bool, output: str,
                   error_message: str = "",
                   snapshot_before: Optional[LayerSnapshot] = None):
        """
        记录一次代码执行的完整结果。

        Args:
            code: 执行的代码
            success: 是否成功
            output: 控制台输出
            error_message: 错误信息（失败时）
            snapshot_before: 执行前的图层快照（用于计算数据变化）
        """
        # 计算数据变化
        data_changes = {"added": [], "removed": [], "modified": []}
        if snapshot_before is not None:
            snapshot_after = self.take_layer_snapshot()
            data_changes = snapshot_after.diff(snapshot_before)

        record = ExecutionRecord(
            code=code,
            success=success,
            output=output,
            error_message=error_message,
            data_changes=data_changes
        )

        self.results.append(record)
        self.executed_codes.append(code)

    # ========================
    # 上下文构建（核心方法）
    # ========================

    def get_context(self, include_plan: bool = True,
                    include_results: bool = True,
                    max_result_count: int = 5) -> List[Dict[str, str]]:
        """
        构建发给 LLM 的上下文 messages 数组。

        不是把所有历史原文都堆上去，而是智能组装：
        1. system 消息：数据概览 + 当前方案 + 最近的执行结果
        2. 对话历史：最近 N 轮完整保留，更早的用压缩摘要替代

        Args:
            include_plan: 是否包含当前技术方案
            include_results: 是否包含执行结果
            max_result_count: 最多包含多少条最近的执行结果

        Returns:
            适合直接传给 LLM 的 messages 列表
        """
        context_messages = []

        # ---- 1. 构建 system context ----
        system_parts = []

        # 数据概览
        if self.data_overview:
            system_parts.append(f"[Data Overview]\n{self.data_overview}")

        # 当前技术方案
        if include_plan and self.current_plan:
            plan_str = self._format_plan(self.current_plan)
            system_parts.append(f"[Current Plan]\n{plan_str}")

        # 最近的执行结果（含数据变化）
        if include_results and self.results:
            recent_results = self.results[-max_result_count:]
            results_strs = [r.to_context_str() for r in recent_results]
            system_parts.append(
                f"[Execution History - Last {len(recent_results)} results]\n" +
                "\n\n".join(results_strs)
            )

        if system_parts:
            context_messages.append({
                "role": "system",
                "content": "\n\n".join(system_parts)
            })

        # ---- 2. 构建对话历史 ----
        # 早期历史的压缩摘要
        if self._compressed_summary:
            context_messages.append({
                "role": "system",
                "content": f"[Earlier Conversation Summary]\n{self._compressed_summary}"
            })

        # 最近 N 轮完整保留
        recent_messages = self._get_recent_messages()
        for msg in recent_messages:
            context_messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })

        return context_messages

    def _get_recent_messages(self) -> List[Dict[str, str]]:
        """
        获取最近 N 轮的完整消息。
        1轮 = 1个 user 消息 + 1个 assistant 消息。
        从 _compression_point 之后开始计算。
        """
        # 从压缩点之后的消息中取最近的
        uncompressed = self.messages[self._compression_point:]

        if len(uncompressed) == 0:
            return []

        # 计算要保留多少条消息（最近 N 轮 × 2）
        max_messages = self.MAX_RECENT_ROUNDS * 2

        if len(uncompressed) <= max_messages:
            return uncompressed

        return uncompressed[-max_messages:]

    def _format_plan(self, plan: Dict[str, Any]) -> str:
        """将技术方案字典格式化为可读字符串"""
        if isinstance(plan, str):
            return plan

        parts = []
        if "task_breakdown" in plan:
            parts.append(f"Task Breakdown: {plan['task_breakdown']}")
        if "selected_tools" in plan:
            parts.append(f"Selected Tools: {plan['selected_tools']}")
        if "workflow_graph" in plan:
            parts.append(f"Workflow: {plan['workflow_graph']}")
        if "raw_response" in plan:
            parts.append(f"Plan Details: {plan['raw_response']}")

        return "\n".join(parts) if parts else str(plan)

    # ========================
    # 上下文压缩
    # ========================

    def estimate_token_count(self) -> int:
        """粗略估算当前上下文的 token 数"""
        total_chars = 0

        if self.data_overview:
            total_chars += len(self.data_overview)

        if self.current_plan:
            total_chars += len(str(self.current_plan))

        if self._compressed_summary:
            total_chars += len(self._compressed_summary)

        for msg in self.messages[self._compression_point:]:
            total_chars += len(msg.get("content", ""))

        for r in self.results[-5:]:  # 只计算最近5条
            total_chars += len(r.to_context_str())

        return int(total_chars / self.CHARS_PER_TOKEN)

    def needs_compression(self) -> bool:
        """检查是否需要压缩对话历史"""
        return self.estimate_token_count() > self.COMPRESSION_TOKEN_THRESHOLD

    def compress_history(self, compression_func=None):
        """
        压缩早期对话历史。

        Args:
            compression_func: 可选的压缩函数，签名为 (messages: List[Dict]) -> str
                              如果不提供，使用简单的内置压缩逻辑。
                              将来接入 AgentController 后，这里传入调用 LLM 的函数。
        """
        uncompressed = self.messages[self._compression_point:]

        # 保留最近 N 轮
        max_keep = self.MAX_RECENT_ROUNDS * 2
        if len(uncompressed) <= max_keep:
            return  # 没什么可压缩的

        # 需要压缩的部分
        to_compress = uncompressed[:-max_keep]

        if compression_func:
            # 使用外部函数压缩（将来由 AgentController 传入 LLM 调用）
            new_summary = compression_func(to_compress)
        else:
            # 内置简单压缩：提取关键信息
            new_summary = self._simple_compress(to_compress)

        # 合并旧摘要和新摘要
        if self._compressed_summary:
            self._compressed_summary = self._compressed_summary + "\n\n" + new_summary
        else:
            self._compressed_summary = new_summary

        # 更新压缩点
        self._compression_point += len(to_compress)

    def _simple_compress(self, messages: List[Dict[str, str]]) -> str:
        """
        内置的简单压缩逻辑（不调用 LLM）。
        提取 user 消息作为任务摘要，保留关键信息。
        将来会被 LLM 压缩函数替代。
        """
        summary_parts = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user":
                # 用户消息保留前200字符
                truncated = content[:200] + ("..." if len(content) > 200 else "")
                summary_parts.append(f"User requested: {truncated}")
            elif role == "assistant":
                # AI 回复只保留前100字符
                truncated = content[:100] + ("..." if len(content) > 100 else "")
                summary_parts.append(f"AI responded: {truncated}")

        return "Earlier conversation summary:\n" + "\n".join(summary_parts)

    # ========================
    # 会话管理
    # ========================

    def clear(self):
        """
        重置整个会话。
        用户点击"完成"或开始全新任务时调用。
        """
        self.messages.clear()
        self.data_overview = None
        self.current_plan = None
        self.executed_codes.clear()
        self.results.clear()
        self._compressed_summary = None
        self._compression_point = 0

    def soft_reset(self):
        """
        软重置：保留数据概览和对话历史，清除方案和执行结果。
        用于同一批数据上开始新任务时。
        """
        self.current_plan = None
        self.executed_codes.clear()
        self.results.clear()

    def get_summary(self) -> Dict[str, Any]:
        """
        返回当前会话状态的摘要（用于调试和 GUI 状态显示）。
        """
        return {
            "message_count": len(self.messages),
            "has_data_overview": self.has_data_overview(),
            "has_plan": self.current_plan is not None,
            "execution_count": len(self.results),
            "success_count": sum(1 for r in self.results if r.success),
            "estimated_tokens": self.estimate_token_count(),
            "is_compressed": self._compressed_summary is not None,
        }

    def __repr__(self):
        summary = self.get_summary()
        return (
            f"SessionContext("
            f"messages={summary['message_count']}, "
            f"executions={summary['execution_count']}, "
            f"tokens≈{summary['estimated_tokens']}, "
            f"compressed={summary['is_compressed']})"
        )
