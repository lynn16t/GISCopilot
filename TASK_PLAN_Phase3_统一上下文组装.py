"""
================================================================================
Phase 3 工作计划：统一上下文组装
================================================================================

目标：
  把散装的 prompt 拼装逻辑收拢到 SessionContext 一个地方。
  所有 LLM 调用通过 SessionContext.build_messages() 获取完整上下文，
  不再各自拼装。

完成后效果：
  - 每一步 LLM 调用自动获得知识库、数据概览、对话历史
  - 步骤间结果自动传递（AI 在生成代码时知道上一步选了什么工具、什么参数）
  - 静态内容跨调用不变，命中 DeepSeek prompt cache
  - 为后续对话循环（Phase 4+5）打好基础

原则：
  - 不删除旧函数，MyScript.py 保持向后兼容
  - AgentController 使用新函数，MyScript.py 使用旧函数
  - 不做 UI 改动


================================================================================
Task 1：重写 SpatialAnalysisAgent_SessionContext.py
================================================================================

如果 SessionContext 还嵌在 AgentController.py 里，先提取为独立文件。

核心设计：

class SessionContext:

    字段：
        messages: list          # 完整对话历史
        data_overview: list     # 数据概览原始列表
        data_overview_str: str  # 格式化后的数据概览字符串
        current_plan: str       # 仅保留 最新一份 plan（完整保留，不截断）
        executed_codes: list    # 已执行的代码列表
        results: list           # 执行结果列表（完整保留，不截断）
        task_name: str
        current_task: str       # 用户原始任务描述
        knowledge_manager       # 外部引用，用于获取知识库内容

    Token 预算（字符数，约 1:4 对应 token）：
        MAX_CONTEXT_CHARS = 24000   # 单次调用硬上限 ~6K tokens
        STATIC_BUDGET    = 8000     # 规则 + 知识库（无角色定义）
        HISTORY_BUDGET   = 8000     # 对话历史
        STEP_BUDGET      = 8000     # 步骤指令 + 工具文档

    核心方法：

    reset()
        新任务开始时清空所有状态。

    set_task(task)
        记录用户原始任务，追加到 messages。

    set_data_overview(data_overview)
        记录数据概览，缓存格式化字符串。

    set_plan(plan_text)
        覆盖 current_plan（只保留最新一份）。
        同时追加到 messages 以供历史回溯。

    add_executed_code(code)
        追加到 executed_codes 列表。
        追加到 messages。

    add_result(result)
        追加到 results 列表（完整保留，不截断）。
        追加到 messages。

    add_message(role, content)
        追加一条消息到对话历史。

    build_messages(step, step_instruction, step_role="") -> list
        组装完整 messages 列表，结构如下：

        [
          {"role": "system", "content": 静态部分 + 动态部分},
          ...对话历史（压缩后）...,
          {"role": "user", "content": 当前步骤指令}
        ]

    _build_system_message(step) -> str
        组装 system message，分为：

        === 静态部分（跨调用不变，可被 prompt cache 命中）===
        不设角色定义。直接从通用规则开始：
          - 通用 GIS/QGIS 编码规则（processing.run 用法、import 规范等）
          - 项目知识库相关条目（从 knowledge_manager 获取）

        === 动态部分 ===
          - 数据概览（data_overview_str）
          - 当前 plan（完整内容，仅在 code_generation / debug / plan_revision 步骤注入）
          - 最近执行结果（仅在 debug / chat / plan_revision 步骤注入，完整保留）

        两部分用 "---CONTEXT---" 标记分隔。

    _get_knowledge_text() -> str
        从 knowledge_manager 获取与当前图层/任务相关的知识文本。

    _get_compressed_history() -> list
        对话历史压缩策略：
          - 第一条消息（原始任务）：始终保留
          - 最近 3 轮交互（6 条消息）：完整保留
          - 中间消息的压缩规则：
            · [Plan] 标记的消息：删除（因为 current_plan 只保留最新的，旧 plan 无用）
            · [Result] 标记的消息：完整保留，不截断（用户可能引用任意一步的结果）
            · [Executed code] 标记的消息：只保留标记 "[Code was executed]"，删除代码体
            · 普通消息：超过 200 字符则截断


================================================================================
Task 2：在 helper.py 中新增步骤指令构建函数
================================================================================

这些函数只返回该步骤特有的指令文本。
角色、知识库、数据概览、对话历史由 SessionContext 统一注入。
旧函数（create_Query_tuning_prompt 等）全部保留，不修改。

--- 2a. Query Tuning 步骤指令 ---

def build_query_tuning_instruction(task: str) -> str:
    拼装：Query_tuning_prefix + requirements + instructions + User Query + Output Sample
    不包含：角色定义、数据概览、知识库（这些由 SessionContext 注入）

--- 2b. Tool Selection 步骤指令 ---

def build_tool_selection_instruction(task_breakdown: str) -> str:
    拼装：ToolSelect_prefix + task_breakdown + requirements + customized tools index + reply example

    ★ 重要变更：输出格式升级
    
    现在 Tool Selection 只输出工具名列表：
      {'Selected tool': ['native:buffer']}
    
    改为要求输出结构化执行计划（JSON），在 instruction 末尾追加输出格式要求：

    """
    You MUST respond in the following JSON format:
    {
      "steps": [
        {
          "step_number": 1,
          "operation": "简要描述该步操作",
          "tool_id": "native:extractbyattribute",
          "input_layer": "具体图层文件名",
          "key_parameters": {
            "FIELD": "DLBM",
            "OPERATOR": "begins with",
            "VALUE": "01"
          },
          "output_description": "输出结果描述"
        }
      ]
    }

    Rules:
    - input_layer must be an actual filename from the Data Overview
    - key_parameters should include only the most important 2-3 parameters
    - For chained operations, the next step's input_layer should reference
      the previous step's output
    -如果新增新字段，请从key_parameters里输出，或者在output description中描述清楚
    """

    这份结构化计划就是展示给用户确认的 plan，
    也是 Code Generation 步骤的输入蓝图。

--- 2c. Code Generation 步骤指令 ---

def build_code_generation_instruction(
    task_description: str,
    data_path: str, 
    selected_tool: str,
    selected_tool_ID: str,
    documentation_str: str
) -> str:
    拼装：operation_task_prefix + task_description + data_path 
          + selected_tool + documentation + requirements

    注意：current_plan（结构化执行计划）由 SessionContext 在 system message
    的动态部分自动注入，这里不需要重复包含。

--- 2d. Debug 步骤指令 ---

def build_debug_instruction(
    code: str,
    error_msg: str,
    documentation_str: str = ""
) -> str:
    拼装：debug_task_prefix + error_msg + failed_code + documentation + requirements
    使用 constants.get_smart_debug_requirements() 获取动态调试建议。


================================================================================
Task 3：在 Constants.py 中新增结构化工具选择的输出格式
================================================================================

文件：SpatialAnalysisAgent_Constants.py

新增常量：

structured_tool_selection_output_format = """
You MUST respond in the following JSON format only. No explanation outside JSON.
{
  "steps": [
    {
      "step_number": 1,
      "operation": "Brief description of this step",
      "tool_id": "algorithm_id e.g. native:buffer",
      "input_layer": "actual filename from Data Overview",
      "key_parameters": {
        "PARAM_NAME": "value"
      },
      "output_description": "What this step produces"
    }
  ]
}
"""

同时新增一个简单任务和复杂任务的 Output Sample，解决之前只有复杂示例
导致模型过度工程的问题：

structured_tool_selection_example_simple = """
{
  "steps": [
    {
      "step_number": 1,
      "operation": "Create 500m buffer around schools",
      "tool_id": "native:buffer",
      "input_layer": "schools.shp",
      "key_parameters": {
        "DISTANCE": 500,
        "SEGMENTS": 5
      },
      "output_description": "Buffer zones around all schools"
    }
  ]
}
"""

structured_tool_selection_example_complex = """
{
  "steps": [
    {
      "step_number": 1,
      "operation": "Filter counties with rainfall > 2.5 inches",
      "tool_id": "native:extractbyattribute",
      "input_layer": "PA_counties.shp",
      "key_parameters": {
        "FIELD": "annual_rainfall",
        "OPERATOR": ">",
        "VALUE": "2.5"
      },
      "output_description": "Counties meeting rainfall criteria"
    },
    {
      "step_number": 2,
      "operation": "Calculate area of selected counties",
      "tool_id": "native:fieldcalculator",
      "input_layer": "step_1_output",
      "key_parameters": {
        "FIELD_NAME": "area_sqkm",
        "FORMULA": "$area / 1000000"
      },
      "output_description": "Counties with calculated area field"
    }
  ]
}
"""


================================================================================
Task 4：重构 AgentController 使用 SessionContext.build_messages()
================================================================================

文件：SpatialAnalysisAgent_AgentController.py

--- 4a. __init__ 中创建 SessionContext ---

from SpatialAnalysisAgent_SessionContext import SessionContext
self.session_context = SessionContext(knowledge_manager=knowledge_manager)

--- 4b. 新任务开始时 reset ---

在用户点击发送按钮触发分析时：
    self.session_context.reset()
    self.session_context.set_task(task)

--- 4c. _run_analysis() 中各步骤改为新模式 ---

以 Query Tuning 为例：

  旧写法：
    prompt = helper.create_Query_tuning_prompt(task, data_overview, knowledge_text)
    result = helper.Query_tuning(request_id, prompt, model_name, stream=True)

  新写法：
    self.session_context.set_data_overview(data_overview)
    
    step_instruction = helper.build_query_tuning_instruction(task=task)
    messages = self.session_context.build_messages(
        step="query_tuning",
        step_instruction=step_instruction,
    )
    task_breakdown = helper.unified_llm_call(
        request_id=request_id,
        messages=messages,
        model_name=model_name,
        stream=True,
    )
    # 记录结果，供下一步使用
    self.session_context.add_message("assistant", task_breakdown)

  Tool Selection 步骤同理，额外需要：
    - 解析 LLM 返回的 JSON 结构化计划
    - 调用 self.session_context.set_plan(plan_json_str) 保存
    - 从 JSON 中提取工具 ID 列表用于后续 TOML 文档检索

  Code Generation 步骤同理，额外需要：
    - 调用 self.session_context.add_executed_code(code)

  Debug 步骤同理，额外需要：
    - 调用 self.session_context.add_result(result)

--- 4d. 解析结构化工具选择输出 ---

Tool Selection 现在返回 JSON 而不是简单的 dict。
需要一个解析函数（可以加在 helper.py 里）：

def parse_structured_plan(llm_response: str) -> dict:
    \"\"\"从 LLM 响应中提取结构化执行计划 JSON。\"\"\"
    # 清理 markdown 代码块标记
    text = llm_response.strip()
    if text.startswith('```json'):
        text = text[7:]
    if text.startswith('```'):
        text = text[3:]
    if text.endswith('```'):
        text = text[:-3]
    text = text.strip()
    
    import json
    plan = json.loads(text)
    return plan

def extract_tool_ids_from_plan(plan: dict) -> list:
    \"\"\"从结构化计划中提取工具 ID 列表。\"\"\"
    return [step["tool_id"] for step in plan.get("steps", [])]

解析失败时（LLM 没有按格式返回），回退到旧的 extract_dictionary_from_response() 逻辑。


================================================================================
Task 5：确保 MyScript.py 不受影响
================================================================================

不修改 MyScript.py 中的任何代码。
旧函数（create_Query_tuning_prompt、create_ToolSelect_prompt 等）保留在 helper.py 中。
MyScript.py 通过 ScriptThread 运行时走旧路径。
AgentController 走新路径。
两条路径并存。


================================================================================
验证清单
================================================================================

[ ] SpatialAnalysisAgent_SessionContext.py 作为独立文件存在
[ ] SessionContext.build_messages() 返回合法的 messages 列表
[ ] system message 中不包含角色定义（"You are ..."）
[ ] system message 静态部分包含通用规则 + 知识库
[ ] system message 动态部分包含数据概览
[ ] current_plan 只保留最新一份（set_plan 是覆盖不是追加）
[ ] Result 在历史压缩中完整保留，不截断
[ ] 旧 Plan 在历史压缩中被删除（因为只保留最新 plan）
[ ] helper.py 中存在 build_query_tuning_instruction()
[ ] helper.py 中存在 build_tool_selection_instruction()
[ ] helper.py 中存在 build_code_generation_instruction()
[ ] helper.py 中存在 build_debug_instruction()
[ ] helper.py 中存在 parse_structured_plan() 和 extract_tool_ids_from_plan()
[ ] Constants.py 中存在 structured_tool_selection_output_format
[ ] Constants.py 中存在简单 + 复杂两个结构化输出示例
[ ] AgentController._run_analysis() 使用 SessionContext.build_messages()
[ ] AgentController 每步 LLM 调用后都记录结果到 SessionContext
[ ] MyScript.py 未被修改，仍可正常运行
[ ] 插件在 QGIS 中加载无报错
[ ] 通过 AgentController 路径执行一个 GIS 任务，能完整走完
[ ] Output Window 中打印的 prompt 包含 "Project Knowledge" 和 "Data Overview"
[ ] Tool Selection 返回结构化 JSON（而非仅工具名列表）


================================================================================
文件变动总览
================================================================================

新建文件：
  SpatialAnalysisAgent_SessionContext.py — 完整 SessionContext 类

修改文件：
  SpatialAnalysisAgent_helper.py
    新增：build_query_tuning_instruction()
    新增：build_tool_selection_instruction()
    新增：build_code_generation_instruction()
    新增：build_debug_instruction()
    新增：parse_structured_plan()
    新增：extract_tool_ids_from_plan()
    保留：所有旧函数不变

  SpatialAnalysisAgent_Constants.py
    新增：structured_tool_selection_output_format
    新增：structured_tool_selection_example_simple
    新增：structured_tool_selection_example_complex

  SpatialAnalysisAgent_AgentController.py
    修改：__init__() 创建 SessionContext
    修改：_run_analysis() 使用 build_messages()
    修改：_run_execution() 使用 build_messages()
    新增：start_new_task() 方法

不变文件：
  SpatialAnalysisAgent_MyScript.py
  SpatialAnalysisAgent_KnowledgeManager.py
  SpatialAnalysisAgent_KnowledgeUI.py
  SpatialAnalysisAgent_dockwidget.py（本阶段无 UI 改动）
"""
