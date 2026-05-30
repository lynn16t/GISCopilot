# SpatialAnalysisAgent 局限性说明

本文档记录 SpatialAnalysisAgent 当前架构下的两个核心可靠性依赖：**工具文档 TOML 的人工维护** 和 **QGIS 处理算法的版本兼容性**。这两者直接决定了 LLM 生成代码的正确率，且二者高度耦合 —— TOML 滞后于 QGIS 升级会立刻导致代码报错。

---

## 1. 可靠性来源：TOML 工具文档

### 1.1 LLM 是怎么"学会"调 QGIS 工具的

插件不让 LLM 凭训练记忆调 QGIS API，而是走一套"先检索文档、再写代码"的流程：

1. 用户提需求 → LLM 分解任务、给出工具 ID 列表（如 `qgis:aspect`、`gdal:rastercalculator`、`native:rastercalc`）
2. **从本地工具文档库检索**对应工具的参数说明 + 示例代码
3. LLM 拿着检索结果生成实际 Python 代码

第 2 步用到的文档全部存在仓库内：

| 路径 | 作用 |
|------|------|
| [SpatialAnalysisAgent/Tools_Documentation/QGIS_Tools/*.toml](SpatialAnalysisAgent/Tools_Documentation/QGIS_Tools/) | 每个工具一份独立 toml,含 `tool_ID`、`parameters`、`code_example` |
| [SpatialAnalysisAgent/Tools_Documentation/qgis_tools_for_rag.json](SpatialAnalysisAgent/Tools_Documentation/qgis_tools_for_rag.json) | 上面所有 toml 的扁平化 JSON,RAG 向量检索直接读这个 |
| [SpatialAnalysisAgent/Tools_Documentations.py](SpatialAnalysisAgent/Tools_Documentations.py) | 旧式文件检索回退路径下读的 Python dict |
| [SpatialAnalysisAgent/qgis340_tools.json](SpatialAnalysisAgent/qgis340_tools.json) | 工具 ID/名称索引,用于工具选择 |

### 1.2 关键风险：单点真相分散、容易脱节

同一个工具的文档**在多个文件里重复存在**。例如 `native:rastercalc` 的 code_example 同时出现在：

- `Tools_Documentation/QGIS_Tools/native_rastercalc.toml`
- `Tools_Documentation/QGIS_Tools/qgis_rastercalculator.toml`（文件名是旧别名,内容指向同一个 tool_ID）
- `Tools_Documentation/qgis_tools_for_rag.json`
- `Tools_Documentations.py`（两份: `qgis:rastercalculator` 和 `native:rastercalc` 两条 entry,内容相同）

**只改其中一份不够**。一处错例就足以让 LLM 持续生成错误代码 —— 因为检索可能命中任意一份。

### 1.3 已知的"错例传播"案例

| 工具 | 错误内容 | 影响 |
|------|---------|------|
| `native:rastercalc` | 示例传入 `LAYERS / EXTENT / CELL_SIZE / CRS` 四个非法键(QGIS 3.34+ 只接受 `INPUT / EXPRESSION / OUTPUT`) | LLM 反复触发 `ERROR_CODE_PARAM_UNKNOWN`,即便 SmartDebugger 也修不对 —— 因为它再次检索时仍命中错例 |
| `gdal:rastercalculator` | `Tools_Documentations.py` 里 entry 描述是 GDAL,示例代码却调用 `native:rastercalc` | LLM 选择 GDAL 时被错例带偏 |

### 1.4 TOML 维护清单

新增/修复一个工具文档时,必须**同步检查这五处**:

```text
1. Tools_Documentation/QGIS_Tools/<tool>.toml      ← 主文档
2. Tools_Documentation/QGIS_Tools/*_<alias>.toml   ← 是否还有别名文件指向同一 tool_ID
3. Tools_Documentation/qgis_tools_for_rag.json     ← RAG 检索源,改完需要重建 embedding
4. Tools_Documentations.py                          ← 兼容旧路径的 Python dict
5. qgis340_tools.json                               ← 仅工具名变更时
```

**确认改全的快速命令**(在工程根目录下):

```powershell
# 搜索所有还在使用旧参数的 entry
Select-String -Path SpatialAnalysisAgent -Pattern "'CELL_SIZE'\s*:|'LAYERS'\s*:\s*\[" -Recurse
```

如果改了 `qgis_tools_for_rag.json`,需要**让 RAG 重建 embedding 缓存**(否则向量检索仍返回旧文档),具体入口见 [SpatialAnalysisAgent/SpatialAnalysisAgent_ToolRetrieval.py](SpatialAnalysisAgent/SpatialAnalysisAgent_ToolRetrieval.py)。

---

## 2. QGIS 版本依赖

### 2.1 当前目标版本

仓库 metadata 声明 `qgisMinimumVersion=3.0`,但实际示例代码和工具文档主要按 **QGIS 3.34 LTR / 3.40** 写的。两个具体证据:

- `qgis340_tools.json` 文件名直接绑定 3.40
- [SpatialAnalysisAgent_Constants.py](SpatialAnalysisAgent/SpatialAnalysisAgent_Constants.py) 的 `operation_requirement` 多处规则明确说"this Qt6 LTR build"、"native:rastercalculator → native:rastercalc"

### 2.2 算法 ID / 参数会随 QGIS 升级变化

下面这些**已知会随版本变化**的算法,是踩坑高发区:

| 算法 | 变化点 | 影响版本 |
|------|--------|---------|
| `native:rastercalc` | 旧版 `LAYERS / EXTENT / CELL_SIZE / CRS` 全部废弃,新版只剩 `INPUT / EXPRESSION / OUTPUT` | 3.34+ 重写 |
| `qgis:rastercalculator` | 旧别名,新版可能直接路由到 `native:rastercalc`,但参数集不同 | 3.20+ |
| `grass:*` vs `grass7:*` | Qt6 LTR 版本只保留 `grass:` 前缀,`grass7:` 全部失效 | 3.34 Qt6 起 |
| `gdal:grid*` | 旧的 `gdal:grid / gdal:grididw / gdal:gridinversedistanceweighted` 不存在,改为 `gdal:gridinversedistance` 等显式 ID | 3.x 中段起 |
| `native:savevectorlayer` | 不存在,改用 `native:savefeatures` | 全版本 |
| `native:executesql` | 不存在,改用 `qgis:executesql` | 3.40 |
| `PyQt5` 导入 | Qt6 build 下直接报错,必须用 `from qgis.PyQt.* import` | Qt6 LTR 起 |

### 2.3 检测当前 QGIS 实际算法签名的方法

如果不确定某个算法在用户机器上是哪个参数集,直接在 QGIS Python 控制台跑:

```python
import processing
processing.algorithmHelp("native:rastercalc")
```

会打印当前版本下该算法实际接受的参数,以此为准更新 toml。

### 2.4 版本切换会破坏什么

- **算法被改名/删除** → LLM 选了一个本机不存在的工具 → 报 `Algorithm xxx not found`,SmartDebugger 也救不回来
- **参数被增减** → LLM 复用旧示例传非法参数 → `ERROR_CODE_PARAM_UNKNOWN`
- **GDAL 版本不同** → 同一 `gdal:*` 算法的 `RTYPE` 枚举值或 `DATA_TYPE` 选项会变
- **PROJ 数据库版本不同** → CRS 字符串解析行为可能微差
- **QGIS Qt5 vs Qt6 build** → 大量 PyQt 导入路径不兼容

### 2.5 升级 QGIS 时的回归测试建议

至少跑一遍这几类任务,看是否还能完整生成 → 执行通过:

1. **栅格代数**:DEM → 坡度/坡向 → 二值掩膜 → 矢量化 → 属性筛选
2. **缓冲与叠加**:点 → 缓冲 → 与多边形相交 → 字段统计
3. **GRASS 算法**:`grass:r.viewshed` 等带 `grass:` 前缀的
4. **GDAL warp 重投影**:`gdal:warpreproject` 带 `TARGET_RESOLUTION`
5. **绘图输出**:`qgis:vectorlayerscatterplot`

任一类失败,先到 QGIS Python 控制台用 `algorithmHelp` 对照,再回头修对应 toml。

---

## 3. 二者耦合 → 维护成本

TOML 和 QGIS 版本是**乘法关系**而不是加法:

- QGIS 升级 + TOML 不动 → 直接失效(用户看到 `does not accept parameter(s)` / `Algorithm not found`)
- TOML 改了但 RAG embedding 不重建 → 检索仍返回旧文档,LLM 又写错
- 多处副本只改了一处 → 其他副本继续污染

建议把以下三件事作为版本升级的**必做项**:

```text
□ 升级 QGIS 后,对照本文 2.5 节用例跑一遍回归
□ 发现错例时,用本文 1.4 节命令一次性搜全所有副本同步改
□ 改完 qgis_tools_for_rag.json 后,重建 RAG embedding 缓存
```

---

## 3.5 Windows 上的 GDAL .bat shell 脆弱性

QGIS Windows build 里部分 GDAL 算法(`gdal:rastercalculator`、`gdal:warpreproject`、
`gdal:polygonize` 等)是通过 `gdal_calc.bat` / `gdaltindex.bat` 这类 **Windows 批处理脚本**
间接调起 Python CLI 工具的。中间多了一层 `cmd.exe`,带来两个稳定的坑:

1. **FORMULA 表达式被 cmd 误解析**:`(A>100)*1` 里的 `>` 是 cmd 的重定向操作符,
   `*` 是通配符,`(` `)` 是分组符。某些路径/版本下双引号保不住,直接报:

       Process returned error code 1
       The filename, directory name, or volume label syntax is incorrect.

2. **正斜杠路径在 .bat 里可能被当 cmd 开关**:`F:/foo/bar.tif` 进 cmd 后,
   `/foo` 可能被解释成命令行选项标志。

**症状**:`processing.run` 返回成功 dict,`result['OUTPUT']` 指向预期路径,
但磁盘上根本没有文件。assert `os.path.exists` 才能抓到。Feedback 对象的
`reportError` 里会看到 `[ERROR] Process returned error code 1`。

**对策**:Windows 上**优先用 `native:*` 算法**(纯 QGIS Python 实现,
没有 .bat shell 层),实在需要 GDAL 才回退。代码生成 prompt 里已经加了这条规则:

> PREFER `native:rastercalc` over `gdal:rastercalculator` on Windows. ...

具体对照表:

| Windows 优先 | GDAL 备选(仅在必要时) | 区别 |
|--------------|----------------------|------|
| `native:rastercalc` | `gdal:rastercalculator` | 表达式语法不同:NAME@BAND vs A/B/C |
| `native:reprojectlayer` | `gdal:warpreproject` | native 不走 shell |
| `native:polygonize`(若存在) | `gdal:polygonize` | 后者必经 .bat |

## 4. 短期内可降低风险的兜底

代码生成 prompt 里已经加入两条强制规则(见 [SpatialAnalysisAgent/SpatialAnalysisAgent_Constants.py](SpatialAnalysisAgent/SpatialAnalysisAgent_Constants.py) 的 `operation_requirement`):

1. **每次 `processing.run` 后必须 `assert os.path.exists(<output>)`** —— 让"静默失败"立即可见,不会等到下游报"Could not load source layer"才发现
2. **`gdal:rastercalculator` 禁止设 `NO_DATA=0`** —— 避免二值掩膜被全部 mask 掉

这两条规则**与 QGIS 版本无关**,即便 toml 文档过时,也能在最后一道关卡上挡掉一部分错误。但它们不是替代品,核心还是要把 toml 保持与 QGIS 同步。

---

## 5. 未来值得考虑的改造

如果想从根本上降低这种维护负担,可以评估的方向:

1. **去掉 TOML 重复副本**,把 `Tools_Documentations.py` / `qgis_tools_for_rag.json` 改成从 `QGIS_Tools/*.toml` 自动构建的派生文件,启动时生成
2. **运行时元信息校验**:工具被选中后,先调 `processing.algorithmHelp(tool_id)` 拿当前版本真实参数,和 toml 里的参数对比,不一致时立刻打 warning 并以运行时为准
3. **QGIS 版本指纹**:启动时记录 `Qgis.version()` 和 GDAL 版本,把它们注入 system prompt,让 LLM 知道"目前在 QGIS 3.40 / GDAL 3.8 上跑"
4. **回归测试套件**:把第 2.5 节的几类任务固化成自动化测试,QGIS 升级前自动跑一遍

短期人工维护成本可控,长期建议至少做 (1) 和 (3)。
