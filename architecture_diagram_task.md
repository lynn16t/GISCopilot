# 任务：生成 SpatialAnalysisAgent 插件完整技术架构图（单页 HTML）

## 目标

生成一个**单文件 HTML**（`architecture_diagram.html`），用交互式可视化展示 SpatialAnalysisAgent QGIS 插件的完整内部运行机制。要求所有流程**一步步衔接**，不要分散的独立图，而是一个连贯的、可滚动的全链路图。

## 重要：先读代码再画图

在画图之前，请先阅读以下核心文件的实际代码，确保图中内容与代码实现一致：

1. `SpatialAnalysisAgent_AgentController.py` — 状态机 + 流水线核心
2. `SpatialAnalysisAgent_dockwidget.py` — GUI + 动态按钮 + 信号连接
3. `SpatialAnalysisAgent_helper.py` — `unified_llm_call` + 各步骤函数
4. `SpatialAnalysisAgent_SessionContext.py` — 会话记忆数据容器
5. `SpatialAnalysisAgent_Constants.py` — 提示词模板
6. `SpatialAnalysisAgent_kernel.py` — Solution 类 + GraphML
7. `SpatialAnalysisAgent_MyScript.py` — 旧流水线（对照用）
8. `SpatialAnalysisAgent_ModelProvider.py` — API 路由（如果存在）

代码位置：项目根目录下的 `SpatialAnalysisAgent/` 文件夹。

---

## 图的整体结构（从上到下，一步步衔接）

### 第一层：用户输入入口
- 用户在统一输入框输入自然语言
- 显示当前状态（IDLE / CONVERSING / etc.）

### 第二层：意图分类（两层架构）
- **Layer 1 — 硬规则匹配（0ms）**
  - `_UI_OPERATION_PATTERNS` 正则匹配
  - 匹配到 → 直接调用 `iface.xxx()`，完全绕过 LLM
  - 约 10-15 个 UI 操作（如"打开属性表"、"缩放到图层"）
- **Layer 2 — LLM 轻量分类（1-3s）**
  - 运行在 `IntentClassifyThread`（QThread，不阻塞 GUI）
  - IDLE 状态下返回：`CHAT` / `GIS_TASK` / `UNCLEAR`
  - CONVERSING 状态下返回：`CHAT` / `PLAN_MODIFY` / `UNCLEAR`
  - `UNCLEAR` → QMessageBox 弹窗，默认选"聊天"
  - 关键判断逻辑：**祈使句式 → GIS_TASK，疑问句式 → CHAT**（空间名词如"缓冲区"不是可靠信号）
- 分流结果：
  - `CHAT` → 直接 LLM 对话回复，显示在聊天面板
  - `GIS_TASK` → 进入状态机，转 ANALYZING

### 第三层：状态机总览（6 个状态 + 转换）

用一个清晰的状态流转图展示：

```
IDLE → [用户发送 GIS 任务] → ANALYZING
ANALYZING → [自动完成] → PLAN_READY   ← 门控 1
PLAN_READY → [确认] → EXECUTING
PLAN_READY → [修改] → CONVERSING
PLAN_READY → [取消] → IDLE
EXECUTING → [自动完成] → RESULT_READY  ← 门控 2
RESULT_READY → [完成] → IDLE
RESULT_READY → [追问/报错] → CONVERSING
CONVERSING → [生成新方案] → PLAN_READY（闭环）
```

**核心规则高亮**：所有涉及代码执行的转换只能由用户按钮触发，AI 永远不能自行执行。

**动态按钮组**：每个状态对应的按钮要标注出来：
- PLAN_READY: 「确认执行」「我有修改」「取消」
- RESULT_READY: 「继续追问」「结果有误」「完成」
- ANALYZING / EXECUTING: 「中断」
- IDLE / CONVERSING: 无按钮

### 第四层：ANALYZING 阶段内部流水线（展开）

按步骤衔接展示 `AgentController._run_analysis()` 的 7 步：

| 步骤 | 名称 | 调用模块 | LLM? | 说明 |
|------|------|----------|------|------|
| 1 | 任务名生成 | `helper.generate_task_name` | LLM #1 | 生成简短英文任务名 |
| 2 | 数据概览 | `helper.add_data_overview` | 无 LLM | QgsVectorLayer 读取，缓存到 SessionContext |
| 3 | 查询调优/任务分解 | `helper.Query_tuning` | LLM #2 | 注入会话上下文 |
| 4 | 工具选择 | `helper.tool_select` | LLM #3 | 从 600+ QGIS 工具中选择 |
| 5 | TOML 文档检索 | `ToolsDocumentation` | 无 LLM | 从 `Tools_Documentation/QGIS_Tools/` 读取 TOML 文件 |
| 6 | GraphML 工作流生成 | `helper.generate_graph_response` | LLM #4 | 生成 NetworkX 图 + 执行 + 保存 `.graphml` |
| 7 | 方案输出 | → emit `plan_ready` signal | — | 自动转入 PLAN_READY |

每步之间用箭头衔接，并标注"知识注入点"——哪些步骤注入了 SessionContext / Project Knowledge / TOML docs。

### 第五层：知识注入系统（三大来源）

并排展示三个知识来源，用线连接到它们注入的 LLM 调用节点：

1. **SessionContext（会话记忆）**
   - 字段：`messages[]`（最近3轮完整，更早压缩）、`data_overview`（缓存）、`current_plan`（覆盖式）、`executed_codes[]`、`results[]`
   - 压缩策略：>60K tokens 时 LLM 压缩早期历史
   - 生命周期：整个会话

2. **Project Knowledge（用户文档）**
   - 支持格式：PDF（PyMuPDF）、DOCX（python-docx）、XLSX（openpyxl）、CSV、TXT
   - 上传时提取纯文本存储
   - 注入策略：关键词相关度评分（vs 图层名 + 当前查询），最大 6000 字符
   - 注入方式：原始文本直接注入（不经过 LLM 摘要）

3. **TOML 工具文档**
   - 位置：`Tools_Documentation/QGIS_Tools/*.toml`
   - 内容：tool_ID、参数说明、code_example
   - 600+ 文件，按工具选择结果精准检索

### 第六层：EXECUTING 阶段内部（展开）

| 步骤 | 名称 | 调用模块 | 说明 |
|------|------|----------|------|
| 1 | 代码生成 | `helper.generate_operation_code` | LLM #5，注入 TOML 文档 + 上下文 |
| 2 | 代码审查（可选） | `helper.code_review` | 可在 Settings 关闭 |
| 3 | 代码执行 | `exec(compiled_code)` | 在 QGIS Python 环境中执行 |
| 4 | 错误? → 自动调试 | `SmartDebugHelper` | 最多 5 次重试循环 |
| 5 | 成功 → 结果存入 SessionContext | — | emit `result_ready` signal |

重点展示 **5 次重试循环**：执行 → 错误 → traceback + 代码发给 LLM → 修复代码 → 重新执行 → ...

### 第七层：输出分类与路由（5 类）

展示 LLM 返回内容的 **regex/heuristic 解析**（不是 LLM 自标签），分流到不同面板：

| 类别 | 路由目标 | 说明 |
|------|----------|------|
| `CODE` | CodeEditor 面板 | 聊天框只显示"Python code generated" |
| `PLAN` | 触发 PLAN_READY | 结构化方案卡片展示 |
| `QUESTION` | 聊天框 | AI 反问用户 |
| `KNOWLEDGE_UPDATE` | 弹窗确认 → 写入 | 轻量知识更新 |
| `CHAT` | 聊天框 | 普通对话回复 |

**关键**：生成的代码永远不显示在聊天框中。

### 第八层：LLM 调用通道

展示 `unified_llm_call()` → `ModelProvider` 的路由：

- API Key 自动检测规则：
  - `sk-ant-...` → Anthropic (Claude)
  - `sk-...` (非 ant) → OpenAI 或 DeepSeek（试探 /models 端点）
  - `gibd-services-...` → GIBD 代理
  - `eyJ...` (JWT) → MiniMax
  - 其他 → 手动配置面板

- 模型列表随厂商动态刷新

- 流式响应 → `_ThreadStreamRedirector` 缓冲 → 只在 `\n` 时 emit 信号（防止逐 token 换行）

### 第九层：线程模型

展示 Qt 主线程 vs Worker QThread 的职责分离：

**主线程（GUI）**：
- DockWidget（聊天面板、代码面板、方案卡片）
- 动态按钮组
- Project Knowledge Tab（笔记编辑器 + 文档上传）
- Settings（API key 输入 + 自动检测）

**Worker QThread**：
- `AgentController`（状态机 + 流水线）
- `IntentClassifyThread`（意图分类）
- `ScriptThread`（旧版备份，一行回滚）
- `_ThreadStreamRedirector`（流式输出缓冲）

**信号连接**：用 `Qt.QueuedConnection`，标注关键信号：
- `plan_ready` → 更新按钮组 + 显示方案
- `result_ready` → 更新按钮组 + 显示结果
- `status_update` → 更新状态栏
- `code_ready` → 更新 CodeEditor

**线程纪律**（红色警告框）：
- 所有 GUI 操作必须在主线程
- Worker 中禁止调用 `repaint()`
- QThread.run() 中用 `sys.path.insert` + 无前缀 import

---

## 视觉风格要求

- **单页 HTML**，纯 CSS + JS，不依赖外部框架（可用 D3.js 从 CDN 加载）
- 整体从上到下连贯滚动，不要分散的独立图
- 配色方案：
  - 紫色系 — 人工门控点（PLAN_READY / RESULT_READY）
  - 蓝色系 — LLM 处理步骤
  - 琥珀/橙色系 — 意图分类 / 决策判断
  - 青色/绿色系 — 数据/知识/缓存
  - 灰色系 — 中性/空闲
  - 珊瑚/红色系 — 代码执行 / 调试 / 警告
- 每个节点/步骤之间有清晰的连接线和箭头
- 门控点要有醒目的视觉标识（如虚线边框或特殊图标）
- 支持 light/dark mode（用 CSS `prefers-color-scheme`）
- 可以做成可折叠/展开的分层结构（点击展开详细子步骤）
- 中文标注 + 英文技术术语

## 输出

- 文件名：`architecture_diagram.html`
- 放在项目根目录下
- 用浏览器直接打开即可查看，无需服务器
