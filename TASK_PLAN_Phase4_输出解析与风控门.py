"""
================================================================================
Phase 4 工作计划：输出解析 + 风控门
================================================================================

目标：
  新增 AgentOutputParser，用正则/启发式判断 LLM 输出的类型，
  然后根据类型路由到不同的处理逻辑（风控门）。
  同时调整代码输出行为：代码不进聊天框，直接进 CodeEditor 面板。

完成后效果：
  - LLM 输出被自动分类为 5 种类型
  - code 类型：代码进 CodeEditor，聊天框只显示一条反馈
  - plan 类型（含 plan update）：展示给用户确认
  - question 类型：聊天框显示，等用户补充
  - knowledge_update 类型：弹轻量确认，确认后写入知识库
  - chat 类型：聊天框直接显示
  - 为 Phase 5（对话循环）提供完整的输出处理管道

原则：
  - 不依赖 LLM 自己打标签，代码侧用正则/启发式判断
  - 不改变现有状态机逻辑，风控门是在状态机外围的附加层
  - 代码输出保持现有 CodeEditor 展示机制


================================================================================
Task 1：新建 SpatialAnalysisAgent_OutputParser.py
================================================================================

创建一个独立的输出解析模块。

class OutputType(Enum):
    CODE              = "code"
    PLAN              = "plan"                # 包含首次 plan 和 plan update
    QUESTION          = "question"
    KNOWLEDGE_UPDATE  = "knowledge_update"
    CHAT              = "chat"


class ParsedOutput:
    \"\"\"解析后的 LLM 输出。\"\"\"
    
    def __init__(self, output_type: OutputType, raw_response: str,
                 content: any = None):
        self.output_type = output_type
        self.raw_response = raw_response
        self.content = content      # 根据类型不同：
                                    #   CODE → 提取出的 python 代码字符串
                                    #   PLAN → 解析出的 dict (structured plan)
                                    #   QUESTION → 原始文本
                                    #   KNOWLEDGE_UPDATE → 建议写入的知识条目文本
                                    #   CHAT → 原始文本


class AgentOutputParser:
    \"\"\"
    用正则和启发式规则判断 LLM 输出的类型。
    不依赖 LLM 自己打标签。
    \"\"\"

    def parse(self, response: str) -> ParsedOutput:
        \"\"\"
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
        \"\"\"
        
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
            return ParsedOutput(OutputType.KNOWLEDGE_UPDATE, response, 
                              content=knowledge_text)
        
        # --- 4. QUESTION 检测 ---
        if self._is_question(response):
            return ParsedOutput(OutputType.QUESTION, response, content=response)
        
        # --- 5. 兜底：CHAT ---
        return ParsedOutput(OutputType.CHAT, response, content=response)

    # ------------------------------------------------------------------
    # CODE 检测
    # ------------------------------------------------------------------
    
    def _is_code(self, response: str) -> bool:
        \"\"\"检测是否包含 Python 代码块。\"\"\"
        return '```python' in response

    def _extract_code(self, response: str) -> str:
        \"\"\"从回复中提取 Python 代码。
        
        复用现有的 helper.extract_code_from_str() 逻辑。
        \"\"\"
        import re
        pattern = r'```python\s*(.*?)\s*```'
        matches = re.findall(pattern, response, re.DOTALL)
        if matches:
            return matches[0].strip()
        
        # 退路：尝试不带语言标记的代码块
        pattern2 = r'```\s*(.*?)\s*```'
        matches2 = re.findall(pattern2, response, re.DOTALL)
        if matches2:
            return matches2[0].strip()
        
        return response.strip()

    # ------------------------------------------------------------------
    # PLAN 检测
    # ------------------------------------------------------------------
    
    def _try_parse_plan(self, response: str) -> dict | None:
        \"\"\"
        尝试从回复中解析结构化 plan JSON。
        
        判断条件：回复中同时包含 "steps" 和 "tool_id"，
        并且能成功解析为 JSON。
        
        这个条件足够严格，不会把普通包含这些词的文本误判为 plan。
        
        plan update（用户说"改参数"后 AI 输出的修改版）和首次 plan
        用完全相同的 JSON 结构，所以不需要区分。
        \"\"\"
        if '"steps"' not in response or '"tool_id"' not in response:
            return None
        
        import json
        import re
        
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
        \"\"\"
        检测 AI 是否建议更新知识库。
        
        特征：同时满足以下条件
          - 包含知识库相关关键词
          - 包含建议/疑问语气
        \"\"\"
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
        \"\"\"
        从回复中提取建议写入知识库的内容。
        
        简单实现：返回整个回复文本。
        用户确认后，由 UI 层决定如何写入知识库。
        \"\"\"
        return response

    # ------------------------------------------------------------------
    # QUESTION 检测
    # ------------------------------------------------------------------
    
    def _is_question(self, response: str) -> bool:
        \"\"\"
        检测 AI 是否在提问/请求澄清。
        
        特征（满足任一）：
          - 以中文问号结尾
          - 以英文问号结尾
          - 包含明确的提问模式
        \"\"\"
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


================================================================================
Task 2：新建 SpatialAnalysisAgent_GuardGate.py
================================================================================

风控门逻辑，根据 ParsedOutput 类型路由到不同的处理方式。

class GuardGate:
    \"\"\"
    根据输出类型决定如何处理 LLM 的回复。
    
    本类不直接操作 UI，而是返回 Action 对象，
    由调用方（AgentController 或对话循环）根据 Action 执行。
    \"\"\"

    class Action:
        \"\"\"风控门的处理决策。\"\"\"
        def __init__(self, action_type: str, **kwargs):
            self.action_type = action_type
            # action_type 可选值：
            #   "show_code"         → 代码发到 CodeEditor，聊天框显示反馈
            #   "confirm_plan"      → 展示 plan，等待用户确认
            #   "confirm_knowledge" → 轻量确认，确认后写入知识库
            #   "show_message"      → 直接在聊天框显示
            self.data = kwargs
    
    def decide(self, parsed: ParsedOutput) -> Action:
        \"\"\"根据解析结果返回处理决策。\"\"\"
        
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
    
    def _format_plan_for_display(self, plan: dict) -> str:
        \"\"\"将结构化 plan JSON 格式化为用户可读的文本。\"\"\"
        lines = []
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


================================================================================
Task 3：在 AgentController 中集成 OutputParser 和 GuardGate
================================================================================

文件：SpatialAnalysisAgent_AgentController.py

--- 3a. 导入和初始化 ---

from SpatialAnalysisAgent_OutputParser import (
    AgentOutputParser, OutputType, ParsedOutput
)
from SpatialAnalysisAgent_GuardGate import GuardGate

在 __init__ 中：
    self.output_parser = AgentOutputParser()
    self.guard_gate = GuardGate()

--- 3b. 新增统一的输出处理方法 ---

def process_llm_output(self, response: str) -> GuardGate.Action:
    \"\"\"
    统一处理 LLM 输出：解析类型 → 风控门决策 → 返回 Action。
    
    由 AgentController 的各步骤调用，也由未来的对话循环调用。
    \"\"\"
    parsed = self.output_parser.parse(response)
    action = self.guard_gate.decide(parsed)
    return action

--- 3c. 在 _run_execution() 的代码生成步骤中使用 ---

现在的逻辑（大致）：
    response = unified_llm_call(messages=...)
    code = extract_code_from_str(response)
    # 直接执行或发到 CodeEditor

改为：
    response = unified_llm_call(messages=...)
    action = self.process_llm_output(response)
    
    if action.action_type == "show_code":
        code = action.data["code"]
        feedback = action.data["feedback_message"]
        
        # 代码发到 CodeEditor（通过信号）
        self.code_ready.emit(code)
        
        # 聊天框只显示反馈消息
        self.status_update.emit(feedback)
        
        # 记录到 SessionContext
        self.session_context.add_executed_code(code)
    else:
        # 意料之外的输出类型（代码生成步骤应该总是返回代码）
        # 回退：把整个回复当 chat 显示
        self.status_update.emit(action.data.get("message", response))

注意：在现有的 7 步流水线内部（_run_analysis / _run_execution），
输出类型是可预期的（Query Tuning 一定是 chat，Tool Selection 一定是 plan，
Code Generation 一定是 code）。所以 process_llm_output 在流水线内部
主要起验证/格式化的作用。

它真正发挥威力是在未来的对话循环（Phase 5）中，
用户的自由对话可能触发任意类型的输出。

--- 3d. 确保代码不进聊天框 ---

确认 code_ready 信号的处理方式：
  - 在 dockwidget.py 中，code_ready 信号连接到 update_code_editor()
  - update_code_editor() 只更新 CodeEditor 面板
  - 不要在 chatgpt_ans_textBrowser 中显示代码内容

如果现在的信号机制已经是这样工作的，就不需要改。
如果代码目前也在聊天框中显示，需要找到那个连接并移除。

检查方法：
  搜索 dockwidget.py 中所有 code_ready 信号的连接，
  确保只连到 CodeEditor 相关的 slot，不连到聊天框。


================================================================================
Task 4：在 AgentController 中处理 plan 展示
================================================================================

Tool Selection 步骤现在返回结构化 JSON plan。
用 process_llm_output 解析后，需要将格式化的 plan 文本展示给用户。

在 _run_analysis() 的 Tool Selection 步骤之后：

    response = unified_llm_call(messages=...)
    action = self.process_llm_output(response)
    
    if action.action_type == "confirm_plan":
        plan_dict = action.data["plan"]
        plan_display_text = action.data["plan_text"]
        
        # 保存到 SessionContext
        import json
        self.session_context.set_plan(json.dumps(plan_dict, ensure_ascii=False))
        
        # 在聊天框展示格式化的 plan
        self.status_update.emit(plan_display_text)
        
        # 从 plan 中提取工具 ID，用于后续 TOML 文档检索
        tool_ids = [step["tool_id"] for step in plan_dict.get("steps", [])]
        
        # 触发状态机的 PLAN_READY 状态（已有逻辑）
        self.plan_ready.emit(plan_display_text)
    
    else:
        # Tool Selection 没有返回 plan 格式
        # 回退到旧的解析逻辑
        # ...（使用 extract_dictionary_from_response 等旧函数）


================================================================================
Task 5：知识库写入确认的处理接口
================================================================================

为 Phase 5 的对话循环预留接口。当前阶段只需要写好处理函数，
不需要接 UI（因为在流水线模式里 AI 不会主动建议更新知识库）。

在 AgentController 中新增：

def handle_knowledge_update_action(self, action: GuardGate.Action):
    \"\"\"
    处理知识库更新建议。
    
    由对话循环调用（Phase 5），当 AI 建议更新知识库时触发。
    本阶段只写逻辑，不接 UI。
    \"\"\"
    suggestion = action.data.get("suggestion", "")
    
    # 发信号到 UI 层请求确认
    # （Phase 5 中实现信号连接）
    self.knowledge_update_requested.emit(suggestion)

在 AgentController 的信号定义中新增：
    knowledge_update_requested = pyqtSignal(str)


================================================================================
验证清单
================================================================================

[ ] SpatialAnalysisAgent_OutputParser.py 文件存在
[ ] OutputType 枚举包含 5 种类型：CODE, PLAN, QUESTION, KNOWLEDGE_UPDATE, CHAT
[ ] AgentOutputParser.parse() 能正确分类以下测试输入：
    - 包含 ```python 的回复 → CODE
    - 包含 {"steps": [...]} 的回复 → PLAN
    - 包含 "要不要添加到知识库" 的回复 → KNOWLEDGE_UPDATE
    - 以 "？" 结尾的回复 → QUESTION
    - 纯文字回复 → CHAT
[ ] SpatialAnalysisAgent_GuardGate.py 文件存在
[ ] GuardGate.decide() 对每种类型返回正确的 Action
[ ] GuardGate._format_plan_for_display() 能将 JSON plan 格式化为可读文本
[ ] AgentController 中存在 process_llm_output() 方法
[ ] Code Generation 步骤：代码进 CodeEditor，聊天框只显示反馈消息
[ ] Tool Selection 步骤：plan 被格式化展示
[ ] knowledge_update_requested 信号已定义
[ ] 现有流水线功能无回归（通过 AgentController 路径执行 GIS 任务仍正常）
[ ] MyScript.py 路径不受影响


================================================================================
文件变动总览
================================================================================

新建文件：
  SpatialAnalysisAgent_OutputParser.py
    - OutputType 枚举
    - ParsedOutput 数据类
    - AgentOutputParser 类

  SpatialAnalysisAgent_GuardGate.py
    - GuardGate 类
    - GuardGate.Action 类

修改文件：
  SpatialAnalysisAgent_AgentController.py
    - 导入 OutputParser 和 GuardGate
    - __init__: 创建 output_parser 和 guard_gate 实例
    - 新增: process_llm_output() 方法
    - 新增: handle_knowledge_update_action() 方法
    - 新增: knowledge_update_requested 信号
    - 修改: _run_execution() 代码生成步骤使用 process_llm_output()
    - 修改: _run_analysis() Tool Selection 步骤使用 process_llm_output()

  SpatialAnalysisAgent_dockwidget.py
    - 确认 code_ready 信号只连接 CodeEditor，不连接聊天框
    - 如有需要，移除代码在聊天框中的显示逻辑

不变文件：
  SpatialAnalysisAgent_SessionContext.py
  SpatialAnalysisAgent_helper.py
  SpatialAnalysisAgent_MyScript.py
  SpatialAnalysisAgent_KnowledgeManager.py
  SpatialAnalysisAgent_KnowledgeUI.py
  SpatialAnalysisAgent_Constants.py
"""
