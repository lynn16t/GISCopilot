# Spatial Analysis Agent

QGIS 插件,把大语言模型变成你的 GIS 副驾驶。用自然语言描述要做的空间分析任务,
插件自动选工具、生成代码、执行,并把结果加载回 QGIS。

适合两类用户:
- **GIS 初学者** —— 不必记 QGIS 工具箱里 600+ 算法的参数名,自然语言就能跑分析
- **熟练用户** —— 把重复的样板代码交给 AI,自己专注在思路和参数取舍上

---

## 主要特性

- **自然语言 → QGIS Processing 代码**:一句话描述需求,AI 拆解任务、选工具、写
  Python 代码、调 `processing.run` 执行
- **600+ QGIS 算法 + Python 生态**:不限于 QGIS 自带工具,可以混用 geopandas /
  numpy / seaborn 等第三方库
- **状态机驱动**:六状态显式调度(`IDLE → ANALYZING → PLAN_READY → EXECUTING
  → RESULT_READY → CONVERSING`),关键转换由用户按钮确认,AI 不能自主执行代码
- **运行时签名校验**:直接问 `QgsApplication.processingRegistry()` 拿当前
  QGIS 真实接受的参数,绕过文档滞后导致的"假错误"
- **多厂商模型支持**:
  - OpenAI(GPT-5.2 / GPT-5.1 / GPT-5 / GPT-4o / o3-mini)
  - Anthropic Claude(Opus 4.7 / Sonnet 4.6 / Haiku 4.5)
  - Google Gemini(2.5 / 3.1 Pro / Flash)
  - DeepSeek / MiniMax / OpenRouter(可一键访问以上所有)
  - 本地 Ollama(支持 llama / qwen / gpt-oss 等)
- **RAG 工具检索**:用 ONNX + all-MiniLM-L6-v2 在 600+ 工具里做向量检索,
  比固定 prompt 更准
- **SmartDebugger 自动重试**:代码执行失败时把 traceback + 上下文送回 LLM 修复
- **知识库**:可以把项目特有的字段定义、CRS 选择规范等写进去,所有会话共享
- **多轮对话 + 80K 上下文**:支持中途修改方案、追问、报告结果错误,模型能记住
  完整任务历史

---

## 环境要求

- **QGIS**:3.34 LTR 或更新(开发主线是 3.40 / 3.44)
- **Python**:QGIS 自带的 3.12+
- **平台**:Windows / macOS / Linux 都支持(Windows 上注意第 ⚠️ 节)

可选:
- 本地 Ollama 服务(用本地大模型)
- Anthropic / OpenAI 官方 API key 在国内需要代理

---

## 安装

把整个仓库 clone 或解压到 QGIS 插件目录:

**Windows**
```
%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\
```

**macOS**
```
~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/
```

**Linux**
```
~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/
```

启动 QGIS → `插件 → 管理并安装插件` → 在已安装里勾选 **AutonomousGIS-
SpatialAnalysisAgent** → 工具栏会出现一个新图标,点开就是对话面板。

第一次启动如果提示缺少 Python 包(`openai` / `langchain` 等),按提示点
"安装依赖"自动装。

---

## 配置 API Key

打开插件 → `设置` Tab → 把 key 粘进去 → 插件自动按前缀识别厂商:

| Key 前缀 | 对应厂商 | 说明 |
|----------|----------|------|
| `sk-or-...` | OpenRouter | **推荐**,一个 key 通所有模型,国内可直连 |
| `sk-...` | OpenAI 或 DeepSeek | 联网探测自动分流 |
| `sk-ant-...` | Anthropic Claude | 国内需要代理 |
| `AIza...` | Google Gemini | 国内需要代理 |
| `eyJ...` | MiniMax | 国内可用 |
| (留空) | 本地 Ollama | 需要先启动 ollama 服务 |

模型列表统一在 [`SpatialAnalysisAgent/models.toml`](SpatialAnalysisAgent/models.toml)
集中配置,改完重启 QGIS 生效。

国内用户挂代理:启动 QGIS 前在 PowerShell 设环境变量

```powershell
$env:HTTPS_PROXY = "http://127.0.0.1:7890"   # 改成你梯子端口
$env:HTTP_PROXY  = "http://127.0.0.1:7890"
& "C:\Program Files\QGIS 3.44\bin\qgis-bin.exe"
```

---

## 快速上手

1. **加载数据**:把待分析的图层(shp / tif / geojson 等)拖进 QGIS
2. **选数据**:勾选要纳入分析的图层
3. **写需求**:在底部输入框用中文或英文描述任务,例如
   > 帮我找出宾州内坡向大于 100 度、距离最近道路不超过 1000 米的露营适宜区
4. **确认方案**:AI 输出"任务简介 + 完整任务分解 + 已选工具",检查无误点
   "确认执行"
5. **看结果**:代码自动生成、执行、把结果图层加载回 QGIS;失败会自动重试

中途想改方案、追问参数、报告结果不对都可以直接说,AI 会进入对话模式调整。

---

## 工作原理(简要)

```
用户输入
  ↓
AgentController(状态机) ─────────┐
  ↓                              │ 信号 (Qt QThread)
SessionContext(上下文压缩)        │
  ↓                              ↓
LLM 调用 → GuardGate(输出解析) → 用户确认
  ↓
ToolRetrieval(RAG 选工具) → 工具文档拼装
  ↓
代码生成 → preflight 校验(运行时签名) → exec
  ↓
失败:SmartDebugger 闭环重试
  ↓
结果加载到 QGIS
```

更多细节看 [LIMITATIONS.md](LIMITATIONS.md) 的"系统脆弱点说明"那几节。

---

## 已知局限 ⚠️

- **Windows + GDAL `.bat` 工具脆弱**:`gdal:rastercalculator` / `gdal:polygonize`
  等通过 cmd.exe shell 调起,`(A>X)*1` 这种公式可能被 cmd 误解析。
  插件已经在 prompt 里钉了规则让 AI 在 Windows 上优先用 `native:*`,但仍可能
  遇到。详见 [LIMITATIONS.md §3.5](LIMITATIONS.md)
- **TOML 工具文档可能滞后**:虽然 preflight 已经改用运行时签名作权威,但 LLM
  在生成代码时仍会参考 TOML 里的示例。新版 QGIS 算法签名变化时,示例可能
  过时。详见 [LIMITATIONS.md §1](LIMITATIONS.md)
- **某些算法静默失败**:GDAL 工具偶尔会返回成功 dict 但不写文件。插件强制要求
  AI 在每步 `processing.run` 后加 `assert os.path.exists(path)` 防御
- **国内访问限制**:OpenAI / Anthropic / Google 官方 API 国内不可直连,
  建议用 OpenRouter 或挂代理

---

## 目录结构

```
SpatialAnalysisAgent-master/
├── SpatialAnalysisAgent_dockwidget.py      # 主 UI(对话面板)
├── SpatialAnalysisAgent_dockwidget_base.ui # Qt Designer 设计文件
├── metadata.txt                            # QGIS 插件元数据
├── SpatialAnalysisAgent/
│   ├── SpatialAnalysisAgent_AgentController.py  # 状态机核心
│   ├── SpatialAnalysisAgent_SessionContext.py   # 对话上下文管理
│   ├── SpatialAnalysisAgent_ModelProvider.py    # 各厂商 LLM 适配
│   ├── SpatialAnalysisAgent_ToolRetrieval.py    # RAG 工具检索
│   ├── SpatialAnalysisAgent_GuardGate.py        # 输出解析 + 风控
│   ├── SpatialAnalysisAgent_helper.py           # preflight + 调用工具集
│   ├── SpatialAnalysisAgent_Constants.py        # Prompt 和规则常量
│   ├── models.toml                              # 模型/厂商配置
│   └── Tools_Documentation/                     # 600+ QGIS 工具文档(TOML + JSON)
├── LIMITATIONS.md                          # 系统脆弱点与局限说明
└── README.md                               # 本文件
```


