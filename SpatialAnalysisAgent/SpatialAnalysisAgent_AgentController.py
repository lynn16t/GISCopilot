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
import re
import sys
import ast
import time
import uuid
import traceback
import io
from enum import Enum, auto
from typing import Optional, Dict, Any, List

from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

# Phase 4: 输出解析和风控门
from SpatialAnalysisAgent_OutputParser import (
    AgentOutputParser, OutputType, ParsedOutput
)
from SpatialAnalysisAgent_GuardGate import GuardGate


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
# AgentWorkerThread - 在独立线程中运行阻塞的 helper 调用
# ============================================================

class AgentWorkerThread(QThread):
    """
    在独立线程中运行 AgentController 的阻塞方法
    （如 _run_analysis, _run_execution）。
    捕获 stdout 输出并通过信号转发到 UI。
    """
    output_line = pyqtSignal(str)       # 捕获的 stdout 输出
    work_finished = pyqtSignal(bool)    # 工作完成（True=成功）
    work_error = pyqtSignal(str)        # 工作出错

    def __init__(self, target_func, *args, **kwargs):
        super().__init__()
        self._target_func = target_func
        self._args = args
        self._kwargs = kwargs

    def run(self):
        """在线程中执行目标函数，捕获 stdout"""
        original_stdout = sys.stdout
        original_stderr = sys.stderr

        try:
            redirector = _ThreadStreamRedirector()
            redirector.output_written.connect(self.output_line.emit)

            sys.stdout = redirector
            sys.stderr = redirector

            self._target_func(*self._args, **self._kwargs)
            self.work_finished.emit(True)

        except Exception as e:
            traceback_str = traceback.format_exc()
            self.work_error.emit(f"{str(e)}\n{traceback_str}")
            self.work_finished.emit(False)
        finally:
            # flush 残留 buffer（streaming 输出可能不以 \n 结尾）
            if hasattr(sys.stdout, 'flush'):
                sys.stdout.flush()
            if hasattr(sys.stderr, 'flush') and sys.stderr is not sys.stdout:
                sys.stderr.flush()
            sys.stdout = original_stdout
            sys.stderr = original_stderr


class _ThreadStreamRedirector(QObject):
    """轻量级 stdout 重定向器，将 write() 转为 Qt 信号。
    与 dockwidget 中的 StreamRedirector 保持一致：
    缓冲输出，只在遇到换行符时 emit，避免逐 token 断行。
    """
    output_written = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.buffer = ''

    def write(self, text):
        if text:
            self.buffer += text
            while '\n' in self.buffer:
                line, self.buffer = self.buffer.split('\n', 1)
                self.output_written.emit(line)

    def flush(self):
        if self.buffer:
            self.output_written.emit(self.buffer)
            self.buffer = ''


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

    核心规则：
        所有涉及"执行代码"的状态转换只能由用户按钮触发，
        AI 永远不能自行决定执行。
    """

    # ========================
    # Qt 信号（→ GUI）
    # ========================

    state_changed = pyqtSignal(str)
    status_update = pyqtSignal(str)
    data_overview_ready = pyqtSignal(str)
    task_breakdown_ready = pyqtSignal(str)
    tools_selected = pyqtSignal(str)

    plan_ready = pyqtSignal(dict)
    result_ready = pyqtSignal(dict)

    graph_ready = pyqtSignal(str)
    code_ready = pyqtSignal(str)
    execution_output = pyqtSignal(str)

    chat_response = pyqtSignal(str)

    # Phase 4: 知识库更新请求信号
    knowledge_update_requested = pyqtSignal(str)

    error_occurred = pyqtSignal(str)
    task_finished = pyqtSignal(bool)

    def __init__(self, session=None, knowledge_manager=None):
        super().__init__()

        # Phase 3: 使用新的 SessionContext，传入 knowledge_manager
        from SpatialAnalysisAgent_SessionContext import SessionContext
        from SpatialAnalysisAgent_ToolRetrieval import ToolRetriever
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.tool_retriever = ToolRetriever(
            tools_doc_dir=os.path.join(current_dir, "Tools_Documentation"),
            tools_json_path=os.path.join(current_dir, "qgis340_tools.json"),
            model_dir=os.path.join(current_dir, "embedding_model"),
        )
        
        if session is not None and isinstance(session, SessionContext):
            self.session = session
        else:
            # 创建新的 SessionContext，传入 knowledge_manager
            self.session = SessionContext(knowledge_manager=knowledge_manager)

        self.knowledge_manager = knowledge_manager

        # Phase 4: 初始化输出解析器和风控门
        self.output_parser = AgentOutputParser()
        self.guard_gate = GuardGate()

        self._state = AgentState.IDLE
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

        # request_id（每次新任务生成一个）
        self._request_id: str = ""

        # 当前运行的工作线程
        self._worker: Optional[AgentWorkerThread] = None
        
        from SpatialAnalysisAgent_ToolRetrieval import ToolRetriever
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.tool_retriever = ToolRetriever(
            tools_doc_dir=os.path.join(current_dir, "Tools_Documentation"),
            tools_json_path=os.path.join(current_dir, "qgis340_tools.json"),
            model_dir=os.path.join(current_dir, "embedding_model"),
        )

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
    # 按钮组查询
    # ========================

    def get_available_actions(self) -> list:
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
                UserAction.TWEAK_PLAN,
                UserAction.NEW_ANALYSIS,
                UserAction.REPORT_ERROR,
                UserAction.FINISH,
            ],
            AgentState.CONVERSING: [UserAction.SEND_MESSAGE, UserAction.CANCEL],
        }
        return action_map.get(self._state, [])

    # ========================
    # 用户输入路由（核心方法）
    # ========================

    def handle_user_action(self, action: UserAction, message: str = ""):
        print(f"[AgentController] Action: {action.name}, State: {self._state.name}")

        if action == UserAction.CANCEL:
            self._handle_cancel()
            return
        if action == UserAction.INTERRUPT:
            self._handle_interrupt()
            return

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
        Phase 5: 统一对话循环入口。

        所有非阻塞状态（IDLE / PLAN_READY / RESULT_READY / CONVERSING）
        都走同一个对话循环入口。
        """
        message = message.strip()
        if not message:
            return

        # 阻塞状态：正在处理中
        if self._state in (AgentState.ANALYZING, AgentState.EXECUTING):
            self.chat_response.emit("正在处理中，请稍候...")
            return

        # Phase 5: 所有其他状态统一走对话循环
        print(f"[AgentController] handle_text_input in state {self._state.name}")

        # 首次输入或数据概览不存在时，先生成 request_id
        if not self._request_id:
            self._generate_request_id()

        # 统一走对话循环
        self._start_worker(self._run_conversation_loop, message)

    # ========================
    # 启动工作线程
    # ========================

    def _start_worker(self, target_func, *args, **kwargs):
        """创建并启动工作线程来运行阻塞的阶段函数"""
        self._worker = AgentWorkerThread(target_func, *args, **kwargs)
        self._worker.output_line.connect(self.execution_output.emit)
        self._worker.work_error.connect(self._on_worker_error)
        self._worker.start()
        return self._worker

    def _on_worker_error(self, error_msg):
        """工作线程报错时的处理"""
        self.error_occurred.emit(error_msg)
        if self._state in (AgentState.ANALYZING, AgentState.IDLE):
            self.state = AgentState.IDLE
        elif self._state == AgentState.EXECUTING:
            self.state = AgentState.PLAN_READY
        elif self._state == AgentState.CONVERSING:
            if self.session.current_plan:
                self.state = AgentState.PLAN_READY
            else:
                self.state = AgentState.IDLE

    # ========================
    # 动作处理函数
    # ========================

    def _handle_new_task(self, task: str):
        """
        处理 IDLE 状态的用户输入。

        Phase 5: 简化，不再分类意图，直接走对话循环。
        """
        self.task = task

        # Phase 3: 重置 SessionContext 并设置任务
        self.session.reset()
        self.session.set_task(task)

        self.status_update.emit("正在理解您的消息...")
        self._generate_request_id()

        # Phase 5: 直接走对话循环
        self._start_worker(self._run_conversation_loop, task)

    def _handle_confirm_plan(self):
        """用户确认方案：PLAN_READY → EXECUTING → RESULT_READY"""
        self.state = AgentState.EXECUTING
        self.status_update.emit("正在生成并执行代码...")
        self._start_worker(self._run_execution)

    def _handle_modify_plan(self, modification: str):
        """
        用户要求修改方案：PLAN_READY → 对话循环。

        Phase 5: 走对话循环。用户的修改意见作为普通消息，
        LLM 看到当前 plan 上下文后，输出修改后的 PLAN JSON。
        """
        self.status_update.emit("正在根据修改意见调整方案...")
        # Phase 5: 走对话循环
        self._start_worker(self._run_conversation_loop, modification)

    def _handle_tweak_plan(self, modification: str):
        """微调方案：RESULT_READY → CONVERSING → PLAN_READY"""
        self.session.add_message("user", f"微调方案: {modification}")
        self.state = AgentState.CONVERSING
        self.status_update.emit("正在微调方案...")
        self._start_worker(self._run_plan_revision, modification)

    def _handle_new_analysis(self, message: str):
        """
        重新分析：RESULT_READY → 对话循环。

        Phase 5: 走对话循环。用户的新需求作为消息，
        LLM 根据上下文（包含已有结果）判断是输出新 PLAN 还是追问。
        """
        self.task = message
        self.status_update.emit("正在分析...")
        self._generate_request_id()
        # Phase 5: 走对话循环
        self._start_worker(self._run_conversation_loop, message)

    def _handle_report_error(self, error_description: str):
        """
        用户报告结果有误：RESULT_READY → 对话循环。

        Phase 5: 走对话循环。用户描述错误，LLM 看到结果上下文后，
        可能输出修改后的 PLAN，或者追问具体哪里有问题。
        """
        self.status_update.emit("正在分析问题...")
        # Phase 5: 走对话循环
        self._start_worker(self._run_conversation_loop, error_description)

    def _handle_conversation_message(self, message: str):
        """处理 CONVERSING 状态下的对话消息"""
        # 不再预先添加消息，由 _run_conversation_loop 统一添加
        self.status_update.emit("正在处理...")
        self._start_worker(self._run_conversation_loop, message)

    def _handle_cancel(self):
        """取消当前任务，回到 IDLE"""
        self._is_running = False
        if self._worker and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(3000)
        self.session.add_message("system", "用户取消了当前任务")
        self.state = AgentState.IDLE
        self.status_update.emit("任务已取消")
        self._is_running = True

    def _handle_interrupt(self):
        """中断正在运行的操作"""
        self._is_running = False
        if self._worker and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(3000)
        self.status_update.emit("已中断")
        self.state = AgentState.IDLE
        self._is_running = True

    def _handle_finish(self):
        """用户确认任务完成：RESULT_READY → IDLE"""
        self.session.add_message("system", "任务完成")
        self.state = AgentState.IDLE
        self.task_finished.emit(True)
        self.status_update.emit("任务完成")

    # ========================
    # 辅助方法
    # ========================

    def _generate_request_id(self):
        try:
            import SpatialAnalysisAgent_helper as helper
            api_key = helper.load_OpenAI_key()
            if 'gibd-services' in (api_key or ''):
                self._request_id = helper.get_question_id(api_key)
            else:
                self._request_id = str(uuid.uuid4())
        except Exception:
            self._request_id = str(uuid.uuid4())

    def _get_reasoning_kwargs(self) -> dict:
        kwargs = {}
        if (self.reasoning_effort_value
                and self.model_name in ['gpt-5', 'gpt-5.1', 'gpt-5.2']):
            kwargs['reasoning_effort'] = self.reasoning_effort_value
        return kwargs

    # ========================
    # Phase 5: 旧的意图分类代码已删除
    # 现在统一使用对话循环 + OutputParser
    # ========================

    # ============================================================
    # 阶段函数 —— 真实 helper 调用（在 AgentWorkerThread 中运行）
    # ============================================================

    def _run_analysis(self):
        """
        分析阶段：对应 MyScript.py 的步骤 1-6
        任务名生成 → 数据概览 → 任务分解 → 工具选择 → 文档检索 → 工作流图

        完成后自动暂停在 PLAN_READY，等待用户确认。
        """
        import SpatialAnalysisAgent_helper as helper
        import SpatialAnalysisAgent_Constants as constants
        import SpatialAnalysisAgent_ToolsDocumentation as ToolsDocumentation
        import SpatialAnalysisAgent_Codebase as codebase

        current_script_dir = os.path.dirname(os.path.abspath(__file__))
        Tools_Documentation_dir = os.path.join(
            current_script_dir, 'Tools_Documentation')

        operation_model = helper.get_model_for_operation(self.model_name)

        if not self._is_running:
            return

        # ====== 步骤 1: 生成任务名 ======
        self.status_update.emit("正在生成任务名...")
        print("=" * 56)
        print("AI IS ANALYZING THE TASK ...")
        print("=" * 56)

        task_name = helper.generate_task_name_with_model_provider(
            request_id=self._request_id,
            model_name=operation_model,
            stream=False,
            task_description=self.task,
            reasoning_effort=self.reasoning_effort_value)
        task_name = task_name.strip().strip('"').strip("'")
        print(f"task_name: {task_name}")

        if not self._is_running:
            return

        # ====== 步骤 2: 数据概览 ======
        self.status_update.emit("正在分析数据...")
        print("=" * 56)
        print("AI IS EXAMINING THE DATA ...")
        print("=" * 56)

        data_path_str = self.data_path.split('\n')

        if not self.session.has_data_overview():
            attributes_json, data_overview = \
                helper.add_data_overview_to_data_location(
                    request_id=self._request_id,
                    task=self.task,
                    data_location_list=data_path_str,
                    model_name=operation_model,
                    reasoning_effort=self.reasoning_effort_value)
            self.session.set_data_overview(str(data_overview))
            print(f"data overview: {data_overview}")
            print(attributes_json)
        else:
            # 数据未变，复用缓存
            cached = self.session.data_overview
            if cached.startswith('['):
                try:
                    data_overview = ast.literal_eval(cached)
                except Exception:
                    data_overview = cached.split('\n')
            else:
                data_overview = cached.split('\n')
            print("Data overview reused from session cache")

        self.data_overview_ready.emit(self.session.data_overview)

        if not self._is_running:
            return

        # ====== 步骤 3: 任务分解（Query Tuning）======
        self.status_update.emit("正在分解任务...")

        # Phase 3: 使用新模式
        self.session.set_data_overview(data_overview)

        step_instruction = helper.build_query_tuning_instruction(task=self.task)
        messages = self.session.build_messages(
            step="query_tuning",
            step_instruction=step_instruction,
            step_role=constants.Query_tuning_role
        )

        print("TASK_BREAKDOWN:", end="")
        task_breakdown = helper.unified_llm_call(
            request_id=self._request_id,
            messages=messages,
            model_name=self.model_name,
            stream=True,
            reasoning_effort=self.reasoning_effort_value
        )
        print("\n_")

        # 记录结果到 SessionContext
        self.session.add_message("assistant", f"Task breakdown:\n{task_breakdown}")
        self.task_breakdown_ready.emit(task_breakdown)

        if not self._is_running:
            return

        # ====== 步骤 3.5: Embedding 检索候选工具 ======
        from SpatialAnalysisAgent_ToolRetrieval import ToolRetriever, get_whitelist_tool_info
        
        retrieved_tools = self.tool_retriever.retrieve(task_breakdown, top_k=20)
        whitelist_info = get_whitelist_tool_info(self.tool_retriever, constants.TOOL_WHITELIST)
        candidate_tools_str = ToolRetriever.format_for_prompt(whitelist_info, retrieved_tools)
        print(f"[ToolRetrieval] Whitelist: {len(whitelist_info)}, Retrieved: {len(retrieved_tools)}")

        # ====== 步骤 4: 工具选择（结构化执行计划）======
        self.status_update.emit("正在规划执行步骤...")
        print("=" * 56)
        print("AI IS PLANNING THE EXECUTION STEPS ...")
        print("=" * 56)

        step_instruction = helper.build_tool_selection_instruction(
            task_breakdown=task_breakdown,
            candidate_tools_str=candidate_tools_str,  # ← 加这个
        )
        messages = self.session.build_messages(
            step="tool_selection",
            step_instruction=step_instruction,
            step_role=constants.ToolSelect_role
        )

        print("EXECUTION PLAN:", end="")
        plan_response = helper.unified_llm_call(
            request_id=self._request_id,
            messages=messages,
            model_name=operation_model,
            stream=True,
            reasoning_effort=self.reasoning_effort_value
        )
        print("\n")

        # Phase 4: 使用风控门处理工具选择输出
        action = self.process_llm_output(plan_response)

        if action.action_type == "confirm_plan":
            # 成功解析为结构化计划
            structured_plan = action.data["plan"]
            plan_display_text = action.data["plan_text"]

            print(f"Structured plan parsed: {structured_plan}")

            # 提取工具 ID 列表
            selected_tools = helper.extract_tool_ids_from_plan(structured_plan)
            print(f"Extracted tools: {selected_tools}")

            # 保存完整的结构化计划到 SessionContext
            import json
            plan_text = json.dumps(structured_plan, indent=2, ensure_ascii=False)
            self.session.set_plan(plan_text)

            # 在聊天框展示格式化的 plan
            self.status_update.emit(plan_display_text)

        else:
            # 未能解析为 PLAN 类型，回退到旧格式
            print(f"[Warning] Tool selection did not return PLAN type, falling back...")
            print("Falling back to old format...")

            # 回退到旧格式
            Refined = helper.extract_dictionary_from_response(response=plan_response)
            try:
                Selected_Tools_Dict = ast.literal_eval(Refined)
                selected_tools = Selected_Tools_Dict.get('Selected tool', [])
                if isinstance(selected_tools, str):
                    selected_tools = [selected_tools]
                # 保存简单格式的 plan
                self.session.set_plan(f"Selected tools: {selected_tools}")
            except Exception:
                selected_tools = []
                self.session.set_plan("Tool selection failed")

        self.tools_selected.emit(str(selected_tools))

        if not self._is_running:
            return

        # ====== 步骤 5: 文档检索 ======
        self.status_update.emit("正在检索工具文档...")

        # Phase 5: 使用提取的方法
        selected_tool_IDs_list, combined_documentation_str = \
            self._retrieve_tool_docs(selected_tools)

        # 存入 SessionContext（工具选择完成时）
        self.session.set_plan(json.dumps({
            "task_breakdown": task_breakdown,
            "selected_tools": selected_tools,
            "selected_tool_IDs": selected_tool_IDs_list,
        }, indent=2, ensure_ascii=False))
        self.session.add_message(
            "assistant",
            f"Selected tools: {selected_tools}\n"
            f"Tool IDs: {selected_tool_IDs_list}")

        if not self._is_running:
            return

        # ====== 步骤 6: 工作流图 ======
        self.status_update.emit("正在生成工作流图...")
        print('\n---------- AI IS GENERATING THE GEOPROCESSING'
              ' WORKFLOW FOR THE TASK ----------\n')

        script_directory = os.path.dirname(os.path.abspath(__file__))
        save_dir = os.path.join(script_directory, "graphs")
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)

        graph_file_path = os.path.join(save_dir, f"{task_name}.graphml")
        task_explanation = task_breakdown

        graph_response, code_for_graph, solution_graph_dict = \
            helper.generate_graph_response(
                request_id=self._request_id,
                task=self.task,
                task_explanation=task_explanation,
                data_path=data_overview,
                graph_file_path=graph_file_path,
                model_name=operation_model,
                stream=True,
                execute=True,
                reasoning_effort=self.reasoning_effort_value)

        html_graph_path = ""
        if solution_graph_dict and solution_graph_dict.get('graph'):
            G = solution_graph_dict['graph']
            nt = helper.show_graph(G)
            html_graph_path = os.path.join(
                save_dir, f"{task_name}_solution_graph.html")
            counter = 1
            while os.path.exists(html_graph_path):
                html_graph_path = os.path.join(
                    save_dir,
                    f"{task_name}_solution_graph_{counter}.html")
                counter += 1
            nt.save_graph(html_graph_path)
            print(f"GRAPH_SAVED:{html_graph_path}")
        else:
            print("Failed to generate or load solution graph")

        self.graph_ready.emit(html_graph_path)

        # ====== 保存分析结果 ======
        self._analysis_result = {
            "task_name": task_name,
            "task_breakdown": task_breakdown,
            "task_explanation": task_explanation,
            "selected_tools": selected_tools,
            "selected_tool_IDs": selected_tool_IDs_list,
            "combined_documentation_str": combined_documentation_str,
            "data_overview": data_overview,
            "html_graph_path": html_graph_path,
        }

        self.session.set_plan(json.dumps(self._analysis_result, indent=2, ensure_ascii=False, default=str))
        self.session.add_message(
            "assistant",
            f"分析完成。\n任务分解: {task_breakdown}\n"
            f"选用工具: {selected_tools}\n"
            f"工具 ID: {selected_tool_IDs_list}")

        # ---- 自动暂停，等用户确认 ----
        self.state = AgentState.PLAN_READY
        self.plan_ready.emit(self._analysis_result)

    def _run_execution(self):
        """
        执行阶段：对应 MyScript.py 的步骤 7
        代码生成 → 代码审查（可选）→ 代码执行 → 自动调试

        完成后自动暂停在 RESULT_READY，等待用户反馈。
        """
        import SpatialAnalysisAgent_helper as helper
        import SpatialAnalysisAgent_Constants as constants

        if not self._is_running:
            return

        # 从分析结果中获取参数
        task_breakdown = self._analysis_result.get(
            "task_breakdown", self.task)
        task_explanation = self._analysis_result.get(
            "task_explanation", task_breakdown)
        selected_tool_IDs_list = self._analysis_result.get(
            "selected_tool_IDs", [])
        combined_documentation_str = self._analysis_result.get(
            "combined_documentation_str", "")
        data_overview = self._analysis_result.get(
            "data_overview", self.data_path.split('\n'))

        # Phase 3: 图层快照功能已移除（简化 SessionContext）
        # snapshot_before = self.session.take_layer_snapshot()

        # ====== 步骤 7a: 生成代码 ======
        self.status_update.emit("正在生成代码...")

        # Phase 3: 使用新模式
        print('\n---------- AI IS GENERATING THE OPERATION CODE ----------\n')

        # 构建步骤指令
        step_instruction = helper.build_code_generation_instruction(
            task_description=task_breakdown,
            data_path='\n'.join([f"{idx + 1}. {line}" for idx, line in enumerate(data_overview)]),
            selected_tool=', '.join(selected_tool_IDs_list) if selected_tool_IDs_list else 'N/A',
            selected_tool_ID=', '.join(selected_tool_IDs_list) if selected_tool_IDs_list else 'N/A',
            documentation_str=combined_documentation_str
        )

        # 通过 SessionContext 组装上下文（会自动注入 current_plan）
        messages = self.session.build_messages(
            step="code_generation",
            step_instruction=step_instruction,
            step_role=constants.operation_role
        )

        print("GENERATED CODE:", end="")
        LLM_reply_str = helper.unified_llm_call(
            request_id=self._request_id,
            messages=messages,
            model_name=self.model_name,
            stream=True,
            reasoning_effort=self.reasoning_effort_value
        )

        # Phase 4: 使用风控门处理 LLM 输出
        action = self.process_llm_output(LLM_reply_str)

        if action.action_type == "show_code":
            extracted_code = action.data["code"]
            feedback = action.data["feedback_message"]

            print("\n ------------ GENERATED CODE ------------\n")
            print("```python")
            print(extracted_code)
            print("```")

            # 聊天框显示反馈消息（代码不进聊天框）
            self.status_update.emit(feedback)
        else:
            # 意料之外的输出类型（代码生成步骤应该总是返回代码）
            # 回退：使用旧的提取逻辑
            print("\n[Warning] Code generation did not return CODE type, falling back...")
            extracted_code = helper.extract_code_from_str(LLM_reply_str, self.task)
            print("\n ------------ GENERATED CODE ------------\n")
            print("```python")
            print(extracted_code)
            print("```")

        if not self._is_running:
            return

        # ====== 步骤 7b: 代码审查（可选）======
        if self.is_review:
            self.status_update.emit("正在审查代码...")
            print("\n ---- AI IS REVIEWING THE GENERATED CODE ----")

            # Phase 3: 使用新模式
            step_instruction = helper.build_code_review_instruction(
                extracted_code=extracted_code,
                data_path='\n'.join([f"{idx + 1}. {line}" for idx, line in enumerate(data_overview)]),
                selected_tools=', '.join(selected_tool_IDs_list) if selected_tool_IDs_list else 'N/A',
                documentation_str=combined_documentation_str
            )

            messages = self.session.build_messages(
                step="code_review",
                step_instruction=step_instruction,
                step_role=constants.operation_code_review_role
            )

            review_str = helper.unified_llm_call(
                request_id=self._request_id,
                messages=messages,
                model_name=self.model_name,
                stream=True,
                reasoning_effort=self.reasoning_effort_value
            )

            # Phase 4: 使用风控门处理审查输出
            review_action = self.process_llm_output(review_str)

            if review_action.action_type == "show_code":
                reviewed_code = review_action.data["code"]
            else:
                # 回退：使用旧的提取逻辑
                reviewed_code = helper.extract_code_from_str(review_str, task_explanation)

            print("\n\n")
            print("------------ REVIEWED CODE ------------\n")
            print("```python")
            print(reviewed_code)
            print("```")

            final_code = reviewed_code
            print("OPERATION CODE GENERATED AND REVIEWED SUCCESSFULLY")
        else:
            final_code = extracted_code

        # 通知 GUI 显示代码
        import urllib.parse
        print("CODE_READY_URLENCODED:" + urllib.parse.quote(final_code))
        self.code_ready.emit(final_code)

        if not self._is_running:
            return

        # ====== 步骤 7c: 执行代码（含自动调试）======
        self.status_update.emit("正在执行代码...")

        # Phase 3: 记录要执行的代码到 SessionContext
        self.session.add_executed_code(final_code)

        # Phase 3: 传入 SessionContext 以支持新的 debug 模式
        code, output, error_collector = helper.execute_complete_program(
            request_id=self._request_id,
            code=final_code,
            try_cnt=5,
            task=self.task,
            model_name=self.model_name,
            reasoning_effort_value=self.reasoning_effort_value,
            documentation_str=combined_documentation_str,
            data_path=self.data_path,
            workspace_directory=self.workspace_directory,
            review=self.is_review,
            stream=True,
            reasoning_effort=self.reasoning_effort_value,
            session_context=self.session)

        execution_success = (
            code is not None and len(code.strip()) > 0
            and (len(error_collector) == 0 or output))
        error_msg = ""
        if error_collector:
            error_msg = error_collector[-1].get("error_message", "")

        # Phase 3: 构建结果字符串并记录到 SessionContext
        result_parts = []
        result_parts.append(f"Status: {'SUCCESS' if execution_success else 'FAILED'}")
        if output:
            result_parts.append(f"Output:\n{output}")
        if error_msg:
            result_parts.append(f"Error: {error_msg}")

        result_str = "\n\n".join(result_parts)
        self.session.add_result(result_str)

        generated_code = code or final_code
        print("CODE_READY_URLENCODED2:" + urllib.parse.quote(generated_code))
        self.code_ready.emit(generated_code)

        if output:
            for line in output.splitlines():
                print(f"Output: {line}")

        # ---- 自动暂停，等用户反馈 ----
        result_info = {
            "code": generated_code,
            "success": execution_success,
            "output": output or "",
            "error_message": error_msg,
            "error_collector": error_collector,
            # Phase 3: data_changes 功能已移除（简化 SessionContext）
            "data_changes": {},
        }
        self.state = AgentState.RESULT_READY
        self.result_ready.emit(result_info)

        self._send_feedback_report(
            error_collector, generated_code, data_overview)

    def _run_plan_revision(self, user_feedback: str):
        """
        方案修改：用户反馈 + 原方案 + 会话上下文 → LLM 生成新方案
        完成后回到 PLAN_READY，用户再次确认。
        """
        import SpatialAnalysisAgent_helper as helper

        self.status_update.emit("正在修改方案...")

        original_plan = self._analysis_result
        original_breakdown = original_plan.get("task_breakdown", "")
        original_tools = original_plan.get("selected_tools", [])

        revision_prompt = (
            f"原始任务: {self.task}\n\n"
            f"原始方案分解:\n{original_breakdown}\n\n"
            f"原始选用工具: {original_tools}\n\n"
            f"用户反馈/修改意见:\n{user_feedback}\n\n"
            f"请根据用户的反馈修改方案。"
            f"输出修改后的任务分解（与原始格式相同），"
            f"并指明是否需要更换工具。"
            f"如果用户的修改只涉及参数调整"
            f"（如缓冲区距离），保持工具不变，只修改相关描述。"
        )

        session_context = self.session.get_summary()
        if session_context:
            revision_prompt = (
                f"会话上下文:\n{session_context}\n\n"
                f"{revision_prompt}")

        kwargs = self._get_reasoning_kwargs()

        revision_response = helper.unified_llm_call(
            request_id=self._request_id,
            messages=[
                {"role": "system",
                 "content": "你是一个 GIS 分析专家。"
                            "根据用户的修改意见调整分析方案。"},
                {"role": "user", "content": revision_prompt},
            ],
            model_name=self.model_name,
            stream=True,
            **kwargs)

        revised_plan = {
            **self._analysis_result,
            "task_breakdown": revision_response,
            "task_explanation": revision_response,
            "revision_note": f"根据用户反馈修改: {user_feedback}",
        }

        import json
        self.session.set_plan(json.dumps(revised_plan, indent=2, ensure_ascii=False, default=str))
        self._analysis_result = revised_plan
        self.session.add_message(
            "assistant",
            f"已根据您的意见修改方案:\n{revision_response}")

        self.state = AgentState.PLAN_READY
        self.plan_ready.emit(revised_plan)

    # ========================
    # Phase 5: _run_conversation 方法已被 _run_conversation_loop 替代
    # ========================
    # Phase 5: 辅助方法 — 文档检索
    # ========================

    def _retrieve_tool_docs(self, selected_tools: List[str]) -> tuple:
        """
        检索工具文档（从 _run_analysis 提取）。

        Args:
            selected_tools: 工具 ID 列表

        Returns:
            (selected_tool_IDs_list, combined_documentation_str)
        """
        import SpatialAnalysisAgent_Constants as constants
        import SpatialAnalysisAgent_Codebase as codebase
        import SpatialAnalysisAgent_ToolsDocumentation as ToolsDocumentation

        current_script_dir = os.path.dirname(os.path.abspath(__file__))
        Tools_Documentation_dir = os.path.join(
            current_script_dir, 'Tools_Documentation')

        selected_tool_IDs_list = []
        SelectedTools = {}
        all_documentation = []

        for selected_tool in selected_tools:
            if selected_tool in codebase.algorithm_names:
                stid = codebase.algorithms_dict[selected_tool]['ID']
            elif selected_tool in constants.tool_names_lists:
                stid = constants.CustomTools_dict[selected_tool]['ID']
            else:
                stid = selected_tool

            SelectedTools[selected_tool] = stid
            selected_tool_IDs_list.append(stid)
            stfid = re.sub(r'[ :?\/]', '_', stid)
            print(f"TOOL_ID: {stid}")

            found_path = None
            for root, dirs, files in os.walk(Tools_Documentation_dir):
                for file in files:
                    if file == f"{stfid}.toml":
                        found_path = os.path.join(root, file)
                        break
                if found_path:
                    break

            if not found_path:
                print(f"Tool documentation for {stfid}.toml is not provided")
                continue

            if ToolsDocumentation.check_toml_file_for_errors(found_path):
                doc_str = ToolsDocumentation.tool_documentation_collection(
                    tool_ID=stfid)
            else:
                print(f"File {stfid} has errors. Fixing...")
                ToolsDocumentation.fix_toml_file(found_path)
                doc_str = ToolsDocumentation.tool_documentation_collection(
                    tool_ID=stfid)

            all_documentation.append(doc_str)

        print(f"List of selected tool IDs: {selected_tool_IDs_list}")
        combined_documentation_str = '\n'.join(all_documentation)
        print(combined_documentation_str)

        return selected_tool_IDs_list, combined_documentation_str

    def _handle_plan_from_conversation(self, action: GuardGate.Action):
        """
        对话循环中检测到 PLAN → 激活状态机的 PLAN_READY。

        这里做的事：
        1. 解析 plan JSON，提取 tool_id 列表
        2. 检索 TOML 文档
        3. 保存到 SessionContext
        4. 发射 plan_ready 信号，进入 PLAN_READY 等待用户确认

        Args:
            action: GuardGate 返回的 Action 对象
        """
        import SpatialAnalysisAgent_helper as helper

        structured_plan = action.data["plan"]
        plan_display_text = action.data["plan_text"]

        # 提取工具列表
        selected_tools = helper.extract_tool_ids_from_plan(structured_plan)
        print(f"[ConversationLoop] Extracted tools from plan: {selected_tools}")

        # 检索文档
        if selected_tools:
            selected_tool_IDs_list, combined_documentation_str = \
                self._retrieve_tool_docs(selected_tools)
        else:
            selected_tool_IDs_list = []
            combined_documentation_str = ""

        # 保存到 session
        import json
        plan_text = json.dumps(structured_plan, indent=2, ensure_ascii=False)
        self.session.set_plan(plan_text)

        # 保存 _analysis_result 供执行阶段使用
        self._analysis_result = {
            "structured_plan": structured_plan,
            "selected_tools": selected_tools,
            "selected_tool_IDs": selected_tool_IDs_list,
            "combined_documentation_str": combined_documentation_str,
            "task_breakdown": structured_plan.get("task_summary", plan_display_text),
            "data_overview": self.session.data_overview,
        }

        # 进入 PLAN_READY
        self.state = AgentState.PLAN_READY
        self.plan_ready.emit(self._analysis_result)
        self.chat_response.emit(plan_display_text)

        print("[ConversationLoop] Entered PLAN_READY state")

    def _run_conversation_loop(self, message: str):
        """
        统一对话循环（在工作线程中运行）。

        Phase 5 核心方法：替代 _run_idle_input / _run_chat_reply /
        _run_conversation / _classify_intent。

        所有用户输入统一走这个流程：
        用户消息 → LLM → OutputParser → GuardGate → 路由

        Args:
            message: 用户输入的消息
        """
        import SpatialAnalysisAgent_helper as helper
        import SpatialAnalysisAgent_Constants as constants

        # 1. 记录用户消息
        self.session.add_message("user", message)

        # 2. 组装消息（统一用 build_messages）
        #    step="conversation" 让 SessionContext 注入数据概览、当前 plan、最近结果
        messages = self.session.build_messages(
            step="conversation",
            step_instruction=message,
            step_role=""  # 不传角色，使用 CONVERSATION_SYSTEM_PROMPT
        )

        # 3. 调用 LLM
        print("[ConversationLoop] Calling LLM...")
        response = helper.unified_llm_call(
            request_id=self._request_id or str(uuid.uuid4()),
            messages=messages,
            model_name=self.model_name,
            stream=True,
            **self._get_reasoning_kwargs()
        )

        # 4. OutputParser + GuardGate
        action = self.process_llm_output(response)
        print(f"[ConversationLoop] Detected output type: {action.action_type}")

        # 5. 根据 action 路由
        if action.action_type == "confirm_plan":
            # PLAN 类型 → 触发状态机的 PLAN_READY
            self.session.add_message("assistant", response)
            self._handle_plan_from_conversation(action)

        elif action.action_type == "show_code":
            # CODE 类型 → 进 CodeEditor
            self.session.add_message("assistant", response)
            self.code_ready.emit(action.data["code"])
            self.chat_response.emit(action.data["feedback_message"])
            # 状态不变（保持当前状态）
            print("[ConversationLoop] Code sent to CodeEditor")

        elif action.action_type == "confirm_knowledge":
            # 知识库更新建议
            self.session.add_message("assistant", response)
            self.handle_knowledge_update_action(action)
            # 状态不变
            print("[ConversationLoop] Knowledge update requested")

        else:
            # QUESTION / CHAT → 直接显示在对话面板
            self.session.add_message("assistant", response)
            self.chat_response.emit(action.data["message"])
            # 状态不变（保持当前状态，等用户继续输入）
            print(f"[ConversationLoop] {action.action_type.upper()} response sent to chat")

    # ========================
    # Phase 4: 输出处理（OutputParser + GuardGate）
    # ========================

    def process_llm_output(self, response: str) -> GuardGate.Action:
        """
        统一处理 LLM 输出：解析类型 → 风控门决策 → 返回 Action。

        由 AgentController 的各步骤调用，也由未来的对话循环调用。

        Args:
            response: LLM 的原始回复

        Returns:
            GuardGate.Action 对象，包含处理决策
        """
        parsed = self.output_parser.parse(response)
        action = self.guard_gate.decide(parsed)
        return action

    def handle_knowledge_update_action(self, action: GuardGate.Action):
        """
        处理知识库更新建议。

        由对话循环调用（Phase 5），当 AI 建议更新知识库时触发。
        本阶段只写逻辑，不接 UI。

        Args:
            action: GuardGate 返回的 Action 对象
        """
        suggestion = action.data.get("suggestion", "")

        # 发信号到 UI 层请求确认
        # （Phase 5 中实现信号连接）
        self.knowledge_update_requested.emit(suggestion)

    # ========================
    # 反馈发送
    # ========================

    def _send_feedback_report(self, error_collector,
                              generated_code, data_overview):
        try:
            import SpatialAnalysisAgent_helper as helper
            import requests

            api_key = helper.load_OpenAI_key()
            if 'gibd-services' not in (api_key or ''):
                return

            html_graph_path = self._analysis_result.get(
                "html_graph_path", "")
            try:
                html_content = helper.read_html_graph_content(
                    html_graph_path) if html_graph_path else ""
            except Exception:
                html_content = ""

            sel_tools = self._analysis_result.get("selected_tools", [])
            sel_str = (', '.join(sel_tools)
                       if isinstance(sel_tools, list) else str(sel_tools))
            tb = self._analysis_result.get("task_breakdown", "")

            url = f"https://www.gibd.online/api/feedback/{api_key}"
            feedback = {
                "service_name": "GIS Copilot",
                "question_id": self._request_id,
                "question": self.task,
                "error_msg": "Collected execution errors",
                "error_traceback": str(error_collector),
                "generated_code": generated_code,
                "data_overview": str(data_overview),
                "task_breakdown": tb,
                "selected_tools": sel_str,
                "workflow": html_content,
            }
            requests.post(url,
                          headers={"Content-Type": "application/json"},
                          json=feedback)
        except Exception as e:
            print(f"[AgentController] Failed to send feedback: {e}")

    # ========================
    # 公共方法
    # ========================

    def set_task_params(self, task: str, data_path: str,
                        workspace_directory: str, model_name: str,
                        is_review: bool, reasoning_effort_value: str):
        self.task = task
        self.data_path = data_path
        self.workspace_directory = workspace_directory
        self.model_name = model_name
        self.is_review = is_review
        self.reasoning_effort_value = reasoning_effort_value

    def reset(self):
        self._is_running = False
        if self._worker and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(3000)
        self.state = AgentState.IDLE
        self.session.reset()
        self._analysis_result = {}
        self._is_running = True

    def check_running(self) -> bool:
        return self._is_running