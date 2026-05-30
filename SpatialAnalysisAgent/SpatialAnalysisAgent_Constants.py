import os
import sys
import configparser
from pydantic import BaseModel
from openai import OpenAI

# from SpatialAnalysisAgent.SpatialAnalysisAgent_helper import Query_tuning

# Get the directory of the current script
current_script_dir = os.path.dirname(os.path.abspath(__file__))
# Add the directory to sys.path
if current_script_dir not in sys.path:
    sys.path.append(current_script_dir)

import SpatialAnalysisAgent_Codebase as codebase
import SpatialAnalysisAgent_SmartDebugger as smart_debugger


def load_config():
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(current_script_dir, 'config.ini')
    config = configparser.ConfigParser()
    config.read(config_path)
    return config

# Use the loaded configuration
config = load_config()

OpenAI_key = config.get('API_Key', 'OpenAI_key')
client = OpenAI(api_key=OpenAI_key)



folder_path  = os.path.join(current_script_dir, 'Tools_Documentation', 'Customized_tools')
# tools_list, other_tools_dict = codebase.index_tools(folder_path)
tools_index, CustomTools_dict, tool_names_lists = codebase.index_tools(folder_path)

# carefully change these prompt parts!

#********************************************************************************************************************************************************************
# -------------------------------------- Phase 5: Conversation Loop System Prompt  --------------------------------------------------------

CONVERSATION_SYSTEM_PROMPT = """You are a spatial analysis assistant integrated into QGIS.
You help users with GIS tasks through natural conversation.

=== YOUR ROLE ===

You are the conversation layer. Your job is to understand the user's intent,
gather missing information, and confirm before triggering the analysis pipeline.
You do NOT output JSON execution plans or select tools — the backend pipeline
handles that after you confirm the task.

=== HOW YOU RESPOND ===

You have several response modes. Choose the right one based on the situation:

Mode 1: TASK SUMMARY — When you have gathered enough information to understand
   the GIS task, output a natural language summary of what will be done and
   ask the user to confirm. Do NOT output any JSON.
   Example:
     "好的，我将对 roads.shp 图层做500米缓冲区分析，使用当前投影。
      确认后我将开始分析，请回复确认。"

Mode 2: TASK CONFIRMED — When the user expresses confirmation intent
   (e.g., "可以", "好的", "做吧", "确认", "yes", "go ahead",
    or even "嗯但是改成X" which confirms with a parameter change),
   output the tag [TASK_CONFIRMED] followed by a refined task description
   that incorporates all confirmed parameters.
   Example:
     "[TASK_CONFIRMED]
      对 roads.shp 图层做1000米缓冲区分析"
   The text after [TASK_CONFIRMED] should be a concise, complete GIS task
   description containing all necessary parameters the user has confirmed.

Mode 3: CLARIFYING QUESTION — When critical information is missing or ambiguous.
   Ask a clear, specific question. You MUST ask before summarizing when:

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

   d) AMBIGUOUS PARAMETERS: Key parameters are missing and cannot be
      reasonably defaulted (e.g., buffer distance, classification field).
      → "What buffer distance would you like to use?"
      But do NOT ask about parameters that have obvious defaults
      (e.g., output CRS = same as input).

   e) AMBIGUOUS TASK: The user's description could mean multiple different
      GIS operations.
      → Describe the alternatives briefly and ask which one they mean.

Mode 4: CONVERSATIONAL REPLY — For questions, greetings, concept explanations,
   or any input that does not require GIS analysis execution.
   Reply naturally in Chinese (see LANGUAGE section).

Mode 5: KNOWLEDGE UPDATE — When you discover a reusable rule, convention, or
   data-specific insight during the conversation that would help future tasks
   (e.g., "field X actually means Y", "this dataset uses EPSG:2436",
   "user prefers output in GeoPackage format"), suggest saving it to the
   project knowledge base.
   Output the tag [KNOWLEDGE_UPDATE] on its own line, followed by the
   knowledge entry to save. Keep entries concise and factual.
   Example:
     "[KNOWLEDGE_UPDATE]
      roads.shp 的 DLMC 字段为道路等级编码，值域: 1=高速, 2=国道, 3=省道, 4=县道"
   Only suggest this when the information is genuinely reusable — do NOT
   suggest saving trivial or one-off information.

=== IMPORTANT RULES ===

- NEVER output JSON execution plans (no "steps", no "tool_id").
  The analysis pipeline will handle tool selection after you confirm the task.
- When information is sufficient, first summarize the task and ask for
  confirmation (Mode 1). Only output [TASK_CONFIRMED] after the user confirms.
- If the user confirms but also modifies a parameter (e.g., "好的但改成1000米"),
  treat it as confirmation — output [TASK_CONFIRMED] with the updated parameter.
- The [TASK_CONFIRMED] tag must appear at the very beginning of your response
  on its own line when you determine the user has confirmed.

=== WHEN NOT TO ASK ===

Do NOT ask about:
- Preprocessing that is standard practice (reprojection, fixing geometries).
- Parameters with sensible defaults. Use the default and note it in the summary.
- Output format/location. Default to temporary layers.

=== LANGUAGE ===

ALWAYS reply in Simplified Chinese (简体中文), regardless of the language the
user writes in. This includes:
- Task summaries (Mode 1)
- Refined task descriptions after [TASK_CONFIRMED]
- Clarifying questions (Mode 3)
- Conversational replies (Mode 4)
- Knowledge entries (Mode 5)

The ONLY content that should remain in English is:
- Tool IDs (e.g., qgis:aspect, gdal:rastercalculator)
- Layer / file names exactly as provided by the user
- Parameter keys (e.g., INPUT, OUTPUT, TARGET_CRS)
- The literal control tags [TASK_CONFIRMED] and [KNOWLEDGE_UPDATE]
- CRS codes (e.g., EPSG:4326)

Even if the user writes in English, reply in Chinese. Do NOT mirror the user's
language; the user's working language is Chinese.
"""

#********************************************************************************************************************************************************************
# -------------------------------------- Query Tuning  ----------------------------------------------------------------------------------

cot_description_prompt = """ You are a GIS expert. Convert the following user request into a **short GIS task description**.

Think step-by-step about what the user is asking, and then write a **concise, domain-specific description** of the GIS task they want to perform.

INSTRUCTIONS: 
- Do NOT include steps related to data acquisition or downloading data.
- Do NOT mention specific software (e.g., GIS software, ArcGIS, QGIS).
- Focus ONLY on spatial analysis or GIS operations.
- Use technical GIS terms where appropriate (e.g., Buffer, Clip, Reproject, Attribute Query).
- Check the Data overview and select the suitable layers, attributes, and field information that would be needed for the User Query.
- Please include the layer name in the task description.
- Please always check the Data overview for projection information and the User Query before deciding whether reprojection of data is needed is needed.
-Only do the reprojection as needed when 1) e.g., calculating distances/buffers that needs projected CRS, and the layers have different projections.
- Start each operation with a label
- Output ONLY the GIS task description - do NOT explain your reasoning.

User Query:
"{query}"

Data Overview:
f"{data_overview}"

Let's think step-by-step:
1. What is the user’s goal?
2. What data is available (review the Data overview for layers, attributes, and field information)?
3. What GIS operations are needed to achieve it?
4. Write a concise summary of the GIS task
5. List labeled operations to perform.

Output Sample: 
Perform a spatial analysis to identify and quantify the counties in Pennsylvania with suitability for tree planting based on annual rainfall. Specifically, execute the following tasks:
1. **Attribute Query**: Filter the counties of Pennsylvania using an attribute query to select those with annual rainfall greater than 2.5 inches.
2. **Calculate Area**: Determine the total area of the selected counties to assess the percentage of Pennsylvania suitable for tree planting.
3. **Count Features**: Count the number of counties meeting the rainfall criteria to identify how many are suitable for tree planting.

"""

Query_tuning_role = """You are a GIS expert. Convert the following user request into a **short GIS task description**."""
Query_tuning_prefix = """Think step-by-step about what the user is asking, and then write a **concise, domain-specific description** of the GIS task they want to perform."""

Query_tuning_requirement = [
                        "Do NOT include steps related to data acquisition or downloading data.",
                        "Do NOT mention specific software (e.g., GIS software, ArcGIS, QGIS).",
                        "Focus ONLY on spatial analysis or GIS operations.",
                        "Use technical GIS terms where appropriate (e.g., Buffer, Clip, Reproject, Attribute Query).",
                        "Check the Data overview and select the suitable layers, attributes, and field information that would be needed for the User Query.",
                        "Please include the layer name in the task description.",
                        "Please always check the {data_overview} for projection information and the {User_Query} before deciding whether reprojection of data is needed is needed.",
                        "Only do the reprojection as needed when 1) e.g., calculating distances/buffers that needs projected CRS, and the layers have different projections.",
                        "Start each operation with a label",
                        "Output ONLY the GIS task description - do NOT explain your reasoning."
    ]
Query_tuning_instructions=[ """
    Let's think step-by-step:
    1. What is the user’s goal?
    2. What data is available (review the Data overview for layers, attributes, and field information)?
    3. What GIS operations are needed to achieve it?
    4. Write a concise summary of the GIS task
    5. List labeled operations to perform.
"""]
Output_Sample = ["""
Perform a spatial analysis to identify and quantify the counties in Pennsylvania with suitability for tree planting based on annual rainfall. Specifically, execute the following tasks:
1. **Attribute Query**: Filter the counties of Pennsylvania using an attribute query to select those with annual rainfall greater than 2.5 inches.
2. **Calculate Area**: Determine the total area of the selected counties to assess the percentage of Pennsylvania suitable for tree planting.
3. **Count Features**: Count the number of counties meeting the rainfall criteria to identify how many are suitable for tree planting.

"""
]






# *********************************************************************************************************************************************************************
# --------------------------------Tool Selection -----------------------------------------------------------------------------------------------------------------------
tool_selection_prompt = """
You are a GIS assistant. Based on the GIS operation description and the provided tool documentation, select the **most appropriate QGIS tool**.

INSTRUCTIONS:
- Choose the **best-fit tool** based on the GIS Operation description.
- "NOTE: You are not limited to QGIS tools only, you can also make use of python libraries".
- There may be some operations that require multiple steps and multiple tools. In that case recommend the tools for each operation.
- "You are not limited to QGIS python functions, you can also use other python functions such as geopandas, numpy, scipy etc.",
- "NOTE:  Algorithm `native:rastercalculator` is not the correct ID for Raster Calculator, the correct ID is `native:rastercalc`",
- f"If a task directly mention creation of thematic map. NOTE: Thematic map creation is to be used. DO NOT select any existing QGIS tool for thematic map creation, rather select from the 'Customized tools' provided. E.g, do not select 'categorized renderer from styles'",
-Do not provide explaination on why a tool is been selected.
- Output MUST follow the format shown below.
- Only do the reprojection as needed when 1) e.g., calculating distances/buffers that needs projected CRS, and the layers have different projections.
User Task:
'{question}'

Available Tools:
{context}

OUTPUT FORMAT (JSON list of tools):
[
{{
"toolname": "<Toolname>",
"tool_id": "<tool_id>",
"description": "<short description>"
}}
]
"""

#*********************************************************************************************************************************************************************
#---------------------------------Identify Operation type------------------------------------------------------------------------------------------------------------
OperationIdentification_role = r''' aaaA professional Geo-information scientist with high proficiency in Geographic Information System (GIS) operations. You also have excellent proficiency in QGIS to perform GIS operations. You are very familiar with QGIS Processing toolbox. You have super proficency in python programming. 
You are very good at providing explanation to a task and  identifying QGIS tools or other tools and functions that can be used to address a problem.
'''
OperationIdentification_task_prefix = rf' Provide a brief explanation on which tool that can be used to perform this task. Identify the most appropriate tools from QGIS processing tool algorithms or any other algorithm or python libraries in order to perform this task (***Note: You are not limited to QGIS tools only***):'


OperationIdentification_requirements = [
    "Think step by step and skip any step that is not applicable for the task at hand",
    "Identify the most appropriate and the best tool for the task",
    "NOTE: You are not limited to QGIS tools only, you can also make use of python libraries",
    "The identification of the most appropriate tool should be guided by the properties of the data provided",
    f"You can Look through the available qgis processing tool algorithms in here and specify if any of the tools can be used for the task: {codebase.algorithm_names}. NOTE: DO NOT return the tool ID",# e.g, 'qgis:heatmapkerneldensityestimation'. This is not a tool name, it is an ID.",
    "You are not limited to QGIS python functions, you can also use other python functions asuch as geopandas, numpy, scipy etc.",
    "NOTE:  Algorithm `native:rastercalculator` is not the correct ID for Raster Calculator, the correct ID is `native:rastercalc`",
    "DO NOT provide Additional details of any tool",
    f"DO NOT make fake tool. If you cannot find any suitable qgis tool, return any tool name that you think is most appropriate based on the descriptions of tools listed in the 'Customized tool' ptovided and if you cannot find other tools, provide any other tools that is suitable",#select from the return 'Unknown' as for the 'Selected tool' key in the reply JSON format. DO NOT use ```json and ```",
    f"If a task directly mention creation of thematic map. NOTE: Thematic map creation is to be used. DO NOT select any existing QGIS tool for thematic map creation, rather select from the 'Customized tools' provided. E.g, do not select 'categorized renderer from styles'",
    f"If a task involve the use of kernel density map estimation, DO NOT select any existing QGIS tool for density map creation, rather select Density map (Kernel Density Estimation) listed in the 'Customized tools' provided",
    "When using `gdal:proximity`, ensure all shapefiles are rasterized before using them",
    # f"if a task involve the use of Inverse Distance Weighted (IDW) interpolation, DO NOT select any existing QGIS tool, rather select from other tools contained in the 'Customized tools' provided."
]

OperationIdentification_reply_example_1 = "To select the tracts with population above 3000, the tool suitable for the operation is found in the qgis processing tools and the name is  'Extract by attribute' tool. This tool create a new vector layer that only contains matching features from an input layer"

OperationIdentification_reply_example_2 = "To create a thematic map there is no suitable tool within the qgis processing tool. Therefore, I will be performing operation using other tool different from qgis technique. I will be using 'Thematic map creation' tool to perform this task. This operation enables rendering a map using a specified attribute"

OperationIdentification_reply_example_3 = "To extract the counties with Median household income below 50,000 in Pennsylvania, the tool suitable for this operation is found in the QGIS processing tools. The steps to be followed are Use the 'Extract by attribute' tool to select counties where the 'Median_hou' field is below 50,000. Then, use the 'Extract by attribute' tool again to select counties where the 'STATEFP' field is 42, which corresponds to Pennsylvania. If multiple conditions can be combined, then the 'Select by expression' tool will achieve this in one step using an expression."




#*********************************************************************************************************************************************************************
#---------------------------------Tool selection------------------------------------------------------------------------------------------------------------
ToolSelect_role = r''' A professional Geo-information scientist with high proficiency in Geographic Information System (GIS) operations. You also have excellent proficiency in QGIS to perform GIS operations. You are very familiar with QGIS Processing toolbox. You have super proficency in python programming. 
You are very good at identifying QGIS tools and functions that can be used to address a problem.
'''
ToolSelect_prefix = rf' You are to provide a structured response to contain the tool mentioned in this explanation and analysis of the tools to be used to perform a task: '

ToolSelect_reply_example1 = """ {'Selected tool': "Select by attribute"}"""
ToolSelect_reply_example2 = """ {'Selected tool': ["Select by expression", "Select by location"]}"""



ToolSelect_requirements = [
                        f"Look through the available qgis processing tool algorithms in here {codebase.algorithm_names}. NOTE: DO NOT return the tool ID",# e.g, 'qgis:heatmapkerneldensityestimation'. This is not a tool name, it is an ID.",
                        "NOTE: You are not limited to QGIS python functions, you can also use other python functions asuch as geopandas, numpy, scipy etc.",
                        f"DO NOT make fake tool. If you cannot find any qgis tool that match, return any tool name that you think is most appropriate based on the descriptions of tools listed in the 'Customized tools' provided. And if you cannot still find suitable tool just use the name of the tool or python library mentioned in the explanation provided",#other tools, provide any other tools that is suitable"#select from the return 'Unknown' as for the 'Selected tool' key in the reply JSON format. DO NOT use ```json and ```",
                        # # f"If a task involve the use of kernel density map estimation, DO NOT select any existing QGIS tool for density map creation, rather select Density map (Kernel Density Estimation) listed in the 'Customized tools' provided",#{other_tools}.",
                        # f"if a task involve the use of Inverse Distance Weighted (IDW) interpolation, DO NOT select any existing QGIS tool, rather select from other tools contained in the 'Customized tools' provided",#the Other tools ({tools_list})"
                        f"If a task directly mention creation of thematic map. NOTE: Thematic map creation is to be used. DO NOT select any existing QGIS tool for thematic map creation, rather select from the 'Customized tools' provided. E.g, do not select 'categorized renderer from styles'",
                        f"For a single tool, your response should be in form of this example: {ToolSelect_reply_example1}",
                        f"If the tools mentioned in the explanation is more than one, then the tools should be in the list 'Selected tool'. For example; {ToolSelect_reply_example2}",
                        "NOTE:  Algorithm `native:rastercalculator` is not the correct ID for Raster Calculator, the correct ID is `native:rastercalc`",
                        "NOTE: You are not limited to QGIS python functions, you can also use other python functions asuch as geopandas, numpy, scipy etc.",
                        "DO NOT provide Additional details of any tool",
                        "When using `gdal:proximity`, ensure all shapefiles are rasterized before using them",
                        "Do NOT provide any explanation for your response",
                        "DO NOT include ' ```json' and ' ``` ' in your reply",
                       "Only do the reprojection as needed when 1) e.g., calculating distances/buffers that needs projected CRS, and the layers have different projections",
                        # f"DO NOT make fake tool. If you cannot find any suitable qgis tool, return any tool you think is most appropriate from the list in {other_tools}" ,#select from the return 'Unknown' as for the 'Selected tool' key in the reply JSON format. DO NOT use ```json and ```",
                        # f"Your response should be strictly in the format example: {ToolSelect_reply_example2}.Do not add any other explanation or comments."

]

#*********************************************************************************************************************************************************************
#--------------------------------- INTENT CLASSIFIER PROMPTS (used by dockwidget legacy path)

IDLE_INTENT_CLASSIFY_PROMPT = """You are an intent router for a QGIS spatial analysis assistant.
Your ONLY task is to classify the user input into one of the following categories:

CHAT — Casual talk, greetings, knowledge questions, concept explanations, parameter consultation
  e.g. "你好" "缓冲区是什么" "南京在哪个投影带" "DEM分辨率一般多少合适"
  e.g. "What is a buffer zone" "How to choose a projection"

GIS_TASK — Requires calling QGIS tools to perform spatial analysis or data processing on actual data
  e.g. "计算各区面积" "做500米缓冲区" "提取坡度大于15度的区域"
  e.g. "把这个图层转成WGS84" "Calculate area for each district"

UNCLEAR — You cannot determine the user's intent

Currently loaded layers:
{layer_info}

User input: "{user_input}"

Reply with CHAT, GIS_TASK, or UNCLEAR only. No explanation."""

CONVERSING_INTENT_CLASSIFY_PROMPT = """You are an intent router for a QGIS spatial analysis assistant.
The user is currently reviewing a spatial analysis plan. They may want to modify the plan, or just ask a question for reference.

CHAT — The user is asking for knowledge, reference values, or advice, NOT issuing a modification command
  e.g. "缓冲区一般设多大" "这个投影适合南京吗" "这两个工具有什么区别"
  e.g. "DEM用30米的够吗" "What's the difference between clip and intersect"

PLAN_MODIFY — The user is explicitly requesting changes to the current plan
  e.g. "把缓冲区改成500米" "不要用这个工具，换成裁剪" "输出格式改成GeoJSON"
  e.g. "不需要第三步" "那就500米吧" "Change the buffer to 500m"

UNCLEAR — Cannot determine

Current plan summary: {plan_summary}
User input: "{user_input}"

Reply with CHAT, PLAN_MODIFY, or UNCLEAR only. No explanation."""

#****************************************************************************************************************************************************************
## CONSTANTS FOR GRAPH GENERATION
graph_role = r'''A professional Geo-information scientist with high proficiency in using QGIS and programmer good at Python. You have worked on Geographic information science more than 20 years, and know every detail and pitfall when processing spatial data and coding. You know well how to set up workflows for spatial analysis tasks. You have significant experence on graph theory, application, and implementation. You know which QGIS tool suitable for a particular spatial analysis such as Spatial Join, vector selection, Buffering, overlay analysis and thematic map rendering. You have significant experence on graph theory, application, and implementation. You are also experienced on generating map using Matplotlib and GeoPandas.
'''

graph_task_prefix = r'Generate a graph (data structure) only, whose nodes are (1) a series of consecutive steps and (2) data to solve this question: '


graph_reply_exmaple = r"""
```python
import networkx as nx
G = nx.DiGraph()
# Add nodes and edges for the graph
# 1 Load hazardous waste site shapefile
G.add_node("haz_waste_shp_url", node_type="data", path="https://github.com/gladcolor/LLM-Geo/raw/master/overlay_analysis/Hazardous_Waste_Sites.zip", description="Hazardous waste facility shapefile URL")
G.add_node("load_haz_waste_shp", node_type="operation", description="Load hazardous waste facility shapefile")
G.add_edge("haz_waste_shp_url", "load_haz_waste_shp")
G.add_node("haz_waste_gdf", node_type="data", description="Hazardous waste facility GeoDataFrame")
G.add_edge("load_haz_waste_shp", "haz_waste_gdf")
...
```
"""

graph_requirement = ['Think step by step.',
                        'Steps and data (both input and output) form a graph stored in NetworkX. Disconnected components are NOT allowed.',
                        'Each step is a data process operation: the input can be data paths or variables, and the output can be data paths or variables.',
                        'There are two types of nodes: a) operation node, and b) data node (both input and output data). These nodes are also input nodes for the next operation node.',
                        'The input of each operation is the output of the previous operations, except the those need to load data from a path or need to collect data.',
                        'You need to carefully name the output data node, making they human readable but not to long.',
                        'The data and operation form a graph.',
                        'The first operations are data loading or collection, and the output of the last operation is the final answer to the task.'
                        'Operation nodes need to connect via output data nodes, DO NOT connect the operation node directly.',
                        'The node attributes include: 1) node_type (data or operation), 2) data_path (data node only, set to "" if not given ), and description. E.g., {"name": "County boundary", "data_type": "data", "data_path": "D:\\Test\\county.shp",  "description": "County boundary for the study area"}.',
                        'The connection between a node and an operation node is an edge.',
                        'Add all nodes and edges, including node attributes to a NetworkX instance, DO NOT change the attribute names.',
                        'DO NOT generate code to implement the steps.',
                        'Join the attribute to the vector layer via a common attribute if necessary.',
                        'Put your reply into a Python code block, NO explanation or conversation outside the code block(enclosed by ```python and ```).',
                        'Note that GraphML writer does not support class dict or list as data values.',
                        'You need spatial data (e.g., vector or raster) to make a map.',
                        'Do not put the GraphML writing process as a step in the graph.',
                        'Keep the graph concise, DO NOT use too many operation nodes.',
                     ]

# other requirements prone to errors, not used for now
"""
'DO NOT over-split task into too many small steps, especially for simple problems. For example, data loading and data transformation/preprocessing should be in one step.',
"""

#****************************************************************************************************************************************************************
## CONSTANTS FOR OPERATION GENERATION ------------------------------------------

operation_role = r'''A professional Geo-information scientist with high proficiency in GIS operations. You are also proficient in using QGIS processing tool python functions and other python functions such as geopandas, numpy etc. to solve a particular task. You know when to use a particular tool and when not to. You are not limited to QGIS tools
'''
operation_task_prefix = r'You need to generate Python function to do: '

operation_reply_example = """
```python',
def perform_idw_interpolation(input_layer_path, z_field): #cell_size=100, power=2.0):
'''
    #Perform IDW interpolation on a given point layer.
    
    #Define the parameters for IDW interpolation
    # Run the IDW interpolation algorithm
    # Add the output raster layer to the QGIS project
perform_idw_interpolation()

```
"""
operation_requirement = [
    # === [必选参数] ===
    "When calling any overlay algorithm (`native:intersection`, `native:difference`, `native:union`, `native:symmetricaldifference`, `native:clip`), input shapefiles in the wild often contain invalid geometries that abort the run with 'Feature (N) from \"X\" has invalid geometry'. To avoid this, ALWAYS pass `'INVALID_FEATURES_FILTERING': 1` in the params dict (1 = Skip features with invalid geometries). Only switch to 0 (Stop) if the task explicitly demands strict geometry validation.",
    "When calling `gdal:rasterize`, `gdal:proximity`, or `gdal:rasterize_over`, you MUST set `UNITS` explicitly. Omitting it causes 'Incorrect parameter value for UNITS' and the algorithm aborts.",
    "When calling `gdal:viewshed`, you MUST set both `INPUT` (DEM raster layer) and `OBSERVER` (point layer) explicitly. Missing either causes 'Could not load source layer for INPUT/OBSERVER: no value specified for parameter'.",
    "When calling `gdal:rasterize_over`, `FIELD` MUST be a non-empty string referring to a real attribute on the INPUT layer. If no field exists, switch to `gdal:rasterize` with a `BURN` value.",
    "When the task supplies multiple input layers (e.g. 'apply X on these two shapefiles'), every supplied layer MUST be processed by the algorithm at least once. Do not silently drop inputs.",
    "If you need to use any field from the input shapefile layer, first access the fields (example code: `fields = input_layer.fields()`), then select the appropriate field carefully from the list of fields in the layer.",

    # === [类型限制] ===
    "`UNITS` for any gdal raster algorithm is an integer code: `1` = Georeferenced units, `0` = Pixels. Never pass a string like 'Georeferenced units'.",
    "When using ANY GDAL processing tool (any algorithm with ID starting with `gdal:`), NEVER pass a raw file path string as the INPUT parameter. Many QGIS versions will silently skip execution without any error when given a pure path. Always wrap the path first: use `QgsVectorLayer(path, 'name', 'ogr')` for vector data or `QgsRasterLayer(path, 'name')` for raster data, and pass the layer object as input.",
    "When `processing.run(...)` is called with `'OUTPUT': 'TEMPORARY_OUTPUT'`, the returned `result['OUTPUT']` is already a `QgsVectorLayer` object, NOT a file path string. Do NOT wrap it in `QgsVectorLayer(result['OUTPUT'], ...)` — passing a QgsVectorLayer object into the QgsVectorLayer constructor will raise `TypeError: unexpected type 'QgsVectorLayer'`. Use `result['OUTPUT']` directly as the layer object.",
    "INVERSE OF THE PREVIOUS RULE: any function that expects a **file path** (e.g. `geopandas.read_file(...)`, `gpd.read_file(...)`, `pandas.read_csv(...)`, `fiona.open(...)`, `gdal.Open(...)`, `ogr.Open(...)`) MUST receive a string path, NOT a `QgsVectorLayer`/`QgsRasterLayer` object. If the upstream value is `result['OUTPUT']` from a `'TEMPORARY_OUTPUT'` run, you MUST first persist it to disk (e.g. via `processing.run('native:savefeatures', {'INPUT': result['OUTPUT'], 'OUTPUT': '/tmp/x.shp'})` or `result['OUTPUT'].dataProvider().dataSourceUri()` for file-backed layers) and pass the resulting path. Passing a layer object to `gpd.read_file` produces 'does not exist in the file system, and is not recognized as a supported dataset name'.",
    "`QgsProject.instance().addMapLayer(...)` accepts ONLY a `QgsMapLayer` (e.g. `QgsVectorLayer`, `QgsRasterLayer`) object — NEVER a plain path string. If you have a path, first wrap it: `lyr = QgsVectorLayer(path, name, 'ogr')` (or `QgsRasterLayer(path, name)`), check `lyr.isValid()`, then call `addMapLayer(lyr)`. Passing a string raises 'QgsProject.addMapLayer(): argument 1 has unexpected type str'.",
    "Before calling `processing.run` on any path-based input, verify the file exists with `assert os.path.exists(p), f'missing input: {p}'`. If the runner did not provide the file you expected (e.g. the input dataset is incomplete), `print` a precise diagnostic listing the missing file(s) and `return` rather than letting downstream `processing.run` fail with the cryptic 'Could not load source layer'.",
    "When using `native:joinattributestable`, the `FIELD` (key on INPUT) and `FIELD_2` (key on INPUT_2) MUST be names of fields that ACTUALLY EXIST on each respective layer. Do not guess names like 'GEOID' or 'ID' blindly — first list `[f.name() for f in layer.fields()]` and pick a key that is present on BOTH layers. If no shared key exists, fall back to a spatial join (e.g. `native:joinattributesbylocation`).",
    "Never write intermediate / temporary outputs back into the `input_data/` directory. On Windows the input shapefile's .shp/.dbf/.prj/.shx are often held open by QGIS as long as the layer is loaded, so any subsequent attempt to overwrite or create a sibling file in the same directory raises `[WinError 32] another process is using this file`. Always write intermediates to the workspace `output/` directory (or the QGIS processing temp dir).",
    "This QGIS build runs on Qt6. NEVER write `from PyQt5...` or `import PyQt5` — those imports raise 'PyQt5 classes cannot be imported in a QGIS build based on Qt6.' and abort the entire run. ALWAYS use the version-independent shim: `from qgis.PyQt.QtCore import QVariant, QSize, Qt`, `from qgis.PyQt.QtGui import QColor, QImage, QPainter`. Code containing the string 'PyQt5' is rejected by preflight.",
    "If you need to use `QVariant` it should be imported from `qgis.PyQt.QtCore` (NOT `PyQt5.QtCore`, NOT `qgis.core`).",
    "If you need to use `QColor` it should be imported from `qgis.PyQt.QtGui` (NOT `PyQt5.QtGui`).",
    "If you need to use `QgsVectorLayer`, it should always be imported from qgis.core.",
    "If you need to load a raster layer, use this format `output_layer = QgsRasterLayer(output_path, 'Slope Output')`",
    "When adding a new field to the a shapefile, it should be noted that the maximum length for field name is 10, so avoid mismatch in the fieldname in the data and in the calculation.",
    "When creating a thematic map after joining attributes to a shapefile, ensure that the field name length for the attribute use for thematic map do not exceed 10, if it exceed 10, truncate the field name (E.g, 'White_Population' can be truncated to 'White_Popu'). Adhering to the 10 field name length limit ensures consistency and prevents errors during thematic map creation.",

    # === [性能阈值] ===
    "Before calling `native:creategrid` or `qgis:regularpoints` with sub-meter spacing (< 1m) on layers in a metric CRS, estimate the resulting cell count from the extent and spacing. If it would exceed 5,000,000 cells, the parameter is almost certainly mis-stated; ask for clarification rather than running it.",
    "MANDATORY pre-check for `native:creategrid` and `qgis:regularpoints` whenever EXTENT is computed at runtime (e.g. `extent = some_layer.extent()` or `f'{e.xMinimum()},...'`): immediately after obtaining the extent and BEFORE calling `processing.run`, you MUST compute the projected cell count: `width = abs(extent.xMaximum() - extent.xMinimum()); height = abs(extent.yMaximum() - extent.yMinimum()); est_cells = (width / HSPACING) * (height / VSPACING)`. If `est_cells > 5_000_000`, DO NOT call `processing.run` — instead, increase HSPACING/VSPACING to a sensible value so `est_cells <= 5_000_000`, `print(f'Adjusted grid spacing from X to Y because est_cells={est_cells:.0f}')`, then proceed. Skipping this check on a polygon that becomes 65 km × 91 km after reprojection at 2 m spacing produces ~1.5 BILLION features and SIGABRTs the QGIS subprocess (case 049). Note: if input data is in geographic CRS (degrees) and you reproject to a metric CRS (meters), the extent jumps from ~1° to ~100,000 m — always estimate AFTER the reprojection.",

    # === [算法 ID 易错] ===
    "When using Raster calculator 'native:rastercalculator' is wrong rather the correct ID for the Raster Calculator algorithm is 'native:rastercalc'.",
    "Algorithm `native:savevectorlayer` does NOT exist; to save a vector use `native:savefeatures`.",
    "Algorithm `native:executesql` does NOT exist in QGIS 3.40; use `qgis:executesql` (or `gdal:executesql` for OGR / `native:postgisexecutesql` for PostGIS).",
    "GDAL grid interpolation has explicit per-method IDs: use `gdal:gridinversedistance` (IDW), `gdal:gridinversedistancenearestneighbor`, `gdal:gridnearestneighbor`, `gdal:gridaverage`, `gdal:gridlinear`, or `gdal:griddatametrics`. The shorthand IDs `gdal:grid`, `gdal:grididw`, `gdal:gridinversedistanceweighted` do NOT exist and will fail with 'Algorithm gdal:... not found'. For point-to-raster IDW from QGIS-side, `qgis:idwinterpolation` is also available.",
    "GRASS algorithms in this Qt6 LTR build use the `grass:` prefix ONLY. NEVER write `grass7:` (e.g. write `grass:r.composite`, `grass:r.neighbors`, `grass:r.viewshed`, `grass:r.watershed` — `grass7:*` IDs do NOT exist and will fail with 'Algorithm grass7:... not found').",
    "When the task asks for the **difference** of two layers, use `native:difference`. Use `native:symmetricaldifference` ONLY when the task literally says 'symmetric(al) difference'.",
    "When using `gdal:proximity`, ensure all shapefiles are rasterized before using them",
    "When using `native:selectbylocation` (or any select-by-* algorithm), the algorithm does NOT produce a new output layer — it creates a selection set on the INPUT layer in-place. Therefore `result['OUTPUT']` IS the input layer object itself, not a separate layer. To export selected features, call `native:saveselectedfeatures` with the ORIGINAL INPUT LAYER (the one you passed as INPUT), NOT `result['OUTPUT']`.",
    "When creating charts/plots (bar, scatter, box, etc.): use `seaborn` by default; save the result (html/image) to the output directory and only print the file path; do NOT load the output into QGIS. For scatter plots, use 'qgis:vectorlayerscatterplot' (NOT 'native:scatterplot' or 'qgis:scatterplot').",

    # === [关键: 中间产物落盘验证 - 避免静默失败] ===
    "MANDATORY after EVERY `processing.run(...)` that writes to a real file path (not TEMPORARY_OUTPUT): immediately add `assert os.path.exists(<the-output-path>), f'<algorithm name> did not produce output: {<the-output-path>}'`. The assert MUST include a descriptive f-string message identifying WHICH step failed — a bare `assert os.path.exists(p)` raises a useless `AssertionError` with no location info, forcing the debugger to guess. Examples of CORRECT asserts (do this): `assert os.path.exists(aspect_path), f'qgis:aspect did not write: {aspect_path}'` / `assert os.path.exists(buffer_path), f'native:buffer step 4 silent fail: {buffer_path}'`. Examples of WRONG asserts (do NOT do this): `assert os.path.exists(p)` / `assert os.path.exists(result['OUTPUT'])`. Some QGIS/GDAL combinations (notably `gdal:polygonize`, `gdal:rastercalculator`, `gdal:rasterize`) return a result dict with success status but silently fail to write the file when the input is empty/all-NoData or has an unsupported driver. Without this assert + message, downstream steps fail with cryptic 'Could not load source layer' errors and waste debug rounds.",
    "When using `gdal:rastercalculator`, NEVER set `NO_DATA` unless the user explicitly asks to mask a specific value as nodata. Setting `NO_DATA=0` is a common pitfall: it marks ALL cells where the FORMULA result equals 0 as nodata, which for binary masks `(A>X)*1` discards the entire 'false' region and can produce a raster of all-nodata if the condition is rarely true. If you need a binary mask, write the 0/1 raster without NO_DATA and filter downstream (e.g., polygonize then `native:extractbyattribute` on `DN=1`).",
    "NEVER import `QgsRasterCalculator` or `QgsRasterCalculatorEntry` when calling `processing.run('native:rastercalc', ...)` — the processing-algorithm path does NOT need those C++ classes. If you do need them (e.g. low-level direct API), import from `qgis.analysis`, NOT `qgis.core`: `from qgis.analysis import QgsRasterCalculator, QgsRasterCalculatorEntry`. Importing from `qgis.core` raises `cannot import name 'QgsRasterCalculatorEntry' from 'qgis.core'`. Default to the processing.run path; only fall back to direct API if processing.run is insufficient.",
    "Use forward slashes `/` consistently in file paths (e.g. `f'{output_dir}/PA_aspect.tif'`), NOT `os.path.join` on Windows — `os.path.join` produces mixed-slash paths like `F:/foo/output\\bar.tif` that some GDAL drivers handle inconsistently. If you must use `os.path.join`, follow with `.replace(os.sep, '/')`.",
    "PREFER `native:rastercalc` over `gdal:rastercalculator` on Windows. The GDAL variant shells out to `gdal_calc.bat`, and cmd.exe mis-parses the FORMULA expression (`>`, `*`, `(`, `)` are special characters) and the forward-slash file paths, causing 'The filename, directory name, or volume label syntax is incorrect' / 'Process returned error code 1'. The `native:rastercalc` path is pure QGIS Python with no shell layer and always works. Convert any `(A>X)*1` style formula to `\"<layer_name>@<band>\" > X` expression with `native:rastercalc`, e.g.: parameters = {'LAYERS': [layer], 'EXPRESSION': '\"aspect@1\" > 100', 'OUTPUT': path}. Only fall back to `gdal:rastercalculator` if the task explicitly demands a numpy function (e.g. `logical_and`, `where`) that native:rastercalc cannot express.",

    # === [输出落盘] ===
    "If you are using the processing algorithm, make the output parameter to be the user's specified output directory . And use `QgsVectorLayer` to load the feature as a new layer: For example `Buffer_layer = QgsVectorLayer(result['OUTPUT'], 'Buffered output', 'ogr')` for the case of a shapefile.",
    "Similarly, if you used geopandas to generate a new layer, use `QgsVectorLayer` to load the feature as a new layer: For example `Buffer_layer = QgsVectorLayer(result['OUTPUT'], 'Buffered output', 'ogr')` for the case of a shapefile.",
    "Whenever a new layer is being saved, ensure the code first checks if a file with the same name already exists in the output directory, and if it doesn't, go ahead and save with the original name, but if same name exist, append a number to the filename to create a unique name, thereby avoiding any errors related to overwriting or saving the layer.",
    "When naming any output layer, choose a name that is concise, descriptive, easy to read, and free of spaces.",
    "Ensure that temporary layer is not used as the output parameter",
    "When performing multi-step tasks that involve creating intermediary layers, ensure there is a waiting period before proceeding to the next step. This allows enough time for the intermediary layers to be fully created, preventing errors such as 'data not found.'",
    "For algorithms whose primary purpose is producing a new layer (e.g. `native:dissolve`, `native:meancoordinates`, `native:zonalstatisticsfb`), set `OUTPUT` to a real file path inside the workspace directory; if you only pass `TEMPORARY_OUTPUT` the result lives in memory and the run produces no inspectable artifact.",

    # === [输出验证] ===
    "When performing any operation that generates an output vector or raster layer , include the code to load the resulting output layer into QGIS",
    "When performing any operation such as counting of features, generating plots (scatter plot, bar plot), etc., which do not require creation of new layers, do not include load the resulting output layer into QGIS rather print the result",
    "When using tool that is used to generate counts e.g 'Vector information(gdal:ogrinfo), Count points in polygon(native:countpointsinpolygon), etc., don't just print the file path (e.g the html path) but also ensure you print the count(e.g Number of conties)",
    "NOTE: `vector_layer.featureCount()` can be use to generate the count of features",
    "If you are printing any file path (e.g html, png, etc.), Do not include any additional information. just print the file path",
    "For tasks that contains interrogative words such as ('how', 'what', 'why', 'when', 'where', 'which'), ensure that no layers are loaded into the QGIS, instead the result should be printed",
    "After `native:createpointslayerfromtable` or any CSV-to-points conversion, immediately check `featureCount()` on the result. If it is 0, print a clear failure message and stop; never let a downstream step continue with an empty layer.",
    "When filtering or querying features by a string attribute value (e.g., county name, city name), NEVER hard-code the target string blindly. Always perform a runtime lookup first to find the exact matching value: (1) get all unique values with `all_vals = list(layer.uniqueValues(layer.fields().indexOf('FIELD_NAME')))`; (2) find the best match case-insensitively: `match = next((v for v in all_vals if str(v).lower() == target.lower()), None)`; (3) if no exact match, try partial: `match = next((v for v in all_vals if target.lower() in str(v).lower() or str(v).lower() in target.lower()), None)`; (4) use `match` (the real value) in the filter expression. This prevents silent failures from format differences (e.g. 'BC' vs 'BC County', 'New York' vs 'NEW YORK').",

    # === [数据加载] ===
    "When loading a CSV layer as a layer, use this: `'f'file///{csv_path}?delimeter=,''`, assuming the csv is comma-separated, but use the csv_path directly for the Input parameter in join operations.",
    "If you are to use processing algorithm, you do not need to include the code to load a data",

    # === [代码骨架] ===
    "Think step by step",
    "If you need to perform more than one operation, you must perform the operations step by step",
    "Use the selected tools provided",
    "DO NOT include the QGIS initialization code in the script",
    f"When using QGIS processing algorithm, use `QgsVectorLayer` to load shapefiles. For example `output_layer = QgsVectorLayer(result['OUTPUT'], 'Layer Name', 'ogr')`",
    "Put your reply into a Python code block, Explanation or conversation can be Python comments at the begining of the code block(enclosed by ```python and ```).",
    "The python code is only in a function named in with the operation name e.g 'perform_idw_interpolation()'. The last line is to execute this function.",
    "Only do the reprojection as needed when 1) e.g., calculating distances/buffers that needs projected CRS, and the layers have different projections",
    "Put your reply into a Python code block (enclosed by python and ), NO explanation or conversation outside the code block.",
    "You are not limited to QGIS python functions/tools, you can also use other python functions asuch as geopandas, numpy, scipy etc.",
    "DO NOT add validity check and DO NOT raise any exception.",
    "DO NOT raise exceptions messages.",
]

# ------------- OPERATION_CODE REVIEW------------------------------------------------------
operation_code_review_role = r''' A professional Geo-information scientist and Python developer with over 20 years of experience in Geographic Information Science (GIS). You are highly knowledgeable about spatial data processing and coding, and you specialize in code review. You are meticulous and enjoy identifying potential bugs and data misunderstandings in code.
'''

operation_code_review_task_prefix = r'''You are reviewing generated Python code for a QGIS geoprocessing task.
Your context includes:
- The **Execution Plan** (in the system message under "=== Current Plan ===") specifying the tools, parameters, and step order.
- The **Tool Documentation** with correct parameter names and code examples.
- The **Data Properties** describing the actual dataset (fields, CRS, geometry type).

Your job: compare the code against the plan, documentation, and data, then fix any issues.
If the code is correct, return it unchanged. Always return the complete corrected code.'''

operation_code_review_requirement = [
    # === [核心审查：对照 Plan] ===
    "Verify the code implements EVERY step in the Execution Plan in the correct order. Flag missing or out-of-order steps.",
    "Verify each tool ID in the code matches the tool ID specified in the plan (e.g. 'native:joinbylocationsummary', not a made-up ID).",
    "Verify tool parameters (PREDICATE, SUMMARIES, FIELD, etc.) match the plan and the tool documentation examples.",
    "Verify input/output layer paths: the code must use the exact data paths provided, not placeholder paths.",

    # === [数据一致性] ===
    "Verify field names used in the code actually exist in the data (check the Data Properties section). Watch out for truncation (shapefile 10-char limit) and case sensitivity.",
    "Verify CRS handling: only add reprojection if the plan or data properties indicate mismatched CRS.",

    # === [必选参数] ===
    "Verify any `gdal:rasterize`, `gdal:proximity`, or `gdal:rasterize_over` call sets `UNITS` explicitly. Missing `UNITS` causes 'Incorrect parameter value for UNITS' and the algorithm aborts.",
    "Verify `gdal:viewshed` calls explicitly set both `INPUT` (DEM raster) and `OBSERVER` (point layer). Missing either causes 'Could not load source layer for INPUT/OBSERVER: no value specified for parameter'.",
    "Verify `gdal:rasterize_over` `FIELD` is a non-empty real attribute on the input. If no field exists, the code should switch to `gdal:rasterize` with a `BURN` value.",
    "Verify multi-input tasks consume every supplied layer at least once. If the task lists N input files but the code only references one, flag the dropped inputs and add the missing processing.",

    # === [类型限制] ===
    "Verify `UNITS` for any gdal raster algorithm is an integer (0=Pixels, 1=Georeferenced units), never a string like 'Georeferenced units'.",
    "When using ANY GDAL tool (any `gdal:*` algorithm), verify the INPUT value is a QgsVectorLayer or QgsRasterLayer object, NOT a raw path string. A raw path silently causes the tool to skip execution without raising any error.",
    "When `processing.run(...)` is called with `'OUTPUT': 'TEMPORARY_OUTPUT'`, verify that `result['OUTPUT']` is used directly as a layer object. Do NOT wrap it in `QgsVectorLayer(result['OUTPUT'], ...)` — it is already a QgsVectorLayer, and double-wrapping raises `TypeError: unexpected type 'QgsVectorLayer'`.",
    "If you need `QColor`, import it from `qgis.PyQt.QtGui` (NEVER from `PyQt5.QtGui` — this is a Qt6 build).",
    "When adding fields to shapefiles, field names must not exceed 10 characters. Ensure consistency between field names in data and in calculations.",

    # === [性能阈值] ===
    "Verify `native:creategrid` and `qgis:regularpoints` spacings are sensible. Sub-meter spacing on metric-CRS layers with county-sized extents will generate billions of features and time out — require coarser spacing or a smaller extent.",

    # === [算法 ID 易错] ===
    "The Raster Calculator algorithm ID must be 'native:rastercalc', NOT 'native:rastercalculator'.",
    "Algorithm `native:savevectorlayer` does NOT exist; to save a vector use `native:savefeatures`.",
    "Algorithm `native:executesql` does NOT exist; rewrite to `qgis:executesql`.",
    "Any `grass7:*` algorithm ID is invalid in this Qt6 LTR build — rewrite to the same name with the `grass:` prefix.",
    "Verify `native:difference` is used when the task says 'difference', and `native:symmetricaldifference` only when the task literally says 'symmetric(al) difference'.",
    "Scatter plot must use 'qgis:vectorlayerscatterplot', NOT 'native:scatterplot' or 'qgis:scatterplot'.",
    "When using `gdal:proximity`, ensure shapefiles are rasterized first.",
    "Verify EVERY `processing.run(...)` that writes to a real file path is followed by `assert os.path.exists(<path>)`. Missing assertion = silent failure waiting to happen on `gdal:polygonize`/`gdal:rastercalculator`/`gdal:rasterize`. Flag missing assertions as bugs.",
    "Verify `gdal:rastercalculator` calls do NOT set `NO_DATA=0` (or any value that the FORMULA produces normally). Setting `NO_DATA=0` for a binary mask `(A>X)*1` mis-marks all 'false' cells as nodata. Flag this pattern.",
    "Verify `QgsRasterCalculator` / `QgsRasterCalculatorEntry` are NEVER imported from `qgis.core` (they live in `qgis.analysis`). Also flag any `from qgis.analysis import QgsRasterCalculator*` when the code is using the `processing.run('native:rastercalc', ...)` path — that path does not need those classes at all; the import is dead weight at best, a future error vector at worst. Remove unused imports.",
    "Verify file paths use forward slashes consistently. Flag `os.path.join` results in GDAL `INPUT`/`OUTPUT` parameters unless followed by `.replace(os.sep, '/')`.",
    "When using `native:selectbylocation` (or any select-by-* algorithm), verify that `native:saveselectedfeatures` receives the ORIGINAL INPUT LAYER (the layer passed as INPUT to the select algorithm), NOT `result['OUTPUT']`. The select algorithm modifies the input layer in-place — `result['OUTPUT']` is the same object as the input layer, but relying on it is error-prone. Always use the original input layer variable.",

    # === [输出落盘] ===
    "Output must be saved to the user's workspace directory, not a temporary path. Ensure that temporary layer is not used as the output parameter.",
    "Verify final-output algorithms (e.g. `native:dissolve`, `native:meancoordinates`, `native:zonalstatisticsfb`) write to a real workspace path, not `TEMPORARY_OUTPUT`. A successful run that produces no inspectable artifact must be treated as a failure.",
    "If a file with the same output name already exists, append a number to avoid overwriting.",
    "When naming output layers, choose concise, descriptive names without spaces.",
    "For multi-step tasks with intermediate layers, add a small delay or flush between steps to prevent 'data not found' errors.",

    # === [输出验证] ===
    "Verify CSV→points conversions (e.g. `native:createpointslayerfromtable`) check `featureCount() > 0` before continuing. An empty result must print a clear failure message; never let a downstream step run on an empty layer.",
    "When the code filters or queries features by a string attribute value, verify that it does NOT hard-code the string literal directly into the filter. The correct pattern is: (1) `all_vals = list(layer.uniqueValues(layer.fields().indexOf('FIELD')))`, (2) find exact or partial match case-insensitively, (3) use the matched real value in the expression. If the code skips this lookup and hard-codes the string (e.g. `\"NAME\" = 'BC County'` without prior lookup), rewrite it to use the runtime lookup pattern.",
    "When performing operations that generate output layers (vector/raster), include code to load the result into QGIS.",
    "When performing operations that only produce counts, stats, or plots (no new layers), do NOT load layers — just print the result.",
    "For interrogative tasks (how, what, why, when, where, which), only print the answer, do not create or load layers.",

    # === [代码骨架] ===
    "Put your reply into a single Python code block (enclosed by ```python and ```). Explanations go as comments at the beginning of the code block.",
    "The python code must be wrapped in a function named after the operation (e.g. 'perform_idw_interpolation()'). The last line must call this function.",
    "Do NOT include QGIS initialization code in the script.",
    "When using QGIS processing algorithms, use `QgsVectorLayer` to load results. Example: `output_layer = QgsVectorLayer(result['OUTPUT'], 'Layer Name', 'ogr')`.",
    "Similarly for geopandas outputs, use `QgsVectorLayer` to load the saved file into QGIS.",
    "The data is already loaded in QGIS — use the provided data paths directly, do not re-load data.",
    "The code runs inside QGIS Python environment. Third-party libraries must be explicitly imported if needed.",
    "When creating charts/plots, use `seaborn` by default. Save the output file to the workspace directory and print only the file path.",
]



# --------------- SMART DEBUGGING CONSTANTS ---------------
# Initialize smart debugger instance
debugger_instance = smart_debugger.SmartDebugger()

# Enhanced debugging role with smart capabilities
debug_role = r'''A professional Geo-information scientist with high proficiency in using QGIS and programmer good at Python. You have worked on Geographic information science more than 20 years, and know every detail and pitfall when processing spatial data and coding. You have advanced debugging capabilities with pattern recognition, contextual analysis, and adaptive learning from debugging history. You analyze errors intelligently and provide targeted solutions.
'''

debug_task_prefix = r'You need to correct the code of a program based on the given error information, then return the complete corrected code. Use smart debugging techniques to analyze the error pattern and provide contextual solutions.'

def get_smart_debug_requirements(error_msg="", code="", operation_type=None):
    """Generate dynamic debugging requirements based on error analysis"""

    # Get smart suggestions from the debugger
    smart_suggestions = debugger_instance.generate_debug_suggestions(error_msg, code, operation_type)

    # Base requirements that are always included
    base_requirements = [
        "Analyze the error pattern and apply contextual debugging strategies",
        "Elaborate your reasons for revision based on error analysis",
        "You must return the entire corrected program in only one Python code block(enclosed by ```python and ```); DO NOT return the revised part only.",
        "If you need to perform more than one operation, you must perform the operations step by step",
        "NOTE: You are not limited to QGIS tools only, you can also make use of python libraries",
        "When using `QgsVectorLayer`, it should always be imported from `qgis.core`.",
        "When using QGIS processing algorithm, use `QgsVectorLayer` to load shapefiles. For example `output_layer = QgsVectorLayer(result['OUTPUT'], 'Layer Name', 'ogr')`",
        "If you need to use `QColor` it should be imported from `qgis.PyQt.QtGui` (NEVER `PyQt5.QtGui` — this is a Qt6 build).",
        "DO NOT include the QGIS initialization code in the script",
        "Make your codes to be concise/short and accurate",
        "`QVariant` should be imported from `qgis.PyQt.QtCore` (NEVER from `PyQt5.QtCore` or `qgis.core` — this is a Qt6 build).",
        "When running processing algorithms, use `processing.run('algorithm_id', {parameter_dictionary})`",
        "Put your reply into a Python code block (enclosed by python and ), NO explanation or conversation outside the code block.",
        "When using Raster calculator 'native:rastercalculator' is wrong rather the correct ID for the Raster Calculator algorithm is 'native:rastercalc'.",
        "When loading a CSV layer as a layer, use this: `'f'file///{csv_path}?delimeter=,''`, assuming the csv is comma-separated, but use the csv_path directly for the Input parameter in join operations.",
        "For tasks that contains interrogative words such as ('how', 'what', 'why', 'when', 'where', 'which'), ensure that no layers are loaded into the QGIS, instead the result should be printed",
        "When creating charts/plots: use `seaborn` by default; save to output directory and only print the file path (no extra comments); for scatter plots use 'qgis:vectorlayerscatterplot' (NOT 'native:scatterplot').",
        "When using tool that is used to generate counts e.g 'Vector information(gdal:ogrinfo), Count points in polygon(native:countpointsinpolygon), etc., ensure you print the count",
        "NOTE: `vector_layer.featureCount()` can be use to generate the count of features",
        "Whenever a new layer is being saved, ensure the code first checks if a file with the same name already exists in the output directory, and if it does, append a number (e.g filename_1, filename_2, etc) to the filename to create a unique name, thereby avoiding any errors related to overwriting or saving the layer.",
        "When naming any output layer, choose a name that is concise, descriptive, easy to read, and free of spaces.",
        "Ensure that temporary layer is not used as the output parameter",
        "When adding a new field to the a shapefile, it should be noted that the maximum length for field name is 10, so avoid mismatch in the fieldname in the data and in the calculation."
    ]

    # Add smart suggestions as requirements
    smart_requirements = [f"Smart Debug Suggestion: {suggestion}" for suggestion in smart_suggestions]

    return base_requirements + smart_requirements

# Legacy support - static requirements for backward compatibility
debug_requirement = [
    "Elaborate your reasons for revision.",
    "If same error persist, please fallback to the best function/tools you are mostly familiar with",
    "You must return the entire corrected program in only one Python code block(enclosed by ```python and ```); DO NOT return the revised part only.",
    "If you need to perform more than one operation, you must perform the operations step by step",
    "NOTE: You are not limited to QGIS tools only, you can also make use of python libraries",
    "If the generated codes for the selected tools provided are not working you can use other python functions such as geopandas, numpy, scipy etc.",
    "When using `QgsVectorLayer`, it should always be imported from `qgis.core`.",
    "When using QGIS processing algorithm, use `QgsVectorLayer` to load shapefiles. For example `output_layer = QgsVectorLayer(result['OUTPUT'], 'Layer Name', 'ogr')`",
    "If you need to use `QColor` it should be imported from `qgis.PyQt.QtGui` (NEVER `PyQt5.QtGui` — this is a Qt6 build).",
    "Use the latest qgis libraries and methods.",
    "DO NOT include the QGIS initialization code in the script",
    "Make your codes to be concise/short and accurate",
    "`QVariant` should be imported from `qgis.PyQt.QtCore` (NEVER from `PyQt5.QtCore` or `qgis.core` — this is a Qt6 build).",
    "NOTE: `QgsVectorJoinInfo` may not always be available or accessible in recent QGIS installations, thus use `QgsVectorLayerJoinInfo` instead",
    "When running processing algorithms, use `processing.run('algorithm_id', {parameter_dictionary})`",
    "Put your reply into a Python code block (enclosed by python and ), NO explanation or conversation outside the code block.",
    "When using `QgsVectorLayer `, it should always be imported from qgis.core.",
    "When using Raster calculator 'native:rastercalculator' is wrong rather the correct ID for the Raster Calculator algorithm is 'native:rastercalc'.",
    "NOTE: When saving a file (e.g shapefile, csv file etc) to the any path/directory, first check if the the filename already exists in the specified path/directory. If it does, overwrite the file. If the file does not exist, then save the new file directly",
    "NOTE, when a one data path is provided, you DO NOT need to perform join.",
    "If you need to use any field from the input shapefile layer, first access the fields (example code: `fields = input_layer.fields()`), then select the appropriate field carefully from the list of fields in the layer.",
    "When loading a CSV layer as a layer, use this: `'f'file///{csv_path}?delimeter=,''`, assuming the csv is comma-separated, but use the csv_path directly for the Input parameter in join operations.",
    "For tasks that contains interrogative words such as ('how', 'what', 'why', 'when', 'where', 'which'), ensure that no layers are loaded into the QGIS, instead the result should be printed",
    "When creating charts/plots: use `seaborn` by default; save to output directory and only print the file path (no extra comments); for scatter plots use 'qgis:vectorlayerscatterplot' (NOT 'native:scatterplot').",
    "When using tool that is used to generate counts e.g 'Vector information(gdal:ogrinfo), Count points in polygon(native:countpointsinpolygon), etc., ensure you print the count",
    "NOTE: `vector_layer.featureCount()` can be use to generate the count of features",
    "When using the processing algorithm, make the output parameter to be the user's specified output directory . And use `QgsVectorLayer` to load the feature as a new layer: For example `output_layer = QgsVectorLayer(result['OUTPUT'], 'Layer Name', 'ogr')` for the case of a shapefile.",
    "Similarly, if you used geopandas to generate a new layer, use `QgsVectorLayer` to load the feature as a new layer: For example `output_layer = QgsVectorLayer(result['OUTPUT'], 'Layer Name', 'ogr')` for the case of a shapefile.",
    "Whenever a new layer is being saved, ensure the code first checks if a file with the same name already exists in the output directory, and if it does, append a number (e.g filename_1, filename_2, etc) to the filename to create a unique name, thereby avoiding any errors related to overwriting or saving the layer.",
    "When naming any output layer, choose a name that is concise, descriptive, easy to read, and free of spaces.",
    "Ensure that temporary layer is not used as the output parameter",
    "When adding a new field to the a shapefile, it should be noted that the maximum length for field name is 10, so avoid mismatch in the fieldname in the data and in the calculation."
]



# *******************************************************************************
#DATA EYE CONSTANT
# ********************************************************************************



table_formats = ["CSV", 'Parquet', "TXT"]
vector_formats = ["ESRI shapefile", "GeoPackage", "KML", "geojson"]
raster_formats = ["Tiff", "JPEG", "PNG", "ERDAS IMG", "JP2", "HDF5", "HDF"]

support_formats = table_formats + vector_formats + raster_formats


eye_role = r'''A professional Geo-information scientist and programmer good at Python. You have worked on Geographic information science more than 20 years, and know every detail and pitfall when processing spatial data and coding. You are a very careful person to follow instruction exactly in work.
'''

mission_prefix = r'''You will be provided with brief geospatial data description and locations for a spatial analysis task.
You need to extract the data path, URL, API, an format from a task and data description. Every given data should be included, and keep the original order.
Below are the description of your reply parameters:
- location: the disk path, URL, or API to access the data. Such as r"C:\test.zip".
- format: the format of data, which belongs one of ['TXT', 'CSV', 'Parquet', 'ESRI shapefile', 'KML', 'geojson', 'HDF', 'HDF5', 'LAS/LAZ', 'XLS', 'GML', 'GeoPackage', 'Tiff', 'JPEG', 'PNG', 'URL', 'REST API', 'other']
'''

class Data(BaseModel):
    location: str
    format: str


class Data_locations(BaseModel):
    data_locations: list[Data]


# ============================================================================
# Phase 3: 结构化工具选择输出格式
# ============================================================================

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

Rules:
- input_layer must be an actual filename from the Data Overview
- key_parameters should include only the most important 2-3 parameters
- For chained operations, the next step's input_layer should reference the previous step's output
- If new fields are created, specify them in key_parameters or describe in output_description
"""

structured_tool_selection_example_simple = """{
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
}"""

structured_tool_selection_example_complex = """{
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
}"""

TOOL_WHITELIST = [
    # --- Projection / CRS ---
    "native:reprojectlayer",
    "native:assignprojection",
    
    # --- Field Operations ---
    "native:fieldcalculator",
    "native:addfieldtoattributestable",
    "native:deletecolumn",
    "native:renametablefield",
    "native:retainfields",
    "native:refactorfields",
    "native:addautoincrementalfield",
    "native:addxyfields",
    
    # --- Selection / Extraction ---
    "native:extractbyattribute",
    "native:extractbyexpression",
    "native:extractbylocation",
    "native:extractbyextent",
    "native:extractwithindistance",
    "native:saveselectedfeatures",
    "native:selectbylocation",
    "native:selectwithindistance",
    "qgis:selectbyattribute",
    "qgis:selectbyexpression",
    
    # --- Join ---
    "native:joinattributesbylocation",
    "native:joinattributestable",
    "native:joinbylocationsummary",
    "native:joinbynearest",
    
    # --- Geometry Basics ---
    "native:buffer",
    "native:clip",
    "native:intersection",
    "native:difference",
    "native:union",
    "native:dissolve",
    "native:fixgeometries",
    "native:multiparttosingleparts",
    "native:centroids",
    "native:creategrid",
    
    # --- Data Management ---
    "native:mergevectorlayers",
    "native:package",
    "native:orderbyexpression",
    "native:removeduplicatesbyattribute",
    "native:deleteduplicategeometries",
    "qgis:exportaddgeometrycolumns",
    
    # --- Statistics ---
    "native:basicstatisticsforfields",
    "native:zonalstatisticsfb",
    "qgis:listuniquevalues",
    
    # --- Raster Basics ---
    "gdal:warpreproject",
    "gdal:cliprasterbymasklayer",
    "gdal:cliprasterbyextent",
    "gdal:merge",
    "gdal:translate",
    "gdal:rastercalculator",
]
