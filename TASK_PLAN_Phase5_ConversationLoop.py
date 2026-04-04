"""
================================================================================
Phase 5 任务计划：对话循环架构（Conversation Loop）
================================================================================

目标：
  状态机从顶层控制器降级为对话循环的子模块。
  用户的每条消息统一进入 LLM → OutputParser 判断回复类型 → GuardGate 路由。
  PLAN 类型才触发状态机，其他类型直接在对话面板处理。

核心改动：
  1. 新建 CONVERSATION_PROMPT — 对话循环的统一 system prompt（替代所有 role 定义）
  2. 改造 handle_text_input() — 所有状态统一走对话循环入口
  3. 干掉 _classify_intent() — 不再需要单独的意图分类调用
  4. 改造 _run_analysis() — 从对话循环内部被触发，而非顶层入口
  5. 精简 Constants.py 中的 role 定义 — 去角色扮演，只留具体规则

================================================================================
Task 1：CONVERSATION_PROMPT — 对话循环的统一 system prompt
================================================================================

位置：Constants.py 新增

这是整个对话循环的核心 prompt，替代现有的：
  - _IDLE_INTENT_PROMPT（AgentController 里的意图分类 prompt）
  - _CONV_INTENT_PROMPT（AgentController 里的对话意图分类 prompt）
  - _run_chat_reply() 里的内联 system prompt
  - Query_tuning_role（Constants.py）
  - ToolSelect_role（Constants.py）

设计原则：
  - 不用角色扮演（"You are a 20-year GIS expert..."），只给具体规则和约束
  - 明确告诉 LLM 什么时候该输出 PLAN JSON，什么时候该提问
  - 用 OutputParser 已有的结构特征（```python / "steps"+"tool_id"）作为输出约定

新增常量如下：
"""

CONVERSATION_SYSTEM_PROMPT = """You are a spatial analysis assistant integrated into QGIS.
You help users with GIS tasks through natural conversation.

=== HOW YOU RESPOND ===

You have several response modes. Choose the right one based on the situation:

1. STRUCTURED PLAN — When you have enough information to plan a GIS operation:
   Output a JSON execution plan with this exact structure:
   ```json
   {
     "task_summary": "Brief description of what will be done",
     "steps": [
       {
         "step_number": 1,
         "operation": "What this step does",
         "tool_id": "native:buffer",
         "input_layer": "layer_name",
         "key_parameters": {"DISTANCE": 500},
         "output_description": "What this step produces"
       }
     ]
   }
   ```
   Include preprocessing steps (e.g., reprojection) directly in the plan
   without asking — the user will review before execution.

2. CLARIFYING QUESTION — When critical information is missing or ambiguous.
   Ask a clear, specific question. You MUST ask before planning when:

   a) NO DATA LOADED: The user wants to run analysis but no layers are loaded.
      → "I don't see any loaded layers. Please load your data first using
         the Load Data button, then describe what you'd like to do."

   b) DATA MISMATCH: The user references data that doesn't match loaded layers.
      → "You mentioned [X], but the loaded layers are: [list].
         Which layer should I use, or do you need to load different data?"

   c) OPERATION-TYPE CONFLICT: The requested operation requires a different
      data type than what's available (e.g., raster operation on vector data).
      → "This operation requires raster data, but [layer] is a vector layer.
         Would you like me to convert it first, or do you have raster data
         to load?"

   d) VECTOR-RASTER CONVERSION: The task implicitly requires converting
      between vector and raster formats.
      → "To do [X], I would need to convert [layer] from vector to raster
         (rasterization). Should I include this conversion in the plan?"

   e) AMBIGUOUS PARAMETERS: Key parameters are missing and cannot be
      reasonably defaulted (e.g., buffer distance, classification field).
      → "What buffer distance would you like to use?"
      But do NOT ask about parameters that have obvious defaults
      (e.g., output CRS = same as input).

   f) AMBIGUOUS TASK: The user's description could mean multiple different
      GIS operations.
      → Describe the alternatives briefly and ask which one they mean.

3. CONVERSATIONAL REPLY — For questions, greetings, concept explanations,
   or any input that does not require GIS analysis execution.
   Reply naturally in the same language the user is using.

=== WHEN NOT TO ASK ===

Do NOT ask about:
- Preprocessing that is standard practice (reprojection, fixing geometries).
  Include these as steps in your plan.
- Parameters with sensible defaults. Use the default and note it in the plan.
- Output format/location. Default to temporary layers.
- Whether to proceed — that's what the plan confirmation UI is for.

=== LANGUAGE ===

Reply in the same language the user writes in. If they write in Chinese,
reply in Chinese (but keep tool_id and parameter names in English).
"""

# -----------------------------------------------------------------------
# 注意：上面的 prompt 会被放在 SessionContext.build_messages() 的 system
# message 静态部分，通用 GIS 规则和项目知识库紧跟其后。
# step_role 参数不再传入角色扮演文本。
# -----------------------------------------------------------------------


"""
================================================================================
Task 2：改造 handle_text_input() — 统一对话循环入口
================================================================================

位置：AgentController.py

现在的逻辑（要被替换的）：

    def handle_text_input(self, message):
        if state == IDLE:       → SEND_TASK → _handle_new_task → _classify_intent
        if state == PLAN_READY: → MODIFY_PLAN → _run_plan_revision
        if state == RESULT_READY: → NEW_ANALYSIS → _run_analysis
        if state == CONVERSING:   → SEND_MESSAGE → _run_conversation → _classify_intent

问题：每个状态走不同的路径，有两处 _classify_intent 调用（双重 token 消耗）。

改成的逻辑：

    def handle_text_input(self, message):
        if state in (ANALYZING, EXECUTING):
            → "正在处理中，请稍候..."
            return

        # 所有其他状态统一走对话循环
        self._start_worker(self._run_conversation_loop, message)

所有状态（IDLE / PLAN_READY / RESULT_READY / CONVERSING）都走同一个入口。
按钮动作（确认执行、取消等）仍然通过 handle_user_action() 直接触发状态转换。

核心方法 _run_conversation_loop() 伪代码：
"""

def _run_conversation_loop(self, message: str):
    """
    统一对话循环（在工作线程中运行）。
    替代 _run_idle_input / _run_chat_reply / _run_conversation / _classify_intent。
    """
    # 1. 记录用户消息
    self.session.add_message("user", message)

    # 2. 组装消息（统一用 build_messages）
    #    step="conversation" 让 SessionContext 注入数据概览、当前 plan、最近结果
    messages = self.session.build_messages(
        step="conversation",
        step_instruction=message,
        step_role=""  # 不传角色，统一用 CONVERSATION_SYSTEM_PROMPT
    )

    # 3. 调用 LLM
    response = helper.unified_llm_call(
        request_id=self._request_id,
        messages=messages,
        model_name=self.model_name,
        stream=True,
        **self._get_reasoning_kwargs()
    )

    # 4. OutputParser + GuardGate
    action = self.process_llm_output(response)

    # 5. 根据 action 路由
    if action.action_type == "confirm_plan":
        # PLAN 类型 → 触发状态机的 PLAN_READY
        self._handle_plan_from_conversation(action)

    elif action.action_type == "show_code":
        # CODE 类型 → 进 CodeEditor
        self.session.add_message("assistant", response)
        self.code_ready.emit(action.data["code"])
        self.chat_response.emit(action.data["feedback_message"])
        # 状态不变（保持当前状态）

    elif action.action_type == "confirm_knowledge":
        # 知识库更新建议
        self.session.add_message("assistant", response)
        self.handle_knowledge_update_action(action)

    else:
        # QUESTION / CHAT → 直接显示在对话面板
        self.session.add_message("assistant", response)
        self.chat_response.emit(action.data["message"])
        # 状态不变（保持当前状态，等用户继续输入）


def _handle_plan_from_conversation(self, action):
    """
    对话循环中检测到 PLAN → 激活状态机的 PLAN_READY。

    这里需要做的事：
    1. 解析 plan JSON，提取 tool_id 列表
    2. 检索 TOML 文档（和 _run_analysis 步骤 5-6 一样）
    3. 生成工作流图（可选，如果需要的话）
    4. 保存到 SessionContext
    5. 发射 plan_ready 信号，进入 PLAN_READY 等待用户确认
    """
    structured_plan = action.data["plan"]
    plan_display_text = action.data["plan_text"]

    # 提取工具列表 + 检索文档
    selected_tools = helper.extract_tool_ids_from_plan(structured_plan)
    documentation_str = self._retrieve_tool_docs(selected_tools)

    # 保存到 session
    import json
    plan_text = json.dumps(structured_plan, indent=2, ensure_ascii=False)
    self.session.set_plan(plan_text)

    # 保存 _analysis_result 供执行阶段使用
    self._analysis_result = {
        "structured_plan": structured_plan,
        "selected_tools": selected_tools,
        "documentation_str": documentation_str,
        "task_breakdown": plan_display_text,
    }

    # 进入 PLAN_READY
    self.state = AgentState.PLAN_READY
    self.plan_ready.emit(self._analysis_result)
    self.chat_response.emit(plan_display_text)


"""
================================================================================
Task 3：SessionContext 适配 — conversation 步骤 + 静态 prompt 注入
================================================================================

位置：SessionContext.py

需要改动的地方：

3a. _build_system_message() 中增加 "conversation" 步骤的处理
    - conversation 步骤注入：数据概览 + 当前 plan + 最近结果（全部注入）
    - 因为对话循环需要 LLM 了解完整上下文才能判断是否该生成 PLAN

3b. CONVERSATION_SYSTEM_PROMPT 注入位置
    - 替代现有的 step_role 参数
    - 放在 system message 的静态部分最前面
    - 后面紧跟 _get_general_rules() 和知识库

3c. 增加 "conversation" 到步骤白名单
    - 在 _build_system_message 的条件判断中，conversation 步骤
      需要注入 plan 和 results（因为 LLM 需要知道当前上下文）

代码改动：
"""

# SessionContext._build_system_message 改动：

def _build_system_message_v2(self, step: str, step_role: str = "") -> str:
    parts = []
    static_parts = []

    # 1. 对话循环使用统一 prompt（不再传 step_role）
    if step == "conversation":
        # from Constants import CONVERSATION_SYSTEM_PROMPT
        static_parts.append(CONVERSATION_SYSTEM_PROMPT)
    elif step_role:
        # 其他步骤（code_generation, debug 等）仍可用步骤专属指令
        static_parts.append(step_role)

    # 2. 通用规则 + 知识库（不变）
    static_parts.append(self._get_general_rules())
    knowledge = self._get_knowledge_text()
    if knowledge:
        static_parts.append(f"=== Project Knowledge ===\n{knowledge}")

    parts.append("\n\n".join(static_parts))

    # 3. 动态部分
    parts.append("---CONTEXT---")
    dynamic_parts = []

    if self.data_overview_str:
        dynamic_parts.append(f"=== Loaded Data ===\n{self.data_overview_str}")

    # conversation 步骤：注入 plan + results（LLM 需要完整上下文）
    if step in ["conversation", "code_generation", "debug",
                "plan_revision", "chat"] and self.current_plan:
        dynamic_parts.append(f"=== Current Plan ===\n{self.current_plan}")

    if step in ["conversation", "debug", "chat",
                "plan_revision"] and self.results:
        recent_results = self.results[-3:]
        results_text = "\n\n".join(recent_results)
        dynamic_parts.append(f"=== Recent Results ===\n{results_text}")

    if dynamic_parts:
        parts.append("\n\n".join(dynamic_parts))

    return "\n\n".join(parts)


"""
================================================================================
Task 4：删除 / 精简旧代码
================================================================================

以下方法/常量在 Phase 5 中被替代，可以删除或标记 deprecated：

AgentController.py 中删除：
  - _IDLE_INTENT_PROMPT（类属性）
  - _CONV_INTENT_PROMPT（类属性）
  - _classify_intent()（整个方法，~60 行）
  - _run_idle_input()（整个方法，被 _run_conversation_loop 替代）
  - _run_chat_reply()（整个方法，被 _run_conversation_loop 替代）
  - _run_conversation()（整个方法，被 _run_conversation_loop 替代）
  - _handle_new_task() 中的分类逻辑（简化为直接调用对话循环）

Constants.py 中精简：
  - Query_tuning_role → 删除角色扮演部分，只保留具体指令
  - ToolSelect_role → 同上
  - OperationIdentification_role → 同上
  - graph_role → 同上
  - operation_role → 同上
  - debug_role → 同上
  - IDLE_INTENT_CLASSIFY_PROMPT → 删除（helper.py 中的旧分类 prompt）
  - CONVERSING_INTENT_CLASSIFY_PROMPT → 删除

保留不动的：
  - handle_user_action()（按钮路由，不变）
  - _handle_confirm_plan()（确认执行，不变）
  - _handle_modify_plan()（方案修改，改为调用 _run_conversation_loop）
  - _run_analysis()（分析 pipeline，仍可被调用，但不再是默认入口）
  - _run_execution()（执行 pipeline，不变）
  - _run_plan_revision()（方案修改，仍可保留作为快速修改路径）
  - process_llm_output()（Phase 4 的核心，直接复用）
  - OutputParser + GuardGate（直接复用）
  - MyScript.py（保留作为回退）


================================================================================
Task 5：Constants.py 提示词精简
================================================================================

原则：去掉角色扮演，只留具体规则和约束。

改之前（以 Query_tuning_role 为例）：
  "You are a GIS expert. Convert the following user request into
   a short GIS task description."

改之后：
  删除这个 role，指令直接放在 step_instruction 中（user message）。

改之前（以 ToolSelect_role 为例）：
  "A professional Geo-information scientist with high proficiency
   in Geographic Information System (GIS) operations. You also have
   excellent proficiency in QGIS to perform GIS operations..."

改之后：
  删除这个 role。工具选择的具体指令放在 build_tool_selection_instruction()
  的 step_instruction 中。

改之前（以 operation_role 为例）：
  "A professional Geo-information scientist with high proficiency
   in GIS operations..."

改之后（精简为规则列表）：
"""

OPERATION_RULES = """
- Use processing.run() for all QGIS Processing algorithms
- Import from qgis.core; import processing
- Use absolute file paths
- Check layer validity before processing
- Use QgsVectorLayer for loading shapefiles, not geopandas
- QVariant: import from PyQt5.QtCore, not qgis.core
- Use QgsVectorLayerJoinInfo instead of QgsVectorJoinInfo
- Raster Calculator: correct ID is 'native:rastercalc', not 'native:rastercalculator'
- For existing output files: overwrite, do not error
- One python code block only, no explanation text outside the code block
"""

# 这些规则合并到 SessionContext._get_general_rules() 中，
# 代替散落在各个 role 里的重复规则。


"""
================================================================================
Task 6：_run_analysis 的角色变化
================================================================================

_run_analysis() 不再是用户输入的默认入口。

它的新角色是：当对话循环产生的 PLAN 被用户确认后，如果执行阶段
需要更详细的工具文档和工作流图，可以在 _handle_plan_from_conversation()
内部按需调用 _run_analysis 的子步骤（文档检索、工作流图生成）。

或者更简单的方案：对话循环里 LLM 直接输出 PLAN JSON，
_handle_plan_from_conversation 只做文档检索（步骤 5）和工作流图（步骤 6），
跳过步骤 1-4（因为对话循环已经完成了任务理解和工具选择）。

步骤 1（任务名生成）→ 可以在 _handle_plan_from_conversation 里轻量调用
步骤 2（数据概览）→ 在对话循环的 system message 里已经注入
步骤 3（任务分解）→ 对话循环的 LLM 回复本身就是任务分解
步骤 4（工具选择）→ PLAN JSON 里已经包含 tool_id
步骤 5（文档检索）→ 需要保留，给代码生成阶段用
步骤 6（工作流图）→ 可选保留

所以 _handle_plan_from_conversation 实际上只跑步骤 5 和 6。


================================================================================
Task 7：handle_user_action 按钮路由的微调
================================================================================

按钮动作不走对话循环，仍然直接触发状态转换。
但部分按钮行为需要微调：

MODIFY_PLAN（PLAN_READY 状态下用户输入文字）：
  改之前：直接调 _run_plan_revision
  改之后：走对话循环。用户的修改意见作为普通消息进 _run_conversation_loop，
         LLM 看到当前 plan 上下文后，输出修改后的 PLAN JSON，
         OutputParser 识别为 PLAN，重新进入 PLAN_READY。

CONFIRM_PLAN：不变。直接 PLAN_READY → EXECUTING → _run_execution()。
CANCEL：不变。→ IDLE。
FINISH：不变。→ IDLE。
INTERRUPT：不变。→ IDLE。

NEW_ANALYSIS（RESULT_READY 状态下用户输入文字）：
  改之前：直接调 _run_analysis
  改之后：走对话循环。用户的新需求作为消息进 _run_conversation_loop，
         LLM 根据上下文（包含已有结果）判断是输出新 PLAN 还是追问。

REPORT_ERROR（RESULT_READY 状态下）：
  改之前：走 _run_conversation → _classify_intent
  改之后：走对话循环。用户描述错误，LLM 看到结果上下文后，
         可能输出修改后的 PLAN，或者追问具体哪里有问题。


================================================================================
数据流总览（改造后）
================================================================================

用户发消息
    │
    ▼
handle_text_input()
    │
    ├── state = ANALYZING/EXECUTING → "正在处理中..."
    │
    └── 其他状态 → _run_conversation_loop(message)
                        │
                        ▼
                  SessionContext.build_messages(step="conversation")
                        │
                        ▼
                  LLM 调用（带完整上下文：数据概览 + plan + results）
                        │
                        ▼
                  OutputParser.parse(response)
                        │
                        ▼
                  GuardGate.decide(parsed)
                        │
                ┌───────┼───────┬──────────┬───────────┐
                ▼       ▼       ▼          ▼           ▼
              PLAN   QUESTION  CHAT      CODE    KNOWLEDGE
                │       │       │          │           │
                ▼       │       │          │           │
        检索TOML文档    │       │          │           │
        生成工作流图    │       │          │           │
                │       │       │          │           │
                ▼       ▼       ▼          ▼           ▼
          PLAN_READY  显示在   显示在    CodeEditor  确认后写入
          [确认][修改]  对话面板  对话面板   面板       知识库
          [取消]


================================================================================
验收清单
================================================================================

[ ] CONVERSATION_SYSTEM_PROMPT 已添加到 Constants.py
[ ] _run_conversation_loop() 已实现并替代旧的分类+路由逻辑
[ ] handle_text_input() 所有非阻塞状态统一走 _run_conversation_loop
[ ] _classify_intent() 已删除
[ ] _run_idle_input() 已删除
[ ] _run_chat_reply() 已删除
[ ] _run_conversation() 已删除
[ ] SessionContext.build_messages() 支持 step="conversation"
[ ] CONVERSATION_SYSTEM_PROMPT 注入到 system message 静态部分
[ ] _handle_plan_from_conversation() 实现文档检索 + 工作流图
[ ] Constants.py 中 6 个 role 变量精简为规则列表（无角色扮演）
[ ] _get_general_rules() 合并所有通用编码规则（不重复）
[ ] 按钮动作（CONFIRM_PLAN / CANCEL / FINISH / INTERRUPT）仍直接触发
[ ] MODIFY_PLAN / NEW_ANALYSIS / REPORT_ERROR 改走对话循环
[ ] QUESTION 场景覆盖：
    [ ] 无数据加载 → 反问
    [ ] 数据不匹配 → 反问
    [ ] 操作与数据类型冲突 → 反问
    [ ] 矢量↔栅格转换 → 反问确认
    [ ] 关键参数缺失 → 反问
    [ ] 歧义任务 → 反问
[ ] 预处理步骤（投影变换等）→ 直接包含在 plan 中，不反问
[ ] MyScript.py 路径不受影响（保留回退）
[ ] 现有 _run_execution() 逻辑不受影响


================================================================================
文件改动总览
================================================================================

修改文件：
  SpatialAnalysisAgent_Constants.py
    - 新增: CONVERSATION_SYSTEM_PROMPT
    - 精简: Query_tuning_role → 删除角色扮演，只留任务指令
    - 精简: ToolSelect_role → 同上
    - 精简: operation_role → 同上
    - 精简: graph_role → 同上
    - 精简: debug_role → 同上
    - 精简: operation_code_review_role → 同上
    - 删除: IDLE_INTENT_CLASSIFY_PROMPT
    - 删除: CONVERSING_INTENT_CLASSIFY_PROMPT

  SpatialAnalysisAgent_AgentController.py
    - 删除: _IDLE_INTENT_PROMPT（类属性）
    - 删除: _CONV_INTENT_PROMPT（类属性）
    - 删除: _classify_intent()
    - 删除: _run_idle_input()
    - 删除: _run_chat_reply()
    - 删除: _run_conversation()
    - 改造: handle_text_input() → 统一走对话循环
    - 改造: _handle_new_task() → 简化，不再分类
    - 改造: _handle_modify_plan() → 走对话循环
    - 改造: _handle_new_analysis() → 走对话循环
    - 新增: _run_conversation_loop()
    - 新增: _handle_plan_from_conversation()
    - 新增: _retrieve_tool_docs()（从 _run_analysis 提取的文档检索子方法）

  SpatialAnalysisAgent_SessionContext.py
    - 改造: _build_system_message() → 支持 conversation 步骤
    - 改造: _get_general_rules() → 合并所有通用规则（去重）

不变文件：
  SpatialAnalysisAgent_OutputParser.py（直接复用）
  SpatialAnalysisAgent_GuardGate.py（直接复用）
  SpatialAnalysisAgent_MyScript.py（保留回退）
  SpatialAnalysisAgent_helper.py（build_*_instruction 函数保留，
    仍被 _run_analysis / _run_execution 内部使用）
  SpatialAnalysisAgent_KnowledgeManager.py
  SpatialAnalysisAgent_KnowledgeUI.py
  SpatialAnalysisAgent_ModelProvider.py
"""
