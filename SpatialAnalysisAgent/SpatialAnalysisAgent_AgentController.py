# -*- coding: utf-8 -*-
"""
AgentController - 状态机调度核心
SpatialAnalysisAgent 的后台调度员，替代 MyScript.py 的线性流水线。

架构：方案 B（代码控制状态转换，AI 生成内容）
- 关键状态转换由用户按钮触发（100% 可控）
- 状态内的内容生成由 LLM 完成（灵活性）
- AI 永远不能自行决定执行代码

运行在 QThread 中，通过 Qt 信号与 GUI 通信。

状态流转：
    IDLE → ANALYZING → PLAN_READY → EXECUTING → RESULT_READY → IDLE
                ↑                                      │
                └──── CONVERSING ←─────────────────────┘
"""

import os
import sys
import traceback
from enum import Enum, auto
from typing import Optional, Dict, Any

from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot


# ============================================================
# 状态定义
# ============================================================

class AgentState(Enum):
    """Agent 的六个状态"""
    IDLE = auto()           # 空闲等待：等用户发送任务
    ANALYZING = auto()      # AI 分析中：任务分解 + 工具选择 + 生成方案
    PLAN_READY = auto()     # 方案待确认：展示方案，等用户确认/修改/取消
    EXECUTING = auto()      # 代码执行中：生成代码 + 执行
    RESULT_READY = auto()   # 结果待反馈：展示结果，等用户追问/报错/完成
    CONVERSING = auto()     # 对话修改中：用户要求修改方案或报告结果有误


# ============================================================
# 按钮动作定义
# ============================================================

class UserAction(Enum):
    """用户通过按钮触发的动作"""
    SEND_TASK = auto()          # 发送新任务（IDLE 状态下）
    CONFIRM_PLAN = auto()       # 确认执行方案（PLAN_READY → EXECUTING）
    MODIFY_PLAN = auto()        # 要求修改方案（PLAN_READY → CONVERSING）
    CANCEL = auto()             # 取消当前任务（任意状态 → IDLE）
    TWEAK_PLAN = auto()         # 微调方案（RESULT_READY → PLAN_READY，小改当前方案）
    NEW_ANALYSIS = auto()       # 重新分析（RESULT_READY → ANALYZING，基于上下文提新问题）
    REPORT_ERROR = auto()       # 报告结果有误（RESULT_READY → CONVERSING → PLAN_READY）
    FINISH = auto()             # 完成任务（RESULT_READY → IDLE）
    INTERRUPT = auto()          # 中断正在进行的操作（ANALYZING/EXECUTING → IDLE）
    SEND_MESSAGE = auto()       # 发送对话消息（CONVERSING 状态下）


# ============================================================
# AgentController 主类
# ============================================================

class AgentController(QObject):
    """
    后台调度员：维护状态机，根据当前状态和用户输入路由到对应的处理函数。

    职责：
        1. 维护状态机，管理六个状态之间的转换
        2. 将 MyScript.py 的线性流水线拆解为可独立调度的阶段函数
        3. 通过 Qt 信号与 GUI 通信
        4. 持有 SessionContext 实例，保证会话状态贯穿多轮交互
        5. 意图分类：区分 GIS 任务和闲聊

    核心规则：
        所有涉及"执行代码"的状态转换只能由用户按钮触发，
        AI 永远不能自行决定执行。
    """

    # ========================
    # Qt 信号（→ GUI）
    # ========================

    # 状态变化：通知 GUI 更新按钮组
    state_changed = pyqtSignal(str)            # 参数: 新状态名称

    # 分析阶段的输出
    status_update = pyqtSignal(str)            # 状态提示（如"正在分析任务..."）
    data_overview_ready = pyqtSignal(str)      # 数据概览完成
    task_breakdown_ready = pyqtSignal(str)     # 任务分解完成
    tools_selected = pyqtSignal(str)           # 工具选择完成

    # 方案和结果
    plan_ready = pyqtSignal(dict)              # 技术方案已生成，等用户确认
    result_ready = pyqtSignal(dict)            # 执行结果已生成，等用户反馈

    # 代码相关
    graph_ready = pyqtSignal(str)              # 工作流图 HTML 路径
    code_ready = pyqtSignal(str)               # 生成的代码
    execution_output = pyqtSignal(str)         # 执行过程的输出

    # 对话/闲聊回复
    chat_response = pyqtSignal(str)            # AI 的对话回复（闲聊或修改建议）

    # 错误和完成
    error_occurred = pyqtSignal(str)           # 发生错误
    task_finished = pyqtSignal(bool)           # 整个任务完成（True=成功）

    def __init__(self, session=None):
        super().__init__()

        # 导入 SessionContext（延迟导入避免循环依赖）
        if session is not None:
            self.session = session
        else:
            from SpatialAnalysisAgent_SessionContext import SessionContext
            self.session = SessionContext()

        # 当前状态
        self._state = AgentState.IDLE

        # 运行控制
        self._is_running = True

        # 任务参数（由 GUI 传入）
        self.task: str = ""
        self.data_path: str = ""
        self.workspace_directory: str = ""
        self.model_name: str = ""
        self.is_review: bool = True
        self.reasoning_effort_value: str = "medium"

        # 分析阶段产出（暂存，供执行阶段使用）
        self._analysis_result: Dict[str, Any] = {}

    # ========================
    # 状态管理
    # ========================

    @property
    def state(self) -> AgentState:
        return self._state

    @state.setter
    def state(self, new_state: AgentState):
        old_state = self._state
        self._state = new_state
        print(f"[AgentController] State: {old_state.name} → {new_state.name}")
        self.state_changed.emit(new_state.name)

    def get_state_name(self) -> str:
        return self._state.name

    # ========================
    # 按钮组查询（GUI 用来决定显示哪些按钮）
    # ========================

    def get_available_actions(self) -> list:
        """
        根据当前状态返回可用的用户动作列表。
        GUI 据此显示对应的按钮。
        """
        action_map = {
            AgentState.IDLE: [UserAction.SEND_TASK],
            AgentState.ANALYZING: [UserAction.INTERRUPT],
            AgentState.PLAN_READY: [
                UserAction.CONFIRM_PLAN,
                UserAction.MODIFY_PLAN,
                UserAction.CANCEL,
            ],
            AgentState.EXECUTING: [UserAction.INTERRUPT],
            AgentState.RESULT_READY: [
                UserAction.TWEAK_PLAN,      # 微调方案 → PLAN_READY
                UserAction.NEW_ANALYSIS,    # 重新分析 → ANALYZING
                UserAction.REPORT_ERROR,    # 结果有误 → CONVERSING → PLAN_READY
                UserAction.FINISH,          # 完成 → IDLE
            ],
            AgentState.CONVERSING: [UserAction.SEND_MESSAGE, UserAction.CANCEL],
        }
        return action_map.get(self._state, [])

    # ========================
    # 用户输入路由（核心方法）
    # ========================

    def handle_user_action(self, action: UserAction, message: str = ""):
        """
        处理用户的按钮动作。这是 GUI 与 AgentController 交互的唯一入口。

        Args:
            action: 用户触发的动作类型
            message: 附带的文本消息（如任务描述、修改意见）
        """
        print(f"[AgentController] Action: {action.name}, State: {self._state.name}")

        # ---- 全局动作：任何状态都能响应 ----
        if action == UserAction.CANCEL:
            self._handle_cancel()
            return

        if action == UserAction.INTERRUPT:
            self._handle_interrupt()
            return

        # ---- 状态相关动作 ----
        if self._state == AgentState.IDLE:
            if action == UserAction.SEND_TASK:
                self._handle_new_task(message)

        elif self._state == AgentState.PLAN_READY:
            if action == UserAction.CONFIRM_PLAN:
                self._handle_confirm_plan()
            elif action == UserAction.MODIFY_PLAN:
                self._handle_modify_plan(message)

        elif self._state == AgentState.RESULT_READY:
            if action == UserAction.TWEAK_PLAN:
                self._handle_tweak_plan(message)
            elif action == UserAction.NEW_ANALYSIS:
                self._handle_new_analysis(message)
            elif action == UserAction.REPORT_ERROR:
                self._handle_report_error(message)
            elif action == UserAction.FINISH:
                self._handle_finish()

        elif self._state == AgentState.CONVERSING:
            if action == UserAction.SEND_MESSAGE:
                self._handle_conversation_message(message)

        else:
            print(f"[AgentController] Ignored action {action.name} in state {self._state.name}")

    def handle_text_input(self, message: str):
        """
        处理用户在输入框中发送的文本。
        根据当前状态自动判断应该走哪条路。

        这个方法让用户不需要关心当前在哪个状态，
        直接在输入框里打字发送就行。
        """
        message = message.strip()
        if not message:
            return

        if self._state == AgentState.IDLE:
            # 空闲状态：可能是新任务，也可能是闲聊
            # TODO: 意图分类（将来用 LLM 判断是 GIS 任务还是闲聊）
            # 现在暂时全部当作新任务处理
            self.handle_user_action(UserAction.SEND_TASK, message)

        elif self._state == AgentState.PLAN_READY:
            # 方案待确认状态：文本输入视为修改意见
            self.handle_user_action(UserAction.MODIFY_PLAN, message)

        elif self._state == AgentState.RESULT_READY:
            # 结果待反馈状态：文本输入默认视为基于当前结果的新分析请求
            # 如果用户想微调方案，通过按钮触发 TWEAK_PLAN
            self.handle_user_action(UserAction.NEW_ANALYSIS, message)

        elif self._state == AgentState.CONVERSING:
            # 对话修改状态：继续对话
            self.handle_user_action(UserAction.SEND_MESSAGE, message)

        elif self._state in (AgentState.ANALYZING, AgentState.EXECUTING):
            # 正在运行中，提示用户等待
            self.chat_response.emit("正在处理中，请稍候...")

        else:
            self.chat_response.emit("系统状态异常，请点击取消后重试。")

    # ========================
    # 动作处理函数
    # ========================

    def _handle_new_task(self, task: str):
        """处理新任务：IDLE → ANALYZING → PLAN_READY"""
        self.task = task
        self.session.add_message("user", task)
        self.state = AgentState.ANALYZING
        self.status_update.emit("正在分析任务...")

        try:
            self._run_analysis()
        except Exception as e:
            self.error_occurred.emit(f"分析阶段出错: {str(e)}")
            traceback.print_exc()
            self.state = AgentState.IDLE

    def _handle_confirm_plan(self):
        """用户确认方案：PLAN_READY → EXECUTING → RESULT_READY"""
        self.state = AgentState.EXECUTING
        self.status_update.emit("正在生成并执行代码...")

        try:
            self._run_execution()
        except Exception as e:
            self.error_occurred.emit(f"执行阶段出错: {str(e)}")
            traceback.print_exc()
            self.state = AgentState.IDLE

    def _handle_modify_plan(self, modification: str):
        """用户要求修改方案：PLAN_READY → CONVERSING → PLAN_READY"""
        self.session.add_message("user", f"修改意见: {modification}")
        self.state = AgentState.CONVERSING
        self.status_update.emit("正在根据修改意见调整方案...")

        try:
            self._run_plan_revision(modification)
        except Exception as e:
            self.error_occurred.emit(f"方案修改出错: {str(e)}")
            traceback.print_exc()
            self.state = AgentState.PLAN_READY

    def _handle_tweak_plan(self, modification: str):
        """
        微调方案：RESULT_READY → CONVERSING → PLAN_READY
        用户觉得结果大方向对但需要小改（如"缓冲区改成500米"）。
        不重新分析，直接修改当前方案。
        """
        self.session.add_message("user", f"微调方案: {modification}")
        self.state = AgentState.CONVERSING
        self.status_update.emit("正在微调方案...")

        try:
            self._run_plan_revision(modification)
        except Exception as e:
            self.error_occurred.emit(f"方案微调出错: {str(e)}")
            traceback.print_exc()
            self.state = AgentState.RESULT_READY

    def _handle_new_analysis(self, message: str):
        """
        重新分析：RESULT_READY → ANALYZING → PLAN_READY
        用户基于当前结果提出新问题（如"再帮我算一下面积"）。
        SessionContext 保留所有历史，AI 能理解上下文。
        """
        self.session.add_message("user", message)
        self.task = message
        self.state = AgentState.ANALYZING
        self.status_update.emit("正在分析新任务...")

        try:
            self._run_analysis()
        except Exception as e:
            self.error_occurred.emit(f"新任务分析出错: {str(e)}")
            traceback.print_exc()
            self.state = AgentState.RESULT_READY

    def _handle_report_error(self, error_description: str):
        """用户报告结果有误：RESULT_READY → CONVERSING → PLAN_READY"""
        self.session.add_message("user", f"结果有误: {error_description}")
        self.state = AgentState.CONVERSING
        self.status_update.emit("正在分析问题并重新规划...")

        try:
            self._run_plan_revision(error_description)
        except Exception as e:
            self.error_occurred.emit(f"错误处理出错: {str(e)}")
            traceback.print_exc()
            self.state = AgentState.RESULT_READY

    def _handle_conversation_message(self, message: str):
        """处理 CONVERSING 状态下的对话消息"""
        self.session.add_message("user", message)
        self.status_update.emit("正在处理...")

        try:
            self._run_conversation(message)
        except Exception as e:
            self.error_occurred.emit(f"对话处理出错: {str(e)}")
            traceback.print_exc()

    def _handle_cancel(self):
        """取消当前任务，回到 IDLE"""
        self._is_running = False
        self.session.add_message("system", "用户取消了当前任务")
        self.state = AgentState.IDLE
        self.status_update.emit("任务已取消")
        self._is_running = True  # 重置，准备接受下一个任务

    def _handle_interrupt(self):
        """中断正在运行的操作"""
        self._is_running = False
        self.status_update.emit("正在中断...")
        # 实际中断由 _run_analysis / _run_execution 内部检查 _is_running 实现
        self.state = AgentState.IDLE
        self._is_running = True

    def _handle_finish(self):
        """用户确认任务完成：RESULT_READY → IDLE"""
        self.session.add_message("system", "任务完成")
        self.state = AgentState.IDLE
        self.task_finished.emit(True)
        self.status_update.emit("任务完成")

    # ========================
    # 阶段函数（占位逻辑，第三步替换为真实 helper 调用）
    # ========================

    def _run_analysis(self):
        """
        分析阶段：对应 MyScript.py 的步骤 1-6
        任务名生成 → 数据概览 → 任务分解 → 工具选择 → 文档检索 → 工作流图

        完成后自动暂停在 PLAN_READY，等待用户确认。
        """
        # ---- 占位逻辑（第三步会替换为真实 helper 调用）----

        if not self._is_running:
            return

        # 步骤 1: 生成任务名
        self.status_update.emit("正在生成任务名...")
        task_name = f"placeholder_task"  # TODO: helper.generate_task_name(...)

        if not self._is_running:
            return

        # 步骤 2: 数据概览
        self.status_update.emit("正在分析数据...")
        if not self.session.has_data_overview():
            data_overview = "placeholder data overview"  # TODO: helper.add_data_overview(...)
            self.session.set_data_overview(data_overview)
        self.data_overview_ready.emit(self.session.data_overview)

        if not self._is_running:
            return

        # 步骤 3: 任务分解
        self.status_update.emit("正在分解任务...")
        task_breakdown = f"Placeholder breakdown for: {self.task}"  # TODO: helper.Query_tuning(...)
        self.task_breakdown_ready.emit(task_breakdown)

        if not self._is_running:
            return

        # 步骤 4: 工具选择
        self.status_update.emit("正在选择工具...")
        selected_tools = ["placeholder_tool"]  # TODO: helper.tool_select(...)
        self.tools_selected.emit(str(selected_tools))

        if not self._is_running:
            return

        # 步骤 5: 文档检索
        self.status_update.emit("正在检索工具文档...")
        documentation = "placeholder documentation"  # TODO: ToolsDocumentation

        if not self._is_running:
            return

        # 步骤 6: 工作流图
        self.status_update.emit("正在生成工作流图...")
        # TODO: helper.generate_graph_response(...)
        self.graph_ready.emit("")

        # 保存分析结果
        self._analysis_result = {
            "task_name": task_name,
            "task_breakdown": task_breakdown,
            "selected_tools": selected_tools,
            "documentation": documentation,
        }

        # 存入 SessionContext
        self.session.set_plan(self._analysis_result)
        self.session.add_message("assistant",
            f"分析完成。任务分解: {task_breakdown}\n选用工具: {selected_tools}")

        # ---- 自动暂停，等用户确认 ----
        self.state = AgentState.PLAN_READY
        self.plan_ready.emit(self._analysis_result)

    def _run_execution(self):
        """
        执行阶段：对应 MyScript.py 的步骤 7
        代码生成 → 代码审查（可选）→ 代码执行 → 自动调试

        完成后自动暂停在 RESULT_READY，等待用户反馈。
        """
        # ---- 占位逻辑（第三步会替换为真实 helper 调用）----

        if not self._is_running:
            return

        # 拍图层快照
        snapshot_before = self.session.take_layer_snapshot()

        # 步骤 7a: 生成代码
        self.status_update.emit("正在生成代码...")
        generated_code = "# placeholder code\nprint('Hello from AgentController')"
        # TODO: helper.generate_operation_code(...)
        self.code_ready.emit(generated_code)

        if not self._is_running:
            return

        # 步骤 7b: 代码审查（可选）
        if self.is_review:
            self.status_update.emit("正在审查代码...")
            reviewed_code = generated_code  # TODO: helper.code_review(...)
        else:
            reviewed_code = generated_code

        if not self._is_running:
            return

        # 步骤 7c: 执行代码
        self.status_update.emit("正在执行代码...")
        # TODO: helper.execute_complete_program(...)
        execution_success = True
        execution_output = "Placeholder: execution completed"
        error_message = ""

        # 记录执行结果
        self.session.add_result(
            code=reviewed_code,
            success=execution_success,
            output=execution_output,
            error_message=error_message,
            snapshot_before=snapshot_before
        )
        self.session.add_message("assistant",
            f"代码执行{'成功' if execution_success else '失败'}。")

        # ---- 自动暂停，等用户反馈 ----
        result_info = {
            "code": reviewed_code,
            "success": execution_success,
            "output": execution_output,
            "error_message": error_message,
            "data_changes": self.session.results[-1].data_changes if self.session.results else {},
        }
        self.state = AgentState.RESULT_READY
        self.result_ready.emit(result_info)

    def _run_plan_revision(self, user_feedback: str):
        """
        方案修改：用户反馈 + 原方案 + 会话上下文 → LLM 生成新方案

        完成后回到 PLAN_READY，用户再次确认。
        """
        # ---- 占位逻辑（第四步实现）----

        self.status_update.emit("正在修改方案...")

        # TODO: 把用户反馈 + 原方案 + session.get_context() 发给 LLM
        # 使用 Constants 里的 plan_revision_role / plan_revision_requirement
        revised_plan = {
            **self._analysis_result,
            "revision_note": f"根据用户反馈修改: {user_feedback}",
        }

        self.session.set_plan(revised_plan)
        self._analysis_result = revised_plan
        self.session.add_message("assistant", f"已根据您的意见修改方案: {user_feedback}")

        # 回到方案确认状态
        self.state = AgentState.PLAN_READY
        self.plan_ready.emit(revised_plan)

    def _run_conversation(self, message: str):
        """
        对话处理：处理 CONVERSING 状态下的自由对话。

        根据对话内容决定下一步：
        - 如果是新的 GIS 任务 → 进入 ANALYZING
        - 如果是方案修改 → 回到 PLAN_READY
        - 如果是闲聊 → 回复后保持当前上下文状态
        """
        # ---- 占位逻辑（第四步实现）----

        # TODO: 意图分类 + LLM 回复
        response = f"[占位回复] 收到您的消息: {message}"
        self.session.add_message("assistant", response)
        self.chat_response.emit(response)

        # 暂时回到 PLAN_READY（将来根据意图判断决定去哪个状态）
        if self.session.current_plan:
            self.state = AgentState.PLAN_READY
            self.plan_ready.emit(self.session.current_plan)
        else:
            self.state = AgentState.IDLE

    # ========================
    # 辅助方法
    # ========================

    def set_task_params(self, task: str, data_path: str, workspace_directory: str,
                        model_name: str, is_review: bool, reasoning_effort_value: str):
        """
        GUI 在启动任务前调用，设置任务参数。
        """
        self.task = task
        self.data_path = data_path
        self.workspace_directory = workspace_directory
        self.model_name = model_name
        self.is_review = is_review
        self.reasoning_effort_value = reasoning_effort_value

    def reset(self):
        """完全重置 AgentController（插件关闭或用户主动清除时）"""
        self._is_running = False
        self.state = AgentState.IDLE
        self.session.clear()
        self._analysis_result = {}
        self._is_running = True

    def check_running(self) -> bool:
        """供阶段函数内部检查是否被中断"""
        return self._is_running
