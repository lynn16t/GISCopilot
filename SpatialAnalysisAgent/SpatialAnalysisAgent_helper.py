import ast
import io
import sys
import re
import traceback
import json
import configparser
from collections import deque
import os
import requests
import warnings
from openai import OpenAI
from SpatialAnalysisAgent_ModelProvider import create_unified_client, ModelProviderFactory
# Fix sys.stderr/sys.stdout being None during QGIS initialization
# (prevents NumPy/GDAL AttributeError: 'NoneType' object has no attribute 'write').
# Note: assigning stderr = stdout is unsafe when stdout itself is None,
# so we always fall back to an in-memory stream.
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

import networkx as nx
from pyvis.network import Network


# Suppress NumPy 2.0 compatibility warning from GDAL
# QGIS 3.34.12 uses NumPy 1.x, but modern packages use NumPy 2.x
# This warning doesn't halt execution, it's just a version mismatch alert
warnings.filterwarnings("ignore", message=".*A module that was compiled using NumPy 1.x.*")
warnings.filterwarnings("ignore", message=".*NumPy.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, module=".*gdal.*")

# Get the directory of the current script
current_script_dir = os.path.dirname(os.path.abspath(__file__))
# Add the directory to sys.path
if current_script_dir not in sys.path:
    sys.path.append(current_script_dir)

json_path = os.path.join(current_script_dir, 'Tools_Documentation', 'qgis_tools_for_rag.json')

def load_config():
    config = configparser.ConfigParser()
    config_path = os.path.join(current_script_dir, 'config.ini')
    config.read(config_path)
    return config


def load_OpenAI_key():
    config = load_config()  # Re-read the configuration file
    OpenAI_key = config.get('API_Key', 'OpenAI_key')
    return OpenAI_key


def create_openai_client():
    OpenAI_key = load_OpenAI_key()
    return OpenAI(api_key=OpenAI_key)

def get_question_id(user_api_key):
    import requests
    url = f"https://www.gibd.online/api/request-question-id"
    payload = {
            "service_name": "GIS Copilot",
            "user_api_key": user_api_key}
    response = requests.post(url, json=payload)
    # response.text
    if response.status_code == 201:
        return response.json()["question_id"]
    else:
        error_msg = f"Error {response.status_code}: {response.text}"
        print(error_msg)
        raise Exception(error_msg)  # This will terminate execution


import SpatialAnalysisAgent_Constants as constants


def generate_task_name_with_gpt(specific_model_name, task_description):
    prompt = f"Given the following task description: '{task_description}',give the best task that represents this task.\n\n" + \
             f"Provide the task name in just one or two words. \n\n" + \
             f"Underscore '_' is the only alphanumeric symbols that is allowed in a task name. A task_name must not contain quotations or inverted commas example or space. \n"
    # Fallback to basic OpenAI client
    client = create_openai_client()
    response = client.chat.completions.create(
        model=specific_model_name,
        messages=[
            {"role": "user", "content": prompt},
        ])
    task_name = response.choices[0].message.content
    return task_name

# Add this function to generate the task name using UNIFIED MODEL PROVIDER
def generate_task_name_with_model_provider(request_id, model_name, stream, task_description, reasoning_effort=None):
    prompt = f"Given the following task description: '{task_description}',give the best task that represents this task.\n\n" + \
             f"Provide the task name in just one or two words. \n\n" + \
             f"Underscore '_' is the only alphanumeric symbols that is allowed in a task name. A task_name must not contain quotations or inverted commas example or space. \n"

    # Use the unified model provider
    # try:
    from SpatialAnalysisAgent_ModelProvider import create_unified_client
    client, provider = create_unified_client(model_name)
    messages=[
        {"role": "user", "content": prompt},
    ]
    # if reasoning_effort:
    #     print(f"[DEBUG] generate_task_name: reasoning_effort = {reasoning_effort}")
    # Generate response using the provider
    kwargs = {}
    # Only pass reasoning_effort for GPT-5 models
    if reasoning_effort and model_name in ['gpt-5', 'gpt-5.1', 'gpt-5.2']:
        kwargs['reasoning_effort'] = reasoning_effort
        # print(f"[DEBUG] generate_task_name: reasoning_effort ENABLED for {model_name}")
    # elif reasoning_effort:
        # print(f"[DEBUG] generate_task_name: reasoning_effort IGNORED for {model_name} (not supported)")



    return unified_llm_call(
        request_id = request_id,
        messages = messages,
        model_name=model_name,
        stream=stream,
        **kwargs)


def create_Query_tuning_prompt(task, data_overview, knowledge_text=""):
    Query_tuning_requirement_str = '\n'.join(
        [f"- {line}" for idx, line in enumerate(constants.Query_tuning_requirement)])

    Query_tuning_instructions_str = '\n'.join(
        [f"{idx + 1}. {line}" for idx, line in enumerate(constants.Query_tuning_instructions)])

    data_overview_str = '\n'.join(
        [f"{idx + 1}. {line}" for idx, line in enumerate(data_overview)])

    Output_Sample_str = '\n'.join(
        [f"{line}" for idx, line in enumerate(constants.Output_Sample)])

    knowledge_section = ""
    if knowledge_text:
        knowledge_section = f"""
    Project Knowledge:
    {knowledge_text}
"""

    prompt = f"""{constants.Query_tuning_role}

    {constants.Query_tuning_prefix}

    REQUIREMENTS:
    {Query_tuning_requirement_str}

    INSTRUCTIONS:
    {Query_tuning_instructions_str}

    Data Overview:
    {data_overview_str}
{knowledge_section}
    User Query:
    "{task}"

    Output Sample:
    {Output_Sample_str}
    """

    return prompt




def create_OperationIdentification_promt(task):
    OperationIdentification_requirement_str = '\n'.join(
        [f"{idx + 1}. {line}" for idx, line in enumerate(constants.OperationIdentification_requirements)])

    prompt = f"Your role: {constants.OperationIdentification_role} \n" + \
             f"Your mission: {constants.OperationIdentification_task_prefix}: " + f"{task}\n" + \
             f"Requirements: \n{OperationIdentification_requirement_str} \n\n" + \
             f"Customized tools:\n{constants.tools_index}\n" + \
             f"Your reply examples, depending on the task. Example 1: {constants.OperationIdentification_reply_example_1}\n " + " OR " + f"Example 2: {constants.OperationIdentification_reply_example_2}\n" + " OR " + f"Example 3: {constants.OperationIdentification_reply_example_3}"
    return prompt


def create_ToolSelect_prompt(task, data_path, candidate_tools_str=None):
    ToolSelect_requirement_str = '\n'.join(
        [f"{idx + 1}. {line}" for idx, line in enumerate(constants.ToolSelect_requirements)])
    data_path_str = '\n'.join([f"{idx + 1}. {line}" for idx, line in enumerate(data_path)])

    # 如果传入了 embedding 检索的候选工具，用它；否则回退到全量 tools_index
    tools_str = candidate_tools_str if candidate_tools_str else str(constants.tools_index)

    prompt = f"Your role: {constants.ToolSelect_role} \n" + \
             f"Your mission: {constants.ToolSelect_prefix}: " + f"{task}\n\n" + \
             f"Based on the provided data {data_path_str}\n" + \
             f"Requirements: \n{ToolSelect_requirement_str} \n\n" + \
             f"Available tools:\n{tools_str}\n" + \
             f"If none of the listed tools are suitable for a sub-task, respond with NEED_TOOL: <description of what you need> and I will search for additional tools.\n" + \
             f"Example for your reply: {constants.ToolSelect_reply_example2}\n"

    return prompt





def create_operation_prompt(task, data_path, selected_tools, documentation_str, workspace_directory):
    operation_requirement_str = '\n'.join(
        [f"{idx + 1}. {line}" for idx, line in enumerate(constants.operation_requirement)])
    data_path_str = '\n'.join([f"{idx + 1}. {line}" for idx, line in enumerate(data_path)])
    prompt = f"Your role: {constants.operation_role} \n" + \
             f"Your mission: {constants.operation_task_prefix}: " + f"{task}" + "Using the following data paths: " + f"{data_path_str}" + "\nAnd this output directory: " + f"{workspace_directory}\n\n" + \
             f"Using the following Selected tool(s): {selected_tools}\n" + \
             f"Documentation of the selected tools: \n{documentation_str}\n" + \
             f"requirements: \n{operation_requirement_str}\n" + \
             f"Set: " + f"{workspace_directory}" + " as the output directory for any operation"
    return prompt


def generate_operation_code(request_id, operation_prompt_str, model_name, stream, reasoning_effort):
    """Return a fine-tuned prompt using the selected model.
    Supports: OpenAI proxy, GPT-5, and normal OpenAI"""

    kwargs = {}
    # Only pass reasoning_effort for GPT-5 models
    if reasoning_effort and model_name in ['gpt-5', 'gpt-5.1', 'gpt-5.2']:
        kwargs['reasoning_effort'] = reasoning_effort
    return unified_llm_call(
        request_id = request_id,
        messages = [
        {"role": "user", "content": operation_prompt_str},
    ],
    model_name=model_name,
    stream=stream,
    **kwargs
    )



def code_review_prompt(extracted_code, data_path, selected_tool_dict, workspace_directory, documentation_str):
    operation_code_review_requirement_str = '\n'.join(
        [f"{idx + 1}. {line}" for idx, line in enumerate(constants.operation_code_review_requirement)])
    # print(f"Code passed to review: {extracted_code}")
    operation_code_review_prompt = f"Your role: {constants.operation_code_review_role} \n" + \
                                   f"Your mission: {constants.operation_code_review_task_prefix} \n\n" + \
                                   f"The code is: \n----------\n{extracted_code}\n----------\n\n" + \
                                   f"The properties of the data are given below:\n{data_path}\n" + \
                                   f"Using the following selected tool(s):{selected_tool_dict}\n " + \
                                   f"The code examples in the Documentation: \n{documentation_str} can be used as an example while reviewing the {extracted_code} \n\n" + \
                                   f"The requirements for the code is: \n{operation_code_review_requirement_str}\n\n" + \
                                   f"Output directory that should be used:{workspace_directory}"
    return operation_code_review_prompt



def code_review(request_id, code_review_prompt_str, model_name, stream, reasoning_effort=None):
    """Return a fine-tuned prompt using the selected model.
    Supports: OpenAI proxy, GPT-5, and normal OpenAI"""
    kwargs = {}
    # Only pass reasoning_effort for GPT-5 models
    if reasoning_effort and model_name in ['gpt-5', 'gpt-5.1', 'gpt-5.2']:
        kwargs['reasoning_effort'] = reasoning_effort
    return unified_llm_call(
        request_id=request_id,
        messages=[
            {"role": "user", "content": code_review_prompt_str},
        ],
        model_name=model_name,
        stream=stream,
        **kwargs
    )


# def get_code_for_operation(task_description, data_path, selected_tool, selected_tool_ID, documentation_str, review =True):
def get_code_for_operation(model_name, task_description, data_path, selected_tool, selected_tool_ID, selected_tool_dict, documentation_str,
                           review=True, stream=True, knowledge_text=""):
    """
    Generate operation code using unified LLM call.
    Supports: OpenAI proxy, GPT-5, Ollama, and normal OpenAI
    """
    operation_requirement_str = '\n'.join(
        [f"{idx + 1}. {line}" for idx, line in enumerate(constants.operation_requirement)])

    knowledge_section = ""
    if knowledge_text:
        knowledge_section = f"Project Knowledge:\n{knowledge_text}\n\n"

    user_prompt = f"Your mission: {constants.operation_task_prefix}: {task_description}\n\n" + \
                  f"Using the following data paths: {data_path}\n\n" + \
                  f"{knowledge_section}" + \
                  f"Selected tool: {selected_tool}\n" + \
                  f'{selected_tool_ID} Documentation: \n{documentation_str}\n' + \
                  f'Requirements: \n{operation_requirement_str}'

    messages = [
        {"role": "system", "content": constants.operation_role},
        {"role": "user", "content": user_prompt}
    ]

    response_str = unified_llm_call(
        messages=messages,
        model_name=model_name,
        stream=stream,
        temperature=1
    )

    extracted_code = extract_code_from_str(response_str)
    print(f"Extracted Operation Code: {extracted_code}")

    if review:
        operation_code = ask_LLM_to_review_operation_code(model_name, extracted_code, selected_tool_ID, selected_tool_dict, documentation_str)
        return operation_code
    else:
        return extracted_code


def ask_LLM_to_review_operation_code(model_name, extracted_code, selected_tool_ID, selected_tool_dict, documentation_str, stream=False):
    """
    Review operation code using unified LLM call.
    Supports: OpenAI proxy, GPT-5, Ollama, and normal OpenAI
    """
    operation_code_review_requirement_str = '\n'.join(
        [f"{idx + 1}. {line}" for idx, line in enumerate(constants.operation_code_review_requirement)])

    print(f"Code passed to review: {extracted_code}")

    user_prompt = f"Your task: {constants.operation_code_review_task_prefix} \n\n" + \
                  f"The code is: \n----------\n{extracted_code}\n----------\n\n" + \
                  f"The selected tool(s) is: {selected_tool_dict}\n" + \
                  f'{selected_tool_ID} Documentation: \n{documentation_str} \n\n' + \
                  f"The requirements for the code is: \n{operation_code_review_requirement_str}"

    print("LLM is reviewing the operation code... \n")

    messages = [
        {"role": "system", "content": constants.operation_code_review_role},
        {"role": "user", "content": user_prompt}
    ]

    response_str = unified_llm_call(
        messages=messages,
        model_name=model_name,
        stream=stream,
        temperature=1
    )

    # Extract code from the string response
    reviewed_code = extract_code_from_str(response_str)

    return reviewed_code


def convert_chunks_to_str(chunks):
    LLM_reply_str = ""
    for c in chunks:
        # print(c)

        cleaned_str = c.content.replace("```json", "").replace("```", "")
        LLM_reply_str += cleaned_str
        # # Append content, remove backticks, and strip leading/trailing whitespace

    return LLM_reply_str


def extract_dictionary_from_response(response):
    dict_pattern = r"\{.*?\}"
    match = re.search(dict_pattern, response)
    if match:
        dict_string = match.group()  # Extract the dictionary-like string
        return dict_string
    else:
        print("No dictionary found in the response.")
        return "{}"  # Return empty dict string as fallback


def convert_chunks_to_code_str(chunks):
    LLM_reply_str = ""
    for c in chunks:
        # Append content, remove backticks, and strip leading/trailing whitespace
        LLM_reply_str += c.content
    return LLM_reply_str


def fix_json_format(incorrect_json_str):
    # Fix common JSON issues such as missing double quotes around keys
    # Example: convert {selected tool: ["Clip","Scatterplot"]} to {"selected tool": ["Clip","Scatterplot"]}
    fixed_json_str = re.sub(r'(\w+):', r'"\1":', incorrect_json_str)
    return fixed_json_str


def parse_llm_reply(LLM_reply_str):
    try:
        # Try to load the string directly as JSON
        selection_operation = json.loads(LLM_reply_str)
    except json.JSONDecodeError:
        # If it fails, try to fix the JSON format and decode again
        corrected_reply = fix_json_format(LLM_reply_str)
        try:
            selection_operation = json.loads(corrected_reply)
        except json.JSONDecodeError as e:
            # If it still fails, return None or raise an error as per your needs
            print(f"Failed to parse LLM reply: {e}")
            selection_operation = None
    except TypeError as e:
        # Catch the case where input is not a string, bytes, or bytearray
        print(f"TypeError: {e} - Input must be a valid JSON string.")
        selection_operation = None
    return selection_operation


def get_LLM_reply(prompt="Provide Python code to read a CSV file from this URL and store the content in a variable. ",
                  system_role=r'You are a professional Geo-information scientist and developer.',
                  model_name=r"gpt-4o",
                  request_id="",
                  # model=r"gpt-3.5-turbo",
                  verbose=True,
                  temperature=1,
                  stream=True,
                  retry_cnt=3,
                  sleep_sec=10,
                  reasoning_effort="medium"  # Add reasoning_effort parameter for GPT-5
                  ):

    try:
        from SpatialAnalysisAgent_ModelProvider import create_unified_client
        client, provider = create_unified_client(model_name)
        use_unified_client = True
    except ImportError:
        # Fallback to basic OpenAI client
        client = create_openai_client()
        use_unified_client = False
    
    count = 0
    isSucceed = False
    while (not isSucceed) and (count < retry_cnt):
        try:
            count += 1
            if use_unified_client:
                # Generate response using the provider
                # Add reasoning_effort for GPT-5
                kwargs = {
                    'stream': stream,
                    'temperature': temperature
                }
                if model_name == 'gpt-5':
                    kwargs['reasoning_effort'] = reasoning_effort

                response = provider.generate_completion(
                    request_id,
                    client,
                    model_name,
                    [{"role": "system", "content": system_role},
                     {"role": "user", "content": prompt}],
                    **kwargs
                )
            else:
                response = client.chat.completions.create(model=model_name,
                                                          messages=[
                                                              {"role": "system", "content": system_role},
                                                              {"role": "user", "content": prompt},
                                                          ],
                                                          temperature=temperature,
                                                          stream=stream)
            isSucceed = True  # Mark as successful if we reach here
        except Exception as e:
            # logging.error(f"Error in get_LLM_reply(), will sleep {sleep_sec} seconds, then retry {count}/{retry_cnt}: \n", e)
            print(f"Error in get_LLM_reply(), will sleep {sleep_sec} seconds, then retry {count}/{retry_cnt}: \n", e)
            time.sleep(sleep_sec)

    response_chucks = []
    if stream:
        for chunk in response:
            response_chucks.append(chunk)
            # Handle different response formats based on provider
            content = None
            if use_unified_client and hasattr(chunk, 'type'):
                # Handle GPT-5 ResponseCreatedEvent format
                if hasattr(chunk, 'response') and hasattr(chunk.response, 'body') and hasattr(chunk.response.body, 'choices'):
                    if chunk.response.body.choices and hasattr(chunk.response.body.choices[0], 'delta'):
                        content = chunk.response.body.choices[0].delta.content
                elif hasattr(chunk, 'delta') and hasattr(chunk.delta, 'content'):
                    content = chunk.delta.content
                # Try alternative GPT-5 streaming format
                elif hasattr(chunk, 'content'):
                    content = chunk.content
            else:
                # Handle standard OpenAI format
                if hasattr(chunk, 'choices') and chunk.choices:
                    if hasattr(chunk.choices[0], 'delta') and hasattr(chunk.choices[0].delta, 'content'):
                        content = chunk.choices[0].delta.content

            if content is not None:
                if verbose:
                    print(content, end='')
    else:
        # Handle non-streaming response
        if use_unified_client:
            # Handle different non-streaming formats for unified client
            if hasattr(response, 'choices') and response.choices:
                content = response.choices[0].message.content
            elif hasattr(response, 'response') and hasattr(response.response, 'body'):
                if hasattr(response.response.body, 'choices') and response.response.body.choices:
                    content = response.response.body.choices[0].message.content
                elif hasattr(response.response.body, 'content'):
                    content = response.response.body.content
            elif hasattr(response, 'content'):
                content = response.content
        else:
            content = response.choices[0].message.content
        # print(content)
    print('\n\n')
    # print("Got LLM reply.")

    response = response_chucks  # good for saving

    return response


def extract_content_from_LLM_reply(response):
    stream = False
    if isinstance(response, list):
        stream = True

    content = ""
    if stream:
        for chunk in response:
            # Handle different response formats based on chunk type
            chunk_content = None

            # Check for GPT-5 ResponseCreatedEvent format
            if hasattr(chunk, 'type'):
                # Handle GPT-5 ResponseCreatedEvent format
                if hasattr(chunk, 'response') and hasattr(chunk.response, 'body') and hasattr(chunk.response.body, 'choices'):
                    if chunk.response.body.choices and hasattr(chunk.response.body.choices[0], 'delta'):
                        chunk_content = chunk.response.body.choices[0].delta.content
                elif hasattr(chunk, 'delta') and hasattr(chunk.delta, 'content'):
                    chunk_content = chunk.delta.content
                # Try alternative GPT-5 streaming format
                elif hasattr(chunk, 'content'):
                    chunk_content = chunk.content
            else:
                # Handle standard OpenAI format
                if hasattr(chunk, 'choices') and chunk.choices:
                    if hasattr(chunk.choices[0], 'delta') and hasattr(chunk.choices[0].delta, 'content'):
                        chunk_content = chunk.choices[0].delta.content

            if chunk_content is not None:
                # print(chunk_content, end='')
                content += chunk_content
                # print(content)
        # print()
    else:
        # Handle non-streaming response
        if hasattr(response, 'choices') and response.choices:
            content = response.choices[0].message.content
        elif hasattr(response, 'response') and hasattr(response.response, 'body'):
            if hasattr(response.response.body, 'choices') and response.response.body.choices:
                content = response.response.body.choices[0].message.content
            elif hasattr(response.response.body, 'content'):
                content = response.response.body.content
        elif hasattr(response, 'content'):
            content = response.content
        # print(content)

    return content



#Fetching the streamed response of LLM
async def fetch_chunks(model, prompt_str):
    # print(f"\n[DEBUG] Model being used inside fetch_chunks: {model.model_name if hasattr(model, 'model_name') else model}\n")
    chunks = []
    async for chunk in model.astream(prompt_str):
        chunks.append(chunk)
        # print(chunk.content, end="", flush=True)
    return chunks


# nest_asyncio.apply()


def extract_selected_tools(chunks):
    """
    Extracts and combines selected tools from a list of chunk dictionaries.

    :param chunks: List of dictionaries, each containing a "Selected tools" key.
    :return: A string of combined selected tools separated by commas.
    """
    all_tools = []

    for chunk in chunks:
        # Ensure the key exists and its value is a list
        tools = chunk.get("Selected tools", [])
        if isinstance(tools, list):
            all_tools.extend(tools)
        else:
            print(f"Warning: 'Selected tools' is not a list in chunk: {chunk}")

    # Optional: Remove duplicates while preserving order
    seen = set()
    unique_tools = []
    for tool in all_tools:
        if tool not in seen:
            seen.add(tool)
            unique_tools.append(tool)

    # Combine the tools into a single string separated by commas
    combined_tools_str = ', '.join(unique_tools)

    return combined_tools_str


def extract_code(response, verbose=False):
    '''
    Extract python code from reply
    '''
    # if isinstance(response, list):  # use OpenAI stream mode.
    #     reply_content = ""
    #     for chunk in response:
    #         chunk_content = chunk["choices"][0].get("delta", {}).get("content")
    #
    #         if chunk_content is not None:
    #             print(chunk_content, end='')
    #             reply_content += chunk_content
    #             # print(content)
    # else:  # Not stream:
    #     reply_content = response["choices"][0]['message']["content"]

    python_code = ""
    reply_content = extract_content_from_LLM_reply(response)
    python_code_match = re.search(r"```(?:python)?(.*?)```", reply_content, re.DOTALL)
    if python_code_match:
        python_code = python_code_match.group(1).strip()

    if verbose:
        print(python_code)

    return python_code


def extract_code_from_str(LLM_reply_str, verbose=False):
    '''
    Extract python code from reply string, not 'response'.
    '''

    python_code = ""
    python_code_match = re.search(r"```(?:python)?(.*?)```", LLM_reply_str, re.DOTALL)
    if python_code_match:
        python_code = python_code_match.group(1).strip()

    if verbose:
        print(python_code)

    return python_code


# =====================================================================
# Preflight validator
# Static AST scan run before exec() to surface known coding mistakes as
# RuntimeError("ERROR_CODE_XXX: ...") so the auto-debug loop below gets a
# precise correction signal instead of letting QGIS abort silently or with
# a vague "Incorrect parameter value" message.
# Coverage maps to case_success_review_001_086.md FAIL cases.
# =====================================================================

# Cache: alg_id -> {'required': [...], 'optional': [...], 'all': set(...)} or None
_TOOL_PARAM_CACHE = {}
_RUNTIME_SIG_CACHE = {}  # 运行时签名缓存,key=alg_id,value=dict 或 None

# Hard-coded parameter aliases for known LLM mix-ups. Used in the prompt
# whitelist block to call out the most common confusions explicitly.
_TOOL_PARAM_CONFUSIONS = {
    "native:lineintersections":
        "USES `INTERSECT` (NOT `OVERLAY`). Confusing siblings: "
        "native:intersection / native:union / native:difference all use OVERLAY.",
    "qgis:lineintersections":
        "USES `INTERSECT` (NOT `OVERLAY`).",
    "native:difference":
        "USES `INPUT` + `OVERLAY`. NOT `INTERSECT`.",
    "native:intersection":
        "USES `INPUT` + `OVERLAY`. NOT `INTERSECT`.",
    "native:union":
        "USES `INPUT` + `OVERLAY`. NOT `INTERSECT`.",
    "native:symmetricaldifference":
        "Use ONLY when task literally says 'symmetric(al) difference'; "
        "otherwise use native:difference.",
    "gdal:viewshed":
        "INPUT must be a QgsRasterLayer object (the DEM). "
        "OBSERVER must be a QgsVectorLayer object (point layer). "
        "DO NOT pass raw paths or coordinate strings.",
    "qgis:basicstatisticsforfields":
        "USES `INPUT_LAYER` (NOT `INPUT`).",
    "native:basicstatisticsforfields":
        "USES `INPUT_LAYER` (NOT `INPUT`).",
    "native:rastercalc":
        "USES `LAYERS` (a Python list of QgsRasterLayer objects), `EXPRESSION` "
        "(string referencing layers by NAME@BAND e.g. '\"DEM@1\" > 100'), and "
        "`OUTPUT`. Optional: EXTENT, CELL_SIZE, CRS, CREATION_OPTIONS. "
        "Do NOT use `INPUT` — that key does NOT exist on this algorithm.",
    "gdal:rastercalculator":
        "Supports multi-input via INPUT_A/BAND_A through INPUT_F/BAND_F, with "
        "FORMULA referring to inputs as 'A', 'B', ..., 'F'. Required: "
        "INPUT_A, BAND_A, FORMULA, EXTENT_OPT, RTYPE, OUTPUT. Optional: "
        "INPUT_B..F, BAND_B..F, NO_DATA, PROJWIN, OPTIONS, CREATION_OPTIONS, EXTRA.",
}


def _find_tool_toml_path(alg_id):
    """Locate the TOML file for an algorithm ID. Tries the alg_id as-is,
    then swaps `native:`<->`qgis:` (QGIS migrated many algorithms between
    those namespaces but keeps both runtime-compatible).
    """
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    docs_dir = os.path.join(current_script_dir, "Tools_Documentation")
    if not os.path.isdir(docs_dir):
        return None

    candidates = [alg_id]
    if alg_id.startswith("native:"):
        candidates.append("qgis:" + alg_id[len("native:"):])
    elif alg_id.startswith("qgis:"):
        candidates.append("native:" + alg_id[len("qgis:"):])

    for cand in candidates:
        stfid = re.sub(r"[ :?\/]", "_", cand)
        target = f"{stfid}.toml"
        for root, _dirs, files in os.walk(docs_dir):
            if target in files:
                return os.path.join(root, target)
    return None


def _parse_param_lines(parameters_text):
    """Parse the body of a tool TOML's `parameters` triple-quoted string.

    Each parameter is one (or more) line(s) of the form
        PARAM_NAME: <description>. Type: [...] Default: <default>
    A parameter is treated as optional if its description contains
    'Optional' (case-sensitive) or a 'Default:' marker.
    """
    if not parameters_text:
        return {"required": [], "optional": [], "all": set()}
    required, optional = [], []
    cur_name = None
    cur_desc_parts = []

    def flush():
        if cur_name is None:
            return
        joined = " ".join(cur_desc_parts)
        if "Optional" in joined or "Default:" in joined:
            optional.append(cur_name)
        else:
            required.append(cur_name)

    for line in parameters_text.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]+)\s*:\s*(.*)$", line)
        if m:
            flush()
            cur_name = m.group(1)
            cur_desc_parts = [m.group(2)]
        else:
            cur_desc_parts.append(line)
    flush()
    return {
        "required": required,
        "optional": optional,
        "all": set(required) | set(optional),
    }


def get_tool_param_whitelist(alg_id):
    """Return parsed parameter whitelist for `alg_id` or None if no TOML
    can be found / parsed. Result is cached per process.
    """
    if alg_id in _TOOL_PARAM_CACHE:
        return _TOOL_PARAM_CACHE[alg_id]

    path = _find_tool_toml_path(alg_id)
    if path is None:
        _TOOL_PARAM_CACHE[alg_id] = None
        return None

    try:
        import tomli as tomllib
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        _TOOL_PARAM_CACHE[alg_id] = None
        return None

    params_text = data.get("parameters", "")
    parsed = _parse_param_lines(params_text)
    if not parsed["all"]:
        _TOOL_PARAM_CACHE[alg_id] = None
        return None

    _TOOL_PARAM_CACHE[alg_id] = parsed
    return parsed


def get_runtime_signature(alg_id):
    """直接问当前 QGIS:这个算法实际接受什么参数?

    这是绕过 toml/json 文档过期问题的"事实之源"。返回 dict:
        {
            "tool_id": str,
            "display_name": str,
            "required": [{"name", "type", "description"}, ...],
            "optional": [{"name", "type", "description", "default"}, ...],
            "outputs":  [{"name", "type", "description"}, ...],
        }
    或 None(算法没注册 / QGIS API 不可用)。结果按进程缓存。
    """
    if alg_id in _RUNTIME_SIG_CACHE:
        return _RUNTIME_SIG_CACHE[alg_id]

    try:
        from qgis.core import QgsApplication
    except ImportError:
        # 非 QGIS 环境(如单元测试) → 直接放弃
        _RUNTIME_SIG_CACHE[alg_id] = None
        return None

    try:
        reg = QgsApplication.processingRegistry()
        alg = reg.algorithmById(alg_id)
        if alg is None:
            _RUNTIME_SIG_CACHE[alg_id] = None
            return None

        required, optional, outputs = [], [], []
        for p in alg.parameterDefinitions():
            cls_name = type(p).__name__
            is_output = "Destination" in cls_name
            try:
                is_optional = bool(p.flags() & p.FlagOptional)
            except Exception:
                is_optional = False

            entry = {
                "name": p.name(),
                "type": p.type(),
                "description": p.description(),
            }
            try:
                entry["default"] = p.defaultValue()
            except Exception:
                pass

            if is_output:
                outputs.append(entry)
            elif is_optional:
                optional.append(entry)
            else:
                required.append(entry)

        sig = {
            "tool_id": alg_id,
            "display_name": alg.displayName(),
            "required": required,
            "optional": optional,
            "outputs": outputs,
        }
        _RUNTIME_SIG_CACHE[alg_id] = sig
        return sig
    except Exception as e:
        print(f"[get_runtime_signature] failed for {alg_id}: {e}")
        _RUNTIME_SIG_CACHE[alg_id] = None
        return None


def build_runtime_signature_block(alg_ids):
    """给一组工具 ID 生成"运行时真实签名"的 prompt 块。

    这个块的优先级高于 toml-derived 的 whitelist 块 —— toml 可能写错
    或滞后于 QGIS 版本,而 QGIS 自己 reportd 的签名永远是当前真相。
    """
    if not alg_ids:
        return ""

    sections = []
    missing = []
    for tid in alg_ids:
        sig = get_runtime_signature(tid)
        if sig is None:
            missing.append(tid)
            continue
        req_names = [p["name"] for p in sig["required"]]
        opt_names = [p["name"] for p in sig["optional"]]
        out_names = [p["name"] for p in sig["outputs"]]
        # 把 OUTPUT 类参数合并进必填(用户必须给输出路径)
        full_required = req_names + out_names
        all_allowed = full_required + opt_names

        lines = [f"  {tid}  ({sig['display_name']}):"]
        lines.append(f"    ALLOWED keys (the ONLY keys accepted by this QGIS build): {all_allowed}")
        lines.append(f"    REQUIRED: {full_required}")
        if opt_names:
            lines.append(f"    OPTIONAL: {opt_names}")
        sections.append("\n".join(lines))

    if not sections:
        return ""

    header = (
        "================================================================\n"
        "RUNTIME-VERIFIED PARAMETER SIGNATURES — ABSOLUTE TRUTH\n"
        "================================================================\n"
        "The parameter lists below were just queried LIVE from your current\n"
        "QGIS install (via QgsApplication.processingRegistry). Any key NOT\n"
        "in ALLOWED for a given algorithm will be rejected with:\n"
        "  ERROR_CODE_PARAM_UNKNOWN: <alg> does not accept parameter(s) [...]\n"
        "Ignore conflicting examples in your training data or in the static\n"
        "documentation that follows — these runtime signatures override them.\n"
        "================================================================\n"
    )
    footer = "\n================================================================\n"
    body = header + "\n".join(sections) + footer
    if missing:
        body += f"(Could not resolve runtime signature for: {missing} — falling back to static docs for those.)\n"
    return body


def build_parameter_whitelist_block(alg_ids):
    """Build a strict-parameter-names prompt block for the given algorithms.
    Returns "" when no TOML data is available for any of them, so callers
    can simply concatenate the result.
    """
    if not alg_ids:
        return ""
    sections = []
    for tid in alg_ids:
        wl = get_tool_param_whitelist(tid)
        if not wl:
            continue
        block = [f"  {tid}:"]
        if wl["required"]:
            block.append(f"    REQUIRED keys: {wl['required']}")
        if wl["optional"]:
            block.append(f"    OPTIONAL keys: {wl['optional']}")
        if tid in _TOOL_PARAM_CONFUSIONS:
            block.append(f"    NOTE: {_TOOL_PARAM_CONFUSIONS[tid]}")
        sections.append("\n".join(block))
    if not sections:
        return ""
    header = (
        "============================================================\n"
        "STRICT PARAMETER NAMES — HARD CONSTRAINT\n"
        "============================================================\n"
        "For each algorithm below, you MUST use ONLY the listed parameter\n"
        "keys when calling processing.run(...). Using any other key triggers:\n"
        "  'Could not load source layer for <KEY>: no value specified for parameter'\n"
        "and the run aborts.\n"
    )
    footer = "\n============================================================\n"
    return header + "\n".join(sections) + footer


# Path-like file extensions used by preflight to decide whether a string
# parameter value should be checked for existence on disk.
_PATHY_EXTENSIONS = (
    ".shp", ".gpkg", ".geojson", ".kml", ".gml", ".tif", ".tiff",
    ".jpg", ".jpeg", ".png", ".asc", ".csv", ".xlsx", ".xls", ".vrt",
    ".dem", ".sdat", ".nc", ".hdf", ".dbf",
)

# Output-style parameter keys whose value is *expected* not to exist yet
# (preflight should NOT path-check them).
_OUTPUT_PARAM_KEYS = {
    "OUTPUT", "OUTPUT_LAYER", "OUTPUT_FILE", "OUTPUT_DIR", "OUTPUT_DIRECTORY",
    "OUTPUT_HTML_FILE", "OUTPUT_RASTER", "OUTPUT_VECTOR",
    "TARGET_FILE", "TARGET", "REPORT", "HTML_FILE",
}

_PREFLIGHT_BAD_ALGORITHM_IDS = {
    # Case 023: agent invented this ID; the real save algorithm is savefeatures.
    "native:savevectorlayer": "native:savefeatures",
    # Common LLM slip elsewhere in the codebase.
    "native:rastercalculator": "native:rastercalc",
    # Case 067: legacy ID; QGIS 3.40 algorithm is native:addxyfields.
    "native:addxyfieldstolayer": "native:addxyfields",
    # Case 079: legacy ID; QGIS 3.40 algorithm is native:rastersampling.
    "native:samplerastervalues": "native:rastersampling",
    # Case 060: no native copy-raster algorithm in QGIS 3.40; gdal:translate is
    # the canonical "copy raster (optionally setting NoData)" path.
    "native:copyraster": "gdal:translate",
    # Case 075: LLM snake-cased the display name "Rasterize (overwrite with
    # attribute)"; the real ID is gdal:rasterize_over.
    "gdal:rasterize_overwrite_with_attribute": "gdal:rasterize_over",
    # Cluster D: native:executesql does NOT exist; the working IDs in QGIS 3.40
    # are qgis:executesql / gdal:executesql / native:postgisexecutesql.
    "native:executesql": "qgis:executesql",
    # Case 075: gdal:grid / gdal:grididw don't exist. The real IDs are spelled
    # out: gdal:gridinversedistance(nearestneighbor), gdal:gridnearestneighbor,
    # gdal:gridaverage, gdal:gridlinear, gdal:griddatametrics. We map the most
    # common LLM hallucinations to the closest correct algorithm.
    "gdal:grididw": "gdal:gridinversedistance",
    "gdal:gridinversedistanceweighted": "gdal:gridinversedistance",
    "gdal:grididw": "gdal:gridinversedistance",
    "gdal:grid": "gdal:gridnearestneighbor",
    "gdal:gridnearest": "gdal:gridnearestneighbor",
    # Cluster C: this Qt6 LTR build registers GRASS algorithms under the
    # `grass:` prefix only — `grass7:` is gone. Generic `grass7:*` -> `grass:*`
    # rewriting is also handled in _preflight_check_call below; these explicit
    # mappings cover the IDs we have seen the LLM emit most often.
    "grass7:r.composite": "grass:r.composite",
    "grass7:r.neighbors": "grass:r.neighbors",
    "grass7:r.viewshed": "grass:r.viewshed",
    "grass7:r.fill.dir": "grass:r.fill.dir",
    "grass7:r.watershed": "grass:r.watershed",
    "grass7:r.water.outlet": "grass:r.water.outlet",
    "grass7:r.stream.extract": "grass:r.stream.extract",
    "grass7:v.surf.idw": "grass:v.surf.idw",
}

# Hard cap on grid / regular-points feature count (Case 049/053).
_PREFLIGHT_SCALE_HARD_CAP = 5_000_000

# Set by _preflight_validate so the per-call checker can inspect the raw
# source for inline guard patterns (e.g. an `assert est_cells < N` that the
# LLM emitted in response to the prompt rule).
_active_code_text = ""


def _preflight_code_has_grid_size_check(code):
    """True if the source text contains a recognizable cell-count guard.

    We accept any of:
      - `assert est_cells <= ...` / `assert ... < 5_000_000`
      - a literal mention of the constant `5_000_000` near `cells` or `est`
      - a `raise ...` inside an `if cells > ...` branch

    This is intentionally lax: we just want evidence that the LLM thought
    about it. False positives are fine; false negatives are what crash QGIS.
    """
    if not isinstance(code, str) or not code:
        return False
    lc = code.lower()
    if "5_000_000" in lc or "5000000" in lc:
        return True
    if "est_cells" in lc:
        return True
    return bool(re.search(r"assert\s+\w*cells?\w*", lc))


def _preflight_estimate_grid_cells_from_data(data_path, hspacing, vspacing):
    """Best-effort: read an input .shp/.gpkg/.tif from `data_path` via OGR/GDAL,
    estimate how big the grid would be in METERS, and divide by spacing.

    Returns int(estimated_cells) or None if we can't tell.

    Why this matters: case 049's input was a 0.79° × 0.82° polygon in EPSG:4269
    (lat/lon). Reprojected to UTM 17N that's ~65 km × 91 km. The LLM picked
    HSPACING=VSPACING=2.0 (meters) and crashed QGIS with ~1.48 billion cells.
    Statically, we can detect the dataset is geographic, convert to a rough
    metric extent (1° ≈ 111 km), and refuse the call.
    """
    if not data_path:
        return None
    try:
        hs = float(hspacing)
        vs = float(vspacing)
    except (TypeError, ValueError):
        return None
    if hs <= 0 or vs <= 0:
        return None
    try:
        from osgeo import ogr, gdal, osr
    except Exception:
        return None
    # data_path may be multi-line manifest text. Pull out a likely path.
    candidates = []
    for line in str(data_path).splitlines():
        s = line.strip().strip("'\"")
        if not s:
            continue
        m = re.search(
            r"([A-Za-z]:[\\/][^,\s'\"]+\.(?:shp|gpkg|geojson|kml|gml|tif|tiff))",
            s, re.IGNORECASE,
        )
        if m and os.path.exists(m.group(1)):
            candidates.append(m.group(1))
    seen = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]
    if not candidates:
        return None
    best = None
    for path in candidates:
        try:
            ext = path.lower().rsplit(".", 1)[-1]
            if ext in ("tif", "tiff"):
                ds = gdal.Open(path)
                if ds is None:
                    continue
                gt = ds.GetGeoTransform()
                if not gt:
                    continue
                xs = ds.RasterXSize
                ys = ds.RasterYSize
                xmin = gt[0]
                ymax = gt[3]
                xmax = xmin + gt[1] * xs
                ymin = ymax + gt[5] * ys
                wkt = ds.GetProjection() or ""
            else:
                ds = ogr.Open(path)
                if ds is None:
                    continue
                lyr = ds.GetLayer()
                if lyr is None:
                    continue
                xmin, xmax, ymin, ymax = lyr.GetExtent()
                sref = lyr.GetSpatialRef()
                wkt = sref.ExportToWkt() if sref else ""
            width = abs(xmax - xmin)
            height = abs(ymax - ymin)
            # If CRS looks geographic (degrees), inflate to approximate meters
            # using 1° latitude ≈ 111 km, 1° longitude ≈ 111 km × cos(lat).
            is_geo = False
            try:
                if wkt:
                    sr = osr.SpatialReference()
                    sr.ImportFromWkt(wkt)
                    is_geo = bool(sr.IsGeographic())
            except Exception:
                pass
            if is_geo:
                lat_mid = (ymin + ymax) / 2.0
                import math
                width_m = width * 111_000 * max(math.cos(math.radians(lat_mid)), 0.1)
                height_m = height * 111_000
            else:
                width_m, height_m = width, height
            cells = (width_m / hs) * (height_m / vs)
            if best is None or cells > best:
                best = cells
        except Exception:
            continue
    if best is None:
        return None
    return int(best)



class _PreflightUnresolved:
    """Sentinel for AST values we cannot statically evaluate."""

    def __repr__(self):
        return "<unresolved>"


_PREFLIGHT_UNRESOLVED = _PreflightUnresolved()


def _preflight_literal(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return _PREFLIGHT_UNRESOLVED


def _preflight_dict(node):
    if not isinstance(node, ast.Dict):
        return {}
    out = {}
    for k_node, v_node in zip(node.keys, node.values):
        if k_node is None:
            continue
        key = _preflight_literal(k_node)
        if not isinstance(key, str):
            continue
        out[key] = _preflight_literal(v_node)
    return out


def _preflight_collect_dict_assignments(tree):
    """Scan top-level / function-level `var = {...}` assignments so we can
    resolve `processing.run('alg', params_var)` calls statically.

    LLM-generated code overwhelmingly uses the pattern:
        params_viewshed = {'INPUT': dem_layer, 'OBSERVER': pts, ...}
        processing.run('gdal:viewshed', params_viewshed)
    Without this resolver the per-call validator sees an empty dict and
    fires ERROR_CODE_PARAM_MISSING for every required key — exactly the
    case 028 / case 029 false-positive that kept the LLM in a hot loop.

    Returns: dict {var_name: dict-of-resolved-keys-and-values}
    Variables reassigned multiple times keep the LAST value (closest to
    the processing.run call in linear order, which matches how Python
    actually executes).
    """
    var_map = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        resolved = _preflight_dict(node.value)
        for tgt in node.targets:
            if isinstance(tgt, ast.Name):
                var_map[tgt.id] = resolved
    return var_map


def _preflight_resolve_params(arg_node, dict_var_map):
    """Best-effort: resolve the SECOND positional arg to processing.run
    into a dict of params. Returns either:
      - a dict (literal or resolved variable) -> validator can do hard checks
      - None                                  -> opaque, skip required-key checks
    """
    if isinstance(arg_node, ast.Dict):
        return _preflight_dict(arg_node)
    if isinstance(arg_node, ast.Name) and arg_node.id in dict_var_map:
        return dict_var_map[arg_node.id]
    return None


def _preflight_check_call(alg, params, task, data_path="", params_opaque=False):
    """Return [(error_code, message, suggestion), ...]; empty list = OK.

    If `params_opaque` is True, the caller could not statically resolve the
    params dict (e.g. it came from a function call or runtime computation).
    Required-key checks are skipped in that case so we don't false-positive
    on perfectly valid code that uses a `params_X = {...}` indirection.
    """
    issues = []

    if alg in _PREFLIGHT_BAD_ALGORITHM_IDS:
        issues.append((
            "ERROR_CODE_ALG_NOT_FOUND",
            f"Algorithm ID '{alg}' does not exist in QGIS 3.x.",
            f"Use '{_PREFLIGHT_BAD_ALGORITHM_IDS[alg]}' instead.",
        ))
        return issues  # don't bother validating params on a non-existent alg

    if alg in {"gdal:rasterize_over", "gdal:rasterize_over_fixed_value"} and not params_opaque:
        field = params.get("FIELD", _PREFLIGHT_UNRESOLVED)
        if field is None or field == "":
            issues.append((
                "ERROR_CODE_PARAM_VALUE",
                f"`{alg}` FIELD is empty/None; rasterize_over needs a real attribute name.",
                "Switch to gdal:rasterize with a BURN value if no source field exists.",
            ))

    if alg == "gdal:viewshed" and not params_opaque:
        for required in ("INPUT", "OBSERVER"):
            if required not in params:
                issues.append((
                    "ERROR_CODE_PARAM_MISSING",
                    f"`gdal:viewshed` requires {required}.",
                    f"Set {required} to a loaded layer object, not a placeholder.",
                ))
                continue
            val = params[required]
            if val is None or val == "":
                issues.append((
                    "ERROR_CODE_PARAM_VALUE",
                    f"`gdal:viewshed` {required} is empty/None.",
                    f"Set {required} to a loaded layer object, not a placeholder.",
                ))

    if alg in {"native:creategrid", "qgis:regularpoints"}:
        spacings = []
        for k in ("HSPACING", "VSPACING", "SPACING"):
            v = params.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                spacings.append(float(v))
        if spacings and min(spacings) < 0.5:
            issues.append((
                "ERROR_CODE_SCALE_EXCEEDED",
                f"`{alg}` spacing {min(spacings)} is sub-meter; would generate billions of features (Case 053).",
                "Use spacing >= 1m unless the user explicitly requested finer; otherwise ask for clarification.",
            ))
        elif spacings:
            extent = params.get("EXTENT")
            estimated_from_literal = False
            if isinstance(extent, str):
                m = re.match(
                    r"\s*([\-0-9.eE+]+)\s*,\s*([\-0-9.eE+]+)\s*,\s*([\-0-9.eE+]+)\s*,\s*([\-0-9.eE+]+)",
                    extent,
                )
                if m:
                    estimated_from_literal = True
                    try:
                        xmin, xmax, ymin, ymax = (float(g) for g in m.groups())
                        hs = params.get("HSPACING") or params.get("SPACING") or spacings[0]
                        vs = params.get("VSPACING") or params.get("SPACING") or spacings[0]
                        est = (abs(xmax - xmin) / float(hs)) * (abs(ymax - ymin) / float(vs))
                        if est > _PREFLIGHT_SCALE_HARD_CAP:
                            issues.append((
                                "ERROR_CODE_SCALE_EXCEEDED",
                                f"`{alg}` would generate ~{int(est):,} cells (cap: {_PREFLIGHT_SCALE_HARD_CAP:,}).",
                                "Increase spacing, restrict extent, or write to GeoPackage instead of shapefile.",
                            ))
                    except (TypeError, ValueError):
                        pass

            # Case 049 / 081 family: EXTENT was computed at runtime
            # (`layer.extent()`, f-string with .xMinimum() etc.) so the static
            # check above never fired and the LLM walked into a billion-cell
            # OOM crash. Two-step defence:
            #   1. Try to estimate from an input .shp on disk (the runner
            #      provides `data_path`), which lets us refuse the call when
            #      the data is hugely larger than the spacing implies.
            #   2. If we still can't tell, REQUIRE the generated code itself
            #      to include an inline cell-count assertion — refuse otherwise.
            if not estimated_from_literal:
                hs = params.get("HSPACING") or params.get("SPACING") or spacings[0]
                vs = params.get("VSPACING") or params.get("SPACING") or spacings[0]
                static_est = _preflight_estimate_grid_cells_from_data(data_path, hs, vs)
                if static_est is not None and static_est > _PREFLIGHT_SCALE_HARD_CAP:
                    issues.append((
                        "ERROR_CODE_SCALE_EXCEEDED",
                        f"`{alg}` with HSPACING={hs}, VSPACING={vs} on the "
                        f"provided input layer would generate "
                        f"~{int(static_est):,} cells once reprojected to a "
                        f"metric CRS (cap: {_PREFLIGHT_SCALE_HARD_CAP:,}). "
                        f"Calling processing.run on this would SIGABRT the "
                        f"QGIS subprocess.",
                        "Increase HSPACING/VSPACING (e.g. 50–500 m for a "
                        "county-sized polygon) until cells <= 5_000_000, or "
                        "restrict EXTENT to a sub-region the user actually "
                        "wants. Compute the estimate explicitly in code.",
                    ))
                elif static_est is None:
                    # Fall back to demanding an inline assertion in the source.
                    # We accept any of: assert est_cells < N, raise on cap,
                    # an `if ... > 5_000_000` branch.
                    if not _preflight_code_has_grid_size_check(_active_code_text):
                        issues.append((
                            "ERROR_CODE_GRID_UNCHECKED",
                            f"`{alg}` is called with a runtime-computed EXTENT "
                            f"and spacing ({hs}, {vs}); the static validator "
                            f"cannot prove the cell count is bounded.",
                            "Insert BEFORE the processing.run call: "
                            "`width = abs(extent.xMaximum() - extent.xMinimum()); "
                            "height = abs(extent.yMaximum() - extent.yMinimum()); "
                            f"est_cells = (width/{hs})*(height/{vs}); "
                            f"assert est_cells <= 5_000_000, "
                            f"f'grid too large: {{est_cells:.0f}} cells'`. "
                            "If the assertion would fire, raise HSPACING/VSPACING.",
                        ))

    if alg == "native:symmetricaldifference" and task:
        t = task.lower()
        if " difference" in t and "symmetric" not in t and "symmetrical" not in t:
            issues.append((
                "ERROR_CODE_ALG_MISMATCH",
                "Task asks for 'difference' but code uses native:symmetricaldifference.",
                "Use native:difference unless the task literally says 'symmetric(al) difference'.",
            ))

    # Generic parameter-name whitelist check: catches OVERLAY-vs-INTERSECT
    # style hallucinations on any tool whose TOML doc we can parse.
    issues.extend(_preflight_check_param_keys(alg, params))

    # Generic path-existence check: filename strings passed to INPUT-style
    # parameters that don't exist on disk surface as a concrete error
    # instead of QGIS's vague "Could not load source layer" later.
    issues.extend(_preflight_check_param_paths(alg, params))

    return issues


def _preflight_check_param_keys(alg, params):
    """Reject parameter keys not declared by the algorithm.

    优先级:
      1. 运行时签名 (get_runtime_signature) —— 真相之源,直接问 QGIS
      2. TOML whitelist —— fallback,可能滞后/不完整

    历史教训: 旧版只用 TOML whitelist 会"伪造"假错误 —— 比如 native:rastercalc
    的 TOML parameters 段只列了 INPUT/EXPRESSION/OUTPUT,而 QGIS 真正支持的是
    LAYERS/EXPRESSION/EXTENT/CELL_SIZE/CRS/CREATION_OPTIONS/OUTPUT。AI 照
    code_example 用 LAYERS,就被 preflight 误判成 ERROR_CODE_PARAM_UNKNOWN。
    """
    if not params:
        return []

    # 1) 优先用运行时签名 —— 直接问 QGIS,永远是当前 build 的真相
    sig = get_runtime_signature(alg)
    allowed_set = None
    source = None
    if sig is not None:
        allowed = set()
        for entry in sig.get("required", []):
            allowed.add(entry["name"])
        for entry in sig.get("optional", []):
            allowed.add(entry["name"])
        for entry in sig.get("outputs", []):
            allowed.add(entry["name"])
        if allowed:
            allowed_set = allowed
            source = "runtime"

    # 2) Fallback: 老的 TOML whitelist
    if allowed_set is None:
        wl = get_tool_param_whitelist(alg)
        if not wl or not wl.get("all"):
            return []
        allowed_set = wl["all"]
        source = "toml"

    used = {k for k in params.keys() if isinstance(k, str)}
    invalid = sorted(used - allowed_set)
    if not invalid:
        return []

    valid_sorted = sorted(allowed_set)
    suggestion_parts = [
        f"Use only these keys (source={source}): {valid_sorted}."
    ]
    if alg in _TOOL_PARAM_CONFUSIONS:
        suggestion_parts.append(f"Hint: {_TOOL_PARAM_CONFUSIONS[alg]}")
    suggestion = " ".join(suggestion_parts).strip()

    return [(
        "ERROR_CODE_PARAM_UNKNOWN",
        f"`{alg}` does not accept parameter(s) {invalid}.",
        suggestion,
    )]


def _preflight_check_param_paths(alg, params):
    """If a parameter value is a string ending in a known data extension
    and is NOT an output parameter, verify the path exists on disk.
    """
    issues = []
    for key, val in params.items():
        if not isinstance(key, str) or key in _OUTPUT_PARAM_KEYS:
            continue
        if not isinstance(val, str) or len(val) < 4:
            continue
        low = val.lower()
        if not low.endswith(_PATHY_EXTENSIONS):
            continue
        # Only treat values that look path-like (drive letter / separator)
        # as paths; bare basenames are usually layer-display names.
        if not (re.match(r"^[A-Za-z]:[\\/]", val) or val.startswith(("/", "\\"))
                or "/" in val or "\\" in val):
            continue
        if os.path.exists(val):
            continue
        issues.append((
            "ERROR_CODE_PATH_NOT_FOUND",
            f"`{alg}` parameter `{key}` points at a file that does not exist: {val}",
            "Use the absolute path emitted by the runner (see Data path / "
            "loaded_layers in the system message); do not hardcode or guess "
            "subdirectories.",
        ))
    return issues


def _preflight_check_multi_input(code, data_path):
    """Case 048: task supplies N input files but only one is referenced."""
    if not data_path:
        return []
    candidates = []
    for line in data_path.splitlines():
        m = re.search(
            r"([A-Za-z0-9_\-.]+\.(shp|gpkg|geojson|tif|tiff|csv|kml))",
            line.strip(),
            re.IGNORECASE,
        )
        if m:
            candidates.append(m.group(1))
    if len(candidates) < 2:
        return []
    code_lower = code.lower()
    referenced = [
        c for c in candidates
        if c.lower() in code_lower or c.rsplit(".", 1)[0].lower() in code_lower
    ]
    missing = [c for c in candidates if c not in referenced]
    if missing and referenced:
        return [(
            "ERROR_CODE_INPUT_MISSED",
            f"Task supplies {len(candidates)} input files but the code only references "
            f"{len(referenced)}. Missing: {', '.join(missing)}.",
            "If every input is required, process each one explicitly.",
        )]
    return []


# osgeo subpackages we expect to be explicitly imported when used (Case 072).
# `from osgeo import gdal, ogr, osr` is the canonical form; LLMs sometimes
# import only a subset and then call into one they forgot.
_OSGEO_SUBPACKAGES = {"osr", "ogr", "gdal"}


def _preflight_collect_imported_names(tree):
    """Return the set of top-level names introduced by import / from-import."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import gdal` -> 'gdal'; `import osgeo.osr as osr` -> 'osr'.
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _preflight_check_double_wrap(tree, dict_var_map):
    """Reject `QgsVectorLayer(X, ...)` / `QgsRasterLayer(X, ...)` when X
    is statically known to already be a `QgsMapLayer` (case 023).

    Trigger pattern:
        result = processing.run("alg", {..., 'OUTPUT': 'TEMPORARY_OUTPUT'})
        path  = result['OUTPUT']                         # actually a layer obj
        QgsVectorLayer(path, 'name', 'ogr')              # <-- runtime TypeError
    or directly:
        QgsVectorLayer(result['OUTPUT'], 'name', 'ogr')

    This is documented in operation_requirement but the LLM keeps emitting
    it during debug retries because the variable name is misleading
    (`joined_layer_path` looks like a path even though it isn't).
    """

    def _output_returns_layer(call_node):
        """True iff this processing.run() call's OUTPUT will be a layer obj."""
        if not isinstance(call_node, ast.Call):
            return False
        f = call_node.func
        if not (isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name)
                and f.value.id == "processing"
                and f.attr in ("run", "runAndLoadResults")):
            return False
        if len(call_node.args) < 2:
            return True  # default OUTPUT is memory if omitted
        params = _preflight_resolve_params(call_node.args[1], dict_var_map)
        if params is None:
            return False  # opaque -> don't flag, avoid false positives
        out_val = params.get("OUTPUT", None)
        # If OUTPUT key is absent or set to TEMPORARY_OUTPUT / memory: / a
        # _PREFLIGHT_UNRESOLVED expression (not a path literal), the result
        # IS a layer object. Otherwise (concrete file path) it's a path str.
        if out_val is None:
            return True
        if isinstance(out_val, str):
            v = out_val.strip()
            if v == "" or v == "TEMPORARY_OUTPUT" or v.startswith("memory:"):
                return True
            return False
        # Unresolved (variable / f-string / call) — ambiguous; don't flag.
        return False

    # Vars holding the dict returned by processing.run that yields a layer.
    layer_result_vars = set()
    # Aliases like `path = result['OUTPUT']` — actually a layer.
    layer_alias_vars = {}  # name -> originating result var
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        # Pattern: result = processing.run(...)
        if (isinstance(node.value, ast.Call)
                and _output_returns_layer(node.value)):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    layer_result_vars.add(tgt.id)
        # Pattern: alias = <known_var>['OUTPUT']
        elif (isinstance(node.value, ast.Subscript)
              and isinstance(node.value.value, ast.Name)
              and node.value.value.id in layer_result_vars):
            sl = node.value.slice
            key = None
            if isinstance(sl, ast.Constant):
                key = sl.value
            if key == "OUTPUT":
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        layer_alias_vars[tgt.id] = node.value.value.id

    issues = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Name)
                and f.id in ("QgsVectorLayer", "QgsRasterLayer")):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        hint = None
        if isinstance(arg, ast.Name) and arg.id in layer_alias_vars:
            origin = layer_alias_vars[arg.id]
            hint = (f"`{f.id}({arg.id}, ...)` — `{arg.id}` was assigned "
                    f"`{origin}['OUTPUT']` and that is already a "
                    f"`Qgs{'Vector' if f.id == 'QgsVectorLayer' else 'Raster'}Layer` "
                    f"object (the run used TEMPORARY_OUTPUT / memory).")
        elif (isinstance(arg, ast.Subscript)
              and isinstance(arg.value, ast.Name)
              and arg.value.id in layer_result_vars
              and isinstance(arg.slice, ast.Constant)
              and arg.slice.value == "OUTPUT"):
            hint = (f"`{f.id}({arg.value.id}['OUTPUT'], ...)` — "
                    f"`{arg.value.id}['OUTPUT']` is already a layer object "
                    f"(the run used TEMPORARY_OUTPUT / memory).")
        elif (isinstance(arg, ast.Call)
              and _output_returns_layer(arg)):
            # Inline: QgsVectorLayer(processing.run(...), ...)
            hint = (f"`{f.id}(processing.run(...), ...)` — the inline run "
                    f"returns a dict with a layer object at ['OUTPUT']; "
                    f"you can't pass the dict to {f.id}.")
        if hint:
            issues.append((
                "ERROR_CODE_DOUBLE_WRAP",
                hint,
                f"Use the value directly: drop the `{f.id}(...)` wrapper "
                "and use the variable as-is. If you need to add it to the "
                "project, call `QgsProject.instance().addMapLayer(<var>)`. "
                "Re-wrapping a layer in its own constructor raises "
                "'argument 1 has unexpected type 'Qgs...Layer''.",
            ))
    return issues


def _preflight_check_addmaplayer_path(tree):
    """Reject `QgsProject.instance().addMapLayer(<str>)` (Cluster E / case 069).

    `addMapLayer` accepts only a `QgsMapLayer` object. Passing a path string
    raises 'QgsProject.addMapLayer(): argument 1 has unexpected type str' at
    runtime, which is not actionable from the LLM's perspective unless we
    surface the structural mistake here.
    """
    issues = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "addMapLayer"):
            continue
        if not node.args:
            continue
        arg = node.args[0]
        # String literal (incl. f-string with no formatting) -> definitely wrong
        bad = False
        hint = None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            bad, hint = True, arg.value[:120]
        elif isinstance(arg, ast.JoinedStr):
            bad, hint = True, "<f-string path>"
        # Name like `output_path` / `path` / a var ending in _path -> warn
        elif isinstance(arg, ast.Name) and (
            arg.id.lower().endswith(("_path", "path"))
            or arg.id.lower() in ("p", "filename")
        ):
            bad, hint = True, f"variable `{arg.id}` (looks like a path)"
        if bad:
            issues.append((
                "ERROR_CODE_LAYER_TYPE_MISMATCH",
                f"`addMapLayer({hint})` is being called with a path/string, "
                f"but addMapLayer requires a QgsMapLayer object.",
                "Wrap the path first: `lyr = QgsVectorLayer(path, name, 'ogr')` "
                "for vector or `QgsRasterLayer(path, name)` for raster, then "
                "`QgsProject.instance().addMapLayer(lyr)`.",
            ))
    return issues


def _preflight_check_pyqt5_imports(tree):
    """Reject `from PyQt5...` / `import PyQt5...` statements outright.

    This QGIS LTR build is Qt6-based; PyQt5 imports raise
    'PyQt5 classes cannot be imported in a QGIS build based on Qt6.'
    and abort the entire run. Catching it statically lets the retry loop
    rewrite the imports before exec().
    """
    issues = []
    for node in ast.walk(tree):
        bad_module = None
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "PyQt5" or mod.startswith("PyQt5."):
                bad_module = mod
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "PyQt5" or alias.name.startswith("PyQt5."):
                    bad_module = alias.name
                    break
        if bad_module:
            issues.append((
                "ERROR_CODE_PYQT5_FORBIDDEN",
                f"Code imports `{bad_module}` but this QGIS build is Qt6-based.",
                f"Replace with the version-independent shim: change "
                f"`{bad_module}` to `qgis.PyQt{bad_module[len('PyQt5'):]}` "
                f"(e.g. `from qgis.PyQt.QtCore import QVariant`, "
                f"`from qgis.PyQt.QtGui import QColor`). NEVER write 'PyQt5' "
                f"in any import statement.",
            ))
    return issues


def _preflight_check_osgeo_imports(tree):
    """Flag uses of `osr.X` / `ogr.X` / `gdal.X` whose name was never imported."""
    imported = _preflight_collect_imported_names(tree)
    used = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in _OSGEO_SUBPACKAGES):
            used.add(node.value.id)
    issues = []
    for name in sorted(used - imported):
        issues.append((
            "ERROR_CODE_NAME_NOT_IMPORTED",
            f"Code uses `{name}.<...>` but never imports `{name}` from osgeo.",
            f"Add `from osgeo import {name}` "
            f"(or extend an existing osgeo import, e.g. "
            f"`from osgeo import gdal, ogr, osr`).",
        ))
    return issues


def _preflight_autofix_code(code):
    """Apply text-level rewrites for algorithm IDs that we know are wrong
    in this Qt6 LTR build (Cluster C/D). Returns (new_code, fixes_applied)
    where fixes_applied is a list of human-readable descriptions.

    Why text-level and not AST: round-tripping through ast.unparse in 3.12
    drops f-string formatting, comments, and reflows lines, which makes the
    debug LLM's diff against the original much noisier. A scoped regex on
    the literal `processing.run("...:..."` arguments is sufficient because
    every wrong ID we know about is a string literal at that exact site.
    """
    if not isinstance(code, str) or "processing.run" not in code:
        # Still rewrite imports below.
        pass
    fixes = []
    new_code = code

    # 1) Known bad algorithm IDs (explicit map).
    for bad, good in _PREFLIGHT_BAD_ALGORITHM_IDS.items():
        for quote in ('"', "'"):
            needle = f"processing.run({quote}{bad}{quote}"
            replacement = f"processing.run({quote}{good}{quote}"
            if needle in new_code:
                new_code = new_code.replace(needle, replacement)
                fixes.append(f"rewrote algorithm ID `{bad}` -> `{good}`")
            # Also handle runAndLoadResults
            needle2 = f"processing.runAndLoadResults({quote}{bad}{quote}"
            replacement2 = f"processing.runAndLoadResults({quote}{good}{quote}"
            if needle2 in new_code:
                new_code = new_code.replace(needle2, replacement2)
                fixes.append(f"rewrote algorithm ID `{bad}` -> `{good}`")

    # 2) Generic grass7:* -> grass:* rewrite for any GRASS module the LLM
    #    invents under the legacy prefix. The Qt6 LTR build does not register
    #    ANY grass7:* IDs (verified via processingRegistry()).
    grass7_pattern = re.compile(
        r'(processing\.run(?:AndLoadResults)?\(\s*[\'"])grass7:'
    )
    if grass7_pattern.search(new_code):
        new_code, n = grass7_pattern.subn(r'\1grass:', new_code)
        fixes.append(f"rewrote {n} `grass7:*` algorithm ID(s) -> `grass:*`")

    # 3) PyQt5 import rewrite (Cluster A). The preflight check above raises
    #    a hard error, but if we can rewrite it deterministically, do so —
    #    saves an LLM round-trip.
    pyqt5_from = re.compile(r'^(\s*)from\s+PyQt5(\b|\.[\w.]*)\s+import\s+',
                            re.MULTILINE)
    if pyqt5_from.search(new_code):
        new_code, n = pyqt5_from.subn(r'\1from qgis.PyQt\2 import ', new_code)
        if n:
            fixes.append(f"rewrote {n} `from PyQt5...` import(s) -> `from qgis.PyQt...`")
    pyqt5_import = re.compile(r'^(\s*)import\s+PyQt5(\b|\.[\w.]*)',
                              re.MULTILINE)
    if pyqt5_import.search(new_code):
        new_code, n = pyqt5_import.subn(r'\1import qgis.PyQt\2', new_code)
        if n:
            fixes.append(f"rewrote {n} `import PyQt5...` statement(s) -> `import qgis.PyQt...`")

    # 4) Qt6 enum scoping (case 064). PyQt6 / Qt6 moved many enums into a
    #    nested scope, so legacy attribute access patterns now raise
    #    `type object 'QPainter' has no attribute 'Antialiasing'` and
    #    similar. The same translation applies inside qgis.PyQt — autofix
    #    so the LLM doesn't have to chase these one by one.
    qt6_enum_rewrites = [
        # QPainter.RenderHint.*
        (r"\bQPainter\.Antialiasing\b", "QPainter.RenderHint.Antialiasing"),
        (r"\bQPainter\.TextAntialiasing\b", "QPainter.RenderHint.TextAntialiasing"),
        (r"\bQPainter\.SmoothPixmapTransform\b",
            "QPainter.RenderHint.SmoothPixmapTransform"),
        (r"\bQPainter\.HighQualityAntialiasing\b",
            "QPainter.RenderHint.Antialiasing"),
        (r"\bQPainter\.LosslessImageRendering\b",
            "QPainter.RenderHint.LosslessImageRendering"),
        # QImage.Format.*
        (r"\bQImage\.Format_(ARGB32|RGB32|RGB888|Mono|Indexed8|Grayscale8|"
         r"ARGB32_Premultiplied|RGBA8888|RGBA8888_Premultiplied|RGB16)\b",
         r"QImage.Format.Format_\1"),
        # Qt.AlignmentFlag.*
        (r"\bQt\.Align(Left|Right|HCenter|Center|Top|Bottom|VCenter|Justify|"
         r"Absolute|Leading|Trailing)\b",
         r"Qt.AlignmentFlag.Align\1"),
        # Qt.GlobalColor.* (rare but seen)
        (r"\bQt\.(black|white|red|green|blue|cyan|magenta|yellow|"
         r"darkRed|darkGreen|darkBlue|darkCyan|darkMagenta|darkYellow|gray|darkGray|lightGray|transparent)\b",
         r"Qt.GlobalColor.\1"),
    ]
    qt6_total = 0
    for pattern, replacement in qt6_enum_rewrites:
        # Avoid double-rewriting if the code already uses the scoped form.
        new_code, n = re.subn(pattern, replacement, new_code)
        # Heuristic guard: don't double-up if the replacement already exists
        # alongside the original (e.g. "QPainter.RenderHint.Antialiasing"
        # then we'd match the inner "QPainter.Antialiasing" — fixed by \b).
        qt6_total += n
    if qt6_total:
        fixes.append(f"rewrote {qt6_total} Qt6 enum scope reference(s) "
                     f"(e.g. QPainter.Antialiasing -> QPainter.RenderHint.Antialiasing)")

    return new_code, fixes


def _preflight_validate(code, task="", data_path=""):
    """
    Static AST scan over the generated code. Raises RuntimeError with a
    structured `ERROR_CODE_XXX:` message on the first hard issue, so the
    auto-debug loop in execute_complete_program receives a precise
    correction signal. No-op when nothing is wrong (or when the AST cannot
    parse — syntax errors are left for exec() to surface naturally).
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return

    # Make raw source available to per-call checks that need to look for
    # inline guard patterns (e.g. assert est_cells < N).
    global _active_code_text
    _active_code_text = code

    # Resolve `params_X = {...}` assignments so processing.run("alg", params_X)
    # can be validated against the actual dict (case 028/029 false-positive).
    dict_var_map = _preflight_collect_dict_assignments(tree)

    all_issues = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "processing"
                and func.attr in {"run", "runAndLoadResults"}):
            continue
        if not node.args:
            continue
        alg = _preflight_literal(node.args[0])
        if not isinstance(alg, str):
            continue
        if len(node.args) >= 2:
            resolved = _preflight_resolve_params(node.args[1], dict_var_map)
        else:
            resolved = {}
        # `resolved is None` means the params dict was passed as an opaque
        # expression (e.g. computed in a function, branched, **kwargs, etc.).
        # In that case we skip "missing required key" checks to avoid the
        # case 028/029 false positive — the call may still be perfectly
        # valid at runtime.
        params_opaque = resolved is None
        params = resolved if isinstance(resolved, dict) else {}
        all_issues.extend(_preflight_check_call(
            alg, params, task, data_path, params_opaque=params_opaque
        ))

    all_issues.extend(_preflight_check_pyqt5_imports(tree))
    all_issues.extend(_preflight_check_addmaplayer_path(tree))
    all_issues.extend(_preflight_check_double_wrap(tree, dict_var_map))
    all_issues.extend(_preflight_check_osgeo_imports(tree))
    all_issues.extend(_preflight_check_multi_input(code, data_path))

    if not all_issues:
        return

    lines = [f"{code_id}: {msg} -> {suggestion}"
             for code_id, msg, suggestion in all_issues]
    raise RuntimeError("\n".join(lines))


def execute_complete_program(request_id, code: str, try_cnt: int, task: str, model_name: str, reasoning_effort_value:str, documentation_str: str,  data_path,
                             workspace_directory, stream,
                             review=True, reasoning_effort=None, session_context=None) -> (str, str):
    count = 0
    output_capture = io.StringIO()
    original_stdout = sys.stdout  # Save the original stdout

    error_collector = []

    kwargs = {}
    # Only pass reasoning_effort for GPT-5 models
    if reasoning_effort and model_name in ['gpt-5', 'gpt-5.1', 'gpt-5.2']:
        kwargs['reasoning_effort'] = reasoning_effort

    # Generate unique request ID to track all attempts for this user request
    # import uuid
    # request_id = str(uuid.uuid4())
    # print(f"REQUEST_ID:{request_id}")  # Use parseable format for UI to capture

    while count < try_cnt:
        print(f"\n\n-------------- Running code (trial # {count + 1}/{try_cnt}) --------------\n\n")
        original_stdout.flush()  # Ensure the message is printed immediately
        exec_error = None
        try:
            count += 1
            # Redirect stdout to capture print output
            sys.stdout = output_capture

            # Preflight autofix: rewrite known-bad algorithm IDs (e.g.
            # native:executesql -> qgis:executesql, grass7:* -> grass:*) and
            # PyQt5 imports inside the generated source, so the next step sees
            # corrected code and we don't burn an LLM round-trip on mechanical
            # fixups.
            fixed_code, autofixes = _preflight_autofix_code(code)
            if autofixes:
                code = fixed_code
                for fx in autofixes:
                    print(f"[preflight-autofix] {fx}")

            # Preflight: catch known parameter / algorithm-ID mistakes before
            # exec so the auto-debug loop gets a structured ERROR_CODE message.
            _preflight_validate(code, task=task, data_path=str(data_path or ""))

            compiled_code = compile(code, 'Complete program', 'exec')

            exec(compiled_code, globals())  # pass only globals()

            # Capture only user output (from exec), before appending meta info
            user_output = output_capture.getvalue()

            # Display the successfully executed code (goes to output_capture but
            # we already saved user_output above, so it won't be duplicated)
            print("\nSuccessfully executed code:")
            print("```python")
            print(code)
            print("```")
            print(f"\n\n--------------- Done ---------------\n\n")

            return code, user_output, error_collector

        except Exception as err:
            exec_error = err
        finally:
            # Always restore stdout, even if exec() or exception handler crashes
            sys.stdout = original_stdout

        # --- Error handling (after stdout is safely restored) ---
        if exec_error is None:
            continue

        err = exec_error
        # Capture full traceback for error reporting
        error_traceback = traceback.format_exc()
        error_collector.append({"attempt": count,
                                "code_snapshot": code[:800],  # truncate long code
                                "error_message": str(err),
                                "error_traceback": error_traceback
                                })

        if count == try_cnt:
            print(f"Failed to execute and debug the code within {try_cnt} times.")
            return code, output_capture.getvalue(), error_collector

        print("=" * 56)
        print("AI IS DEBUGGING THE CODE...")
        print("=" * 56)

        # Phase 3: 使用 SessionContext（如果提供）
        if session_context is not None:
            # 新模式：通过 SessionContext 构建上下文
            step_instruction = build_debug_instruction(
                code=code,
                error_msg=str(err),
                documentation_str=documentation_str
            )

            messages = session_context.build_messages(
                step="debug",
                step_instruction=step_instruction,
                step_role=constants.debug_role
            )
        else:
            # 旧模式：直接构建 prompt（向后兼容）
            debug_prompt = get_debug_prompt(
                exception=err, code=code, task=task,
                data_path=data_path, documentation_str=documentation_str
            )
            formatted_debug_prompt = f"{constants.debug_role}\n\n{debug_prompt}"
            messages = [{"role": "user", "content": formatted_debug_prompt}]

        print("DEBUGGING RESPONSE:", end="", flush=True)

        try:
            debug_response_str = unified_llm_call(
                request_id=request_id,
                messages=messages,
                model_name=model_name,
                stream=stream,
                **kwargs
            )
        except Exception as api_error:
            # If API call fails (e.g., invalid API key, network error), print error and continue to next iteration
            print(f"\n\nAPI call failed during debugging: {api_error}")
            print(f"Retrying with same code (attempt {count}/{try_cnt})...")
            continue

        # Extract code from the string response (same as code generation)
        code = extract_code_from_str(debug_response_str)

        # Emit the debugged code to the UI
        print("=" * 56, flush=True)
        print("\nDEBUGGED CODE:")
        print("```python")
        print(code)
        print("```")

        import urllib.parse
        print("CODE_READY_URLENCODED:" + urllib.parse.quote(code), flush=True)

        sys.stdout.flush()  # Force flush to ensure output reaches UI

    return code, output_capture.getvalue(), error_collector




def get_debug_prompt(exception, code, task, data_path, documentation_str):
    etype, exc, tb = sys.exc_info()
    exttb = traceback.extract_tb(tb)  # Do not quite understand this part.
    # https://stackoverflow.com/questions/39625465/how-do-i-retain-source-lines-in-tracebacks-when-running-dynamically-compiled-cod/39626362#39626362

    print("code in get_debug_prompt:", code)
    ## Fill the missing data:
    exttb2 = [(fn, lnnr, funcname,
               (code.splitlines()[lnnr - 1] if fn == 'Complete program'
                else line))
              for fn, lnnr, funcname, line in exttb]

    # Print:
    error_info_str = 'Traceback (most recent call last):\n'
    for line in traceback.format_list(exttb2[1:]):
        error_info_str += line
    for line in traceback.format_exception_only(etype, exc):
        error_info_str += line

    print(f"Error_info_str: \n{error_info_str}")

    # print(f"traceback.format_exc():\n{traceback.format_exc()}")

    debug_requirement_str = '\n'.join(
        [f"{idx + 1}. {line}" for idx, line in enumerate(constants.debug_requirement)])

    debug_prompt = f"Your role: {constants.debug_role} \n" + \
                   f"Your task: {constants.debug_task_prefix} \n\n" + \
                   f"The properties of the data are given below:\n{data_path}\n" + \
                   f"Requirement: \n {debug_requirement_str} \n\n" + \
                   f"Your reply examples: {constants.OperationIdentification_reply_example_1} + ' or ' + '{constants.OperationIdentification_reply_example_2}'. \n\n " + \
                   f"The given code is used for this task: {task} \n\n" + \
                   f"When you are correcting the codes, Check the task again to ensure that the correct parameters (such as attributes, data paths) are being used. \n\n" + \
                   f"The technical guidelines for the code: \n {documentation_str} \n\n" + \
                   f"The error information for the code is: \n{str(error_info_str)} \n\n" + \
                   f"The code is: \n{code}"

    return debug_prompt




def has_disconnected_components(directed_graph, verbose=True):
    # Get the weakly connected components
    weakly_connected = list(nx.weakly_connected_components(directed_graph))

    # Check if there is more than one weakly connected component
    if len(weakly_connected) > 1:
        if verbose:
            print("component count:", len(weakly_connected))
        return True
    else:
        return False


def read_html_graph_content(html_graph_path: str) -> str:
    """
    Read the HTML graph file content and return as string.

    Args:
        html_graph_path: Path to the HTML graph file

    Returns:
        str: Complete HTML content as string

    Raises:
        FileNotFoundError: If file doesn't exist
        IOError: If file cannot be read
    """

    if not os.path.exists(html_graph_path):
        raise FileNotFoundError(f"HTML graph file not found: {html_graph_path}")

    try:
        with open(html_graph_path, 'r', encoding='utf-8') as f:
            html_content = f.read()

        # print(f"✓ HTML graph loaded: {len(html_content)} bytes")
        return html_content

    except Exception as e:
        raise IOError(f"Error reading HTML graph file: {e}")


def generate_function_def(node_name, G):
    '''
    Return a dict, includes two lines: the function definition and return line.
    parameters: operation_node
    '''
    node_dict = G.nodes[node_name]
    node_type = node_dict['node_type']

    predecessors = G.predecessors(node_name)

    # print("predecessors:", list(predecessors))

    # create parameter list with default values
    para_default_str = ''  # for the parameters with the file path
    para_str = ''  # for the parameters without the file path
    for para_name in predecessors:
        # print("para_name:", para_name)
        para_node = G.nodes[para_name]
        # print(f"para_node: {para_node}")
        # print(para_node)
        data_path = para_node.get('data_path', '')  # if there is a path, the function need to read this file

        if data_path != "":
            para_default_str = para_default_str + f"{para_name}='{data_path}', "
        else:
            para_str = para_str + f"{para_name}={para_name}, "

    all_para_str = para_str + para_default_str

    function_def = f'{node_name}({all_para_str})'
    function_def = function_def.replace(', )', ')')  # remove the last ","

    # generate the return line
    successors = G.successors(node_name)
    return_str = 'return ' + ', '.join(list(successors))

    # print("function_def:", function_def)  # , f"node_type:{node_type}"
    # print("return_str:", return_str)  # , f"node_type:{node_type}"
    # print(function_def, predecessors, successors)
    return_dict = {"function_definition": function_def,
                   "return_line": return_str,
                   'description': node_dict['description'],
                   'node_name': node_name
                   }
    return return_dict


def bfs_traversal(graph, start_nodes):
    visited = set()
    queue = deque(start_nodes)

    order = []
    while queue:
        node = queue.popleft()
        # print(node)
        if node not in visited:
            order.append(node)
            visited.add(node)
            queue.extend(neighbor for neighbor in graph[node] if neighbor not in visited)
    return order


def generate_function_def_list(G):
    '''
    Return a list, each string is the function definition and return line
    '''
    # start with the data loading, following the data flow.
    nodes = []
    # Find nodes without predecessors
    nodes_without_predecessors = [node for node in G.nodes() if G.in_degree(node) == 0]
    # print(nodes_without_predecessors)
    # Traverse the graph using BFS starting from the nodes without predecessors
    traversal_order = bfs_traversal(G, nodes_without_predecessors)

    # print("traversal_order:", traversal_order)

    def_list = []
    data_node_list = []
    for node_name in traversal_order:
        node_type = G.nodes[node_name]['node_type']
        if node_type == 'operation':
            # print(node_name, node_type)
            # predecessors = G.predecessors('Load_shapefile')
            # successors = G.successors('Load_shapefile') 

            function_def_returns = generate_function_def(node_name, G)
            def_list.append(function_def_returns)

        if node_type == 'data':
            data_node_list.append(node_name)

    return def_list, data_node_list


def get_given_data_nodes(G):
    given_data_nodes = []
    for node_name in G.nodes():
        node = G.nodes[node_name]
        in_degrees = G.in_degree(node_name)
        if in_degrees == 0:
            given_data_nodes.append(node_name)
            # print(node_name,in_degrees,  node)
    return given_data_nodes


def get_data_loading_nodes(G):
    data_loading_nodes = set()

    given_data_nodes = get_given_data_nodes(G)
    for node_name in given_data_nodes:

        successors = G.successors(node_name)
        for node in successors:
            data_loading_nodes.add(node)
            # print(node_name,in_degrees,  node)
    data_loading_nodes = list(data_loading_nodes)
    return data_loading_nodes


def get_data_sample_text(file_path, file_type="csv", encoding="utf-8"):
    """
    file_type: ["csv", "shp", "txt"]
    return: a text string
    """
    if file_type == "csv":
        df = pd.read_csv(file_path)
        text = str(df.head(3))

    if file_type == "shp":
        try:
            from qgis.core import QgsVectorLayer
            layer = QgsVectorLayer(file_path, "temp_sample", "ogr")
            if layer.isValid():
                lines = []
                for i, feat in enumerate(layer.getFeatures()):
                    if i >= 2:
                        break
                    attrs = {field.name(): str(feat[field.name()]) for field in layer.fields()}
                    lines.append(str(attrs))
                text = "\n".join(lines)
            else:
                text = f"(Failed to open: {file_path})"
        except Exception as e:
            text = f"(Error reading shp: {e})"

    if file_type == "txt":
        with open(file_path, 'r', encoding=encoding) as f:
            lines = f.readlines()
            text = ''.join(lines[:3])
    return text


def show_graph(G):
    if has_disconnected_components(directed_graph=G):
        print("Disconnected component, please re-generate the graph!")

    nt = Network(notebook=True,
                 cdn_resources="remote",
                 directed=True,
                 # bgcolor="#222222",
                 # font_color="white",
                 height="800px",
                 # width="100%",
                 #  select_menu=True,
                 # filter_menu=True,

                 )

    nt.from_nx(G)

    sinks = find_sink_node(G)
    sources = find_source_node(G)
    # print("sinks:", sinks)

    # Set node colors based on node type
    node_colors = []
    for node in nt.nodes:
        # print('node:', node)
        if node['node_type'] == 'data':
            # print('node:', node)
            if node['label'] in sinks:
                node_colors.append('violet')  # lightgreen
                # print(node)
            elif node['label'] in sources:
                node_colors.append('lightgreen')  #
                # print(node)
            else:
                node_colors.append('orange')

        elif node['node_type'] == 'operation':
            node_colors.append('deepskyblue')

            # Update node colorsb
    for i, color in enumerate(node_colors):
        nt.nodes[i]['color'] = color
        # nt.nodes[i]['shape'] = 'box'
        nt.nodes[i]['shape'] = 'dot'
        # nt.set_node_style(node, shape="box")
        nt.nodes[i]['font'] = {'size': 20}  # set font size

    return nt


def find_sink_node(G):
    """
    Find the sink node in a NetworkX directed graph.

    :param G: A NetworkX directed graph
    :return: The sink node, or None if not found
    """
    sinks = []
    for node in G.nodes():
        if G.out_degree(node) == 0 and G.in_degree(node) > 0:
            sinks.append(node)
    return sinks


# Function to find the source node
def find_source_node(graph):
    # Initialize an empty list to store potential source nodes
    source_nodes = []

    # Iterate over all nodes in the graph
    for node in graph.nodes():
        # Check if the node has no incoming edges
        if graph.in_degree(node) == 0:
            # Add the node to the list of source nodes
            source_nodes.append(node)

    # Return the source nodes
    return source_nodes


# def Query_tuning_gpt(user_query):
#     OpenAI_key = load_OpenAI_key()
#     llm = ChatOpenAI(model_name="gpt-4o", openai_api_key=OpenAI_key)
#     cot_prompt = PromptTemplate(
#         input_variables=["query"],
#         template= constants.cot_description_prompt
#     )
#     task_chain = LLMChain(llm=llm, prompt=cot_prompt)
#     fine_tuned_request = task_chain.run(user_query)
#     # print(f"Preprocessed Task: {fine_tuned_request.strip()}")
#     return fine_tuned_request

# ============================================================
# Token Usage Tracker
# ============================================================

class TokenUsageTracker:
    """
    记录整个 task 流程中所有 unified_llm_call 的 token 用量和估算费用。
    使用：
        helper.get_token_tracker().reset()       # 新 task 开始时重置
        helper.get_token_tracker().get_summary() # 结果出来后获取报告
    """

    # 每 1M tokens 的美元价格（2025 年）
    PRICES = {
        'gpt-4o':                     {'input': 2.50,   'output': 10.00},
        'gpt-4o-mini':                {'input': 0.15,   'output': 0.60},
        'gpt-4':                      {'input': 30.00,  'output': 60.00},
        'gpt-5':                      {'input': 2.50,   'output': 10.00},
        'gpt-5.1':                    {'input': 2.50,   'output': 10.00},
        'o1':                         {'input': 15.00,  'output': 60.00},
        'o1-mini':                    {'input': 3.00,   'output': 12.00},
        'o3-mini':                    {'input': 1.10,   'output': 4.40},
        'deepseek-chat':              {'input': 0.14,   'output': 0.28},
        'deepseek-reasoner':          {'input': 0.55,   'output': 2.19},
        'claude-sonnet-4-20250514':   {'input': 3.00,   'output': 15.00},
        'claude-haiku-4-5-20251001':  {'input': 0.80,   'output': 4.00},
        'gemini-2.5-pro':             {'input': 1.25,   'output': 10.00},
        'gemini-2.5-flash':           {'input': 0.15,   'output': 0.60},
    }
    DEFAULT_PRICE = {'input': 2.50, 'output': 10.00}

    def __init__(self):
        self.reset()

    def reset(self):
        self._calls = []
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """字符数估算 token 数（英文 ÷4，中文 ÷3）"""
        if not text:
            return 0
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        ratio = chinese_chars / max(len(text), 1)
        chars_per_token = 3 if ratio > 0.2 else 4
        return max(1, len(text) // chars_per_token)

    def count_messages_tokens(self, messages: list) -> int:
        total = 0
        for msg in messages:
            content = msg.get('content', '')
            if isinstance(content, str):
                total += self._estimate_tokens(content)
            total += 4   # 每条消息 overhead
        return total + 3  # reply primer

    def record_call(self, model: str, input_tokens: int, output_tokens: int):
        price = self.PRICES.get(model, self.DEFAULT_PRICE)
        cost = (input_tokens * price['input'] + output_tokens * price['output']) / 1_000_000
        self._calls.append({
            'model': model,
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'cost_usd': cost,
        })
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cost_usd += cost

    def get_summary(self) -> str:
        sep = '─' * 48
        lines = [
            sep,
            f'📊 Token Usage & Cost Summary',
            sep,
            f'  API calls      : {len(self._calls)}',
            f'  Input  tokens  : {self.total_input_tokens:,}',
            f'  Output tokens  : {self.total_output_tokens:,}',
            f'  Total  tokens  : {self.total_input_tokens + self.total_output_tokens:,}',
            f'  Estimated cost : ${self.total_cost_usd:.4f} USD',
            sep,
        ]
        if self._calls:
            lines.append('  Per-call breakdown:')
            for i, c in enumerate(self._calls, 1):
                lines.append(
                    f'  [{i:02d}] {c["model"]}  '
                    f'in={c["input_tokens"]:,} out={c["output_tokens"]:,}  '
                    f'${c["cost_usd"]:.4f}'
                )
            lines.append(sep)
        return '\n'.join(lines)


# 模块级单例
_token_tracker = TokenUsageTracker()


def get_token_tracker() -> TokenUsageTracker:
    """获取全局 token 追踪器"""
    return _token_tracker


def _build_llm_retry_exception_tuple():
    """Collect transient-network exception classes that should trigger a
    retry. Built lazily so missing optional packages don't break import.
    """
    classes = [ConnectionError, TimeoutError]
    try:
        import httpx
        classes.extend([
            httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ReadError,
            httpx.ConnectError, httpx.ConnectTimeout, httpx.WriteError,
            httpx.PoolTimeout,
        ])
    except Exception:
        pass
    try:
        import httpcore
        classes.append(httpcore.RemoteProtocolError)
    except Exception:
        pass
    try:
        import openai
        for name in ("APIConnectionError", "APITimeoutError",
                     "InternalServerError"):
            cls = getattr(openai, name, None)
            if cls is not None:
                classes.append(cls)
    except Exception:
        pass
    try:
        import requests
        classes.extend([
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ])
    except Exception:
        pass
    return tuple(set(classes))


_LLM_RETRYABLE_EXCEPTIONS = _build_llm_retry_exception_tuple()
_LLM_MAX_ATTEMPTS = 3
_LLM_RETRY_BASE_SLEEP = 2.0


def unified_llm_call(request_id, messages, model_name, stream=False, temperature=1, **kwargs):
    """
    保留原 GIBD 代理、GPT5 reasoning_effort、Ollama 配置、streaming 处理
    仅改造：
    - 使用 create_unified_client() 动态 provider
    - 支持阶段四新增厂商和动态模型列表
    - 对短暂的网络/流式中断做指数退避重试（case 76 类的
      `httpx.RemoteProtocolError: peer closed connection ...`）
    """
    import time as _time

    last_error = None
    for attempt in range(1, _LLM_MAX_ATTEMPTS + 1):
        try:
            # 调用前：估算 input tokens
            input_tokens = _token_tracker.count_messages_tokens(messages)

            # 获取 provider 和 client
            client, provider = create_unified_client(model_name)
            model_kwargs = kwargs.copy()
            if 'reasoning_effort' in kwargs and model_name not in ['gpt-5','gpt-5.1','gpt-5.2']:
                model_kwargs.pop('reasoning_effort')

            response = provider.generate_completion(
                request_id=request_id,
                client=client,
                model=model_name,
                messages=messages,
                stream=stream,
                temperature=temperature,
                **model_kwargs
            )

            # Streaming 输出处理
            if stream:
                out = ''
                for chunk in response:
                    try:
                        content = chunk.choices[0].delta.content
                        if content:
                            print(content, end="")
                            out += content
                    except (IndexError, AttributeError):
                        # 兼容非标准流式响应（如 GIBD 代理返回的纯字符串）
                        if isinstance(chunk, str):
                            print(chunk, end="")
                            out += chunk
                output_tokens = _token_tracker._estimate_tokens(out)
                _token_tracker.record_call(model_name, input_tokens, output_tokens)
                return out
            else:
                # 非流式
                if hasattr(response, 'choices') and response.choices:
                    result = response.choices[0].message.content
                elif isinstance(response, str):
                    result = response.strip()
                else:
                    result = str(response)
                # 非流式：优先用 response.usage（OpenAI 直连时精确）
                if hasattr(response, 'usage') and response.usage:
                    exact_in = getattr(response.usage, 'prompt_tokens', None)
                    exact_out = getattr(response.usage, 'completion_tokens', None)
                    if exact_in is not None and exact_out is not None:
                        input_tokens, output_tokens = exact_in, exact_out
                    else:
                        output_tokens = _token_tracker._estimate_tokens(result)
                else:
                    output_tokens = _token_tracker._estimate_tokens(result)
                _token_tracker.record_call(model_name, input_tokens, output_tokens)
                return result
        except _LLM_RETRYABLE_EXCEPTIONS as net_err:
            last_error = net_err
            if attempt >= _LLM_MAX_ATTEMPTS:
                break
            backoff = _LLM_RETRY_BASE_SLEEP * (2 ** (attempt - 1))
            print(
                f"\n[unified_llm_call] transient network error on attempt "
                f"{attempt}/{_LLM_MAX_ATTEMPTS}: {type(net_err).__name__}: "
                f"{net_err}. Retrying in {backoff:.1f}s...",
                flush=True,
            )
            _time.sleep(backoff)

    raise last_error
    
def GIBD_Service_call(api_key, service_name, request_id, model_name, messages, stream, temperature, **kwargs):
    url = f"https://www.gibd.online/api/openai/{api_key}"
    payload = {
        "service_name": service_name,
        "question_id": request_id,
        "model": model_name,
        "messages": messages,
        "stream": stream,
        "temperature": temperature,
        **kwargs
    }

    if stream:
        # print("[DEBUG PRINT]: Using streaming GIBD API")
        response_req = requests.post(url, json=payload, stream=True)

        # Handle streaming
        def stream_generator():
            for line in response_req.iter_lines():
                if line:
                    line = line.decode('utf-8')
                    if line.startswith('data: '):
                        data_str = line[6:]
                        if data_str == '[DONE]':
                            break
                        try:
                            chunk = json.loads(data_str)
                            if 'choices' in chunk and len(chunk['choices']) > 0:
                                delta = chunk['choices'][0].get('delta', {})
                                content = delta.get('content')
                                if content:
                                    yield content
                        except json.JSONDecodeError:
                            pass

        # Collect streamed response
        out = ""
        for content in stream_generator():
            print(content, end="")
            out += content

        return out
    else:
        # print("[DEBUG PRINT]: Using Non streaming GIBD API")
        # Non-streaming
        response_req = requests.post(url, json=payload)
        if response_req.status_code == 200:
            data = response_req.json()
            content = data['choices'][0]['message']['content']
            print(content)
            return content
        else:
            print(f"\nError: {response_req.text}")


def streaming_openai_response(response):
    # Handle GPT-5 specialized response format first
    if hasattr(response, 'output'):
        # GPT-5 responses.create() format: response.output contains the content
        output = response.output
        if isinstance(output, str):
            return output.strip()
        elif hasattr(output, 'content'):
            return str(output.content).strip()
        else:
            return str(output).strip()

    # Handle GPT-5 alternative format: response.response.body
    if hasattr(response, 'response') and hasattr(response.response, 'body'):
        body = response.response.body
        # Check for choices format in body
        if hasattr(body, 'choices') and body.choices:
            if hasattr(body.choices[0], 'message'):
                content = body.choices[0].message.content
                return content.strip() if isinstance(content, str) else str(content).strip()
        # Check for content attribute in body
        if hasattr(body, 'content'):
            return str(body.content).strip()

    # Streaming case: iterator without .choices
    if hasattr(response, '__iter__') and not hasattr(response, 'choices'):
        out = ""
        for chunk in response:
            # Proxy strings/bytes
            if isinstance(chunk, (str, bytes)):
                t = chunk.decode("utf-8", "ignore") if isinstance(chunk, bytes) else chunk
                if t:
                    print(t, end="")
                    out += t
                continue

            # Try multiple ways to extract content from chunks
            content = None

            # GPT-5 ResponseCreatedEvent format - event-based streaming
            if hasattr(chunk, 'type'):
                try:
                    event_type = getattr(chunk, 'type', '')

                    # GPT-5 output_item events contain the content
                    if 'output_item' in event_type.lower():
                        if hasattr(chunk, 'item'):
                            item = chunk.item

                            # Try to extract text from the item
                            # Method 1: item.content (for text items)
                            if hasattr(item, 'content'):
                                item_content = item.content
                                if isinstance(item_content, str):
                                    content = item_content
                                # Check if content is a list with text parts
                                elif isinstance(item_content, list):
                                    for part in item_content:
                                        if hasattr(part, 'text'):
                                            content = part.text
                                            break
                                        elif isinstance(part, dict) and 'text' in part:
                                            content = part['text']
                                            break

                            # Method 2: item.text (direct text attribute)
                            if not content and hasattr(item, 'text'):
                                content = item.text

                            # Method 3: Try to dump and look for text
                            if not content and hasattr(item, 'model_dump'):
                                try:
                                    item_data = item.model_dump()
                                    if 'content' in item_data:
                                        content = item_data['content']
                                    elif 'text' in item_data:
                                        content = item_data['text']
                                except:
                                    pass

                    # Content delta events contain the actual text
                    elif 'content' in event_type.lower() and 'delta' in event_type.lower():
                        if hasattr(chunk, 'delta') and hasattr(chunk.delta, 'content'):
                            content = chunk.delta.content
                        elif hasattr(chunk, 'content'):
                            content = chunk.content

                    # For done events, check if there's output in response
                    elif event_type == 'response.done':
                        if hasattr(chunk, 'response'):
                            response_obj = chunk.response
                            # Check for output array
                            if hasattr(response_obj, 'output') and response_obj.output:
                                # Output is usually a list of items
                                for output_item in response_obj.output:
                                    if hasattr(output_item, 'content'):
                                        item_content = output_item.content
                                        if isinstance(item_content, str):
                                            content = item_content
                                            break
                                        elif isinstance(item_content, list):
                                            for part in item_content:
                                                if hasattr(part, 'text'):
                                                    content = part.text
                                                    break
                except Exception as e:
                    pass

            # Standard OpenAI ChatCompletionChunk format
            if not content:
                try:
                    if hasattr(chunk, 'choices') and chunk.choices:
                        content = getattr(chunk.choices[0].delta, "content", None)
                except:
                    pass

            # GPT-5 streaming format - check for delta.content directly
            if not content:
                try:
                    if hasattr(chunk, 'delta') and hasattr(chunk.delta, 'content'):
                        content = chunk.delta.content
                except:
                    pass

            # GPT-5 streaming format - check for content attribute directly
            if not content:
                try:
                    if hasattr(chunk, 'content'):
                        content = chunk.content
                except:
                    pass

            # GPT-5 format - check for output in chunk
            if not content:
                try:
                    if hasattr(chunk, 'output'):
                        content = chunk.output
                except:
                    pass

            if content:
                print(content, end="")
                out += str(content)

        print()
        return out

    # Non-streaming case - Standard OpenAI format
    if hasattr(response, "choices"):
        c = getattr(response.choices[0].message, "content", "")
        return c.strip() if isinstance(c, str) else (c or "")

    if isinstance(response, dict):
        c = response.get("choices", [{}])[0].get("message", {}).get("content", "")
        return c.strip() if isinstance(c, str) else (c or "")

    return str(response)


def Query_tuning(request_id, Query_tuning_prompt_str, model_name, stream, reasoning_effort=None):
    """Return a fine-tuned prompt using the selected model."""
    # if reasoning_effort:
    #     print(f"[DEBUG] select_source: reasoning_effort = {reasoning_effort}")

    kwargs = {}
    # Only pass reasoning_effort for GPT-5 models
    if reasoning_effort and model_name in ['gpt-5', 'gpt-5.1', 'gpt-5.2']:
        kwargs['reasoning_effort'] = reasoning_effort
        # print(f"[DEBUG] select_source: reasoning_effort ENABLED for {model_name}")
    # elif reasoning_effort:
    #     print(f"[DEBUG] select_source: reasoning_effort IGNORED for {model_name} (not supported)")


    return unified_llm_call(
        request_id=request_id,
        messages=[
            {"role": "user", "content": Query_tuning_prompt_str},
    ],
    model_name=model_name,
    stream=stream,
    **kwargs
    )

import re as _re

# ── Hard rules ──
_HARD_CHAT_PATTERNS = [
    _re.compile(r'^(你好|hi|hello|嗨|早上好|晚上好|hey|嘿|good\s*morning|good\s*evening)', _re.IGNORECASE),
    _re.compile(r'^(谢谢|好的|明白|收到|了解|ok|got\s*it|知道了|thanks|thank\s*you)', _re.IGNORECASE),
    _re.compile(r'你(是谁|能做什么|叫什么|会什么)', _re.IGNORECASE),
]
_HARD_GIS_PATTERNS = [
    _re.compile(r'(native:|qgis:|gdal:|grass7:|saga:)', _re.IGNORECASE),
    _re.compile(r'(这个图层|当前图层|加载的数据|已有数据|这些数据|当前数据)', _re.IGNORECASE),
]


def get_layer_info() -> str:
    """Collect loaded layer info from QGIS: name, type, geometry, fields, CRS, extent, and raster metadata."""
    try:
        from qgis.core import QgsProject, QgsVectorLayer, QgsRasterLayer, QgsWkbTypes
        layers = QgsProject.instance().mapLayers().values()
        if not layers:
            return "None"
        lines = []
        for layer in layers:
            name = layer.name()
            if isinstance(layer, QgsVectorLayer):
                geom = QgsWkbTypes.displayString(layer.wkbType())
                fields = [f.name() for f in layer.fields()][:10]
                crs = layer.crs().authid() if layer.crs().isValid() else "unknown"
                feature_count = layer.featureCount()
                ext = layer.extent()
                extent_str = f"{ext.xMinimum():.4f},{ext.yMinimum():.4f} : {ext.xMaximum():.4f},{ext.yMaximum():.4f}"
                lines.append(
                    f"- {name} (Vector/{geom})\n"
                    f"  CRS: {crs} | Features: {feature_count} | Extent: {extent_str}\n"
                    f"  Fields: {', '.join(fields)}"
                )
            elif isinstance(layer, QgsRasterLayer):
                crs = layer.crs().authid() if layer.crs().isValid() else "unknown"
                ext = layer.extent()
                extent_str = f"{ext.xMinimum():.4f},{ext.yMinimum():.4f} : {ext.xMaximum():.4f},{ext.yMaximum():.4f}"
                width = layer.width()
                height = layer.height()
                res_x = round(layer.rasterUnitsPerPixelX(), 6)
                res_y = round(layer.rasterUnitsPerPixelY(), 6)
                band_count = layer.bandCount()
                provider = layer.dataProvider()
                dtype = provider.dataTypeSize(1) if provider else "unknown"
                nodata = provider.sourceNoDataValue(1) if provider else "unknown"
                lines.append(
                    f"- {name} (Raster)\n"
                    f"  CRS: {crs} | Extent: {extent_str}\n"
                    f"  Size: {width}x{height} px | Resolution: ({res_x}, {res_y}) | Bands: {band_count}\n"
                    f"  DataType size (bits): {dtype} | NoData: {nodata}"
                )
            else:
                lines.append(f"- {name} (Other)")
        return '\n'.join(lines)
    except Exception:
        return "Unknown (failed to read QGIS layers)"


def _parse_intent_result(llm_response: str, valid_labels: list) -> str:
    """Extract the first matching label from LLM response text."""
    text = llm_response.strip().upper()
    for label in valid_labels:
        if label in text:
            return label
    return 'CHAT'  # safe default


def classify_intent(user_input: str, model_name: str,
                    state: str = 'IDLE',
                    plan_summary: str = '') -> str:
    """
    Two-layer intent classifier.
    Layer 1: Hard rules (0ms) — intercepts definite cases.
    Layer 2: LLM lightweight classification (1-3s) — semantic understanding.

    Returns: 'CHAT' | 'GIS_TASK' | 'PLAN_MODIFY' | 'UNCLEAR'
    """
    text = user_input.strip()

    # ── Layer 1: Hard rules ──
    for pattern in _HARD_CHAT_PATTERNS:
        if pattern.search(text):
            return 'CHAT'
    for pattern in _HARD_GIS_PATTERNS:
        if pattern.search(text):
            return 'GIS_TASK'

    # ── Layer 2: LLM classification ──
    layer_info = get_layer_info()

    if state == 'CONVERSING':
        prompt = constants.CONVERSING_INTENT_CLASSIFY_PROMPT.format(
            plan_summary=plan_summary or 'N/A',
            user_input=text
        )
        valid_labels = ['PLAN_MODIFY', 'UNCLEAR', 'CHAT']
    else:
        prompt = constants.IDLE_INTENT_CLASSIFY_PROMPT.format(
            layer_info=layer_info,
            user_input=text
        )
        valid_labels = ['GIS_TASK', 'UNCLEAR', 'CHAT']

    try:
        response = unified_llm_call(
            request_id=None,
            messages=[{"role": "user", "content": prompt}],
            model_name=model_name,
            stream=False,
            temperature=0
        )
        return _parse_intent_result(response, valid_labels)
    except Exception as e:
        print(f"[Intent Classifier] LLM call failed: {e}, defaulting to CHAT")
        return 'CHAT'



async def stream_llm_response(model, prompt_str):
    """
    Universal streaming function for all LLM calls.
    Streams output in real-time and returns the complete response.
    """
    complete_response = ""
    async for chunk in model.astream(prompt_str):
        chunk_content = chunk.content
        if chunk_content:
            print(chunk_content, end="")
            complete_response += chunk_content
    return complete_response

# async def Query_tuning_streaming(user_query, model_name="gpt-4o", stream=False):
#     """
#     Fine-tune user query using the specified model with streaming support.
#     Streams output in real-time and returns the complete response.
#     """
#     try:
#         # Check if this is a local model
#         import SpatialAnalysisAgent_ModelProvider as ModelProvider
#         provider_name = ModelProvider.ModelProviderFactory._model_providers.get(model_name, 'openai')
#
#         if provider_name == 'ollama':
#             # Use local model with LangChain ChatOpenAI pointing to local server
#             llm = OpenAI(
#                 base_url="http://128.118.54.16:11434/v1",
#                 api_key="no-api",
#                 model_name=model_name,
#                 openai_api_key="no-api"
#             )
#         else:
#             # Use OpenAI model
#             OpenAI_key = load_OpenAI_key()
#             if 'gibd-services' in (OpenAI_key or ''):
#                 url = f"http://128.118.54.16:3030/api/openai/{OpenAI_key}"
#                 payload = {"model": model_name, "messages": user_query, "stream": True}
#                 response=requests.post(url, json=payload, stream=True)
#                 # Stream or non-stream handling
#                 if stream:
#                     with response as r:
#                         for line in r.iter_lines():
#                             if line:
#                                 decoded_line = line.decode('utf-8')
#                                 print(decoded_line, flush=True)
#                     return  # Streamed output is already printed
#                 else:
#                     response = response
#                     return response.json()
#
#                 return response.text
#             else:
#                 llm = OpenAI(model_name=model_name, openai_api_key=OpenAI_key)
#
#
#     except ImportError:
#         # Fallback to OpenAI
#         OpenAI_key = load_OpenAI_key()
#         llm = OpenAI(model_name=model_name, openai_api_key=OpenAI_key)
#
#     # Create the formatted prompt
#     formatted_prompt = constants.cot_description_prompt.format(query=user_query)
#
#     # Use the universal streaming function
#     return await stream_llm_response(llm, formatted_prompt)


# def Query_tuning_streaming(user_query, model_name="gpt-4o"):
#     """
#     Fine-tune user query using the specified model with streaming support.
#     Streams output in real-time and returns the complete response.
#     """
#     try:
#         # Check if this is a local model
#         import SpatialAnalysisAgent_ModelProvider as ModelProvider
#         provider_name = ModelProvider.ModelProviderFactory._model_providers.get(model_name, 'openai')
#
#         if provider_name == 'ollama':
#             # Use local model with LangChain ChatOpenAI pointing to local server
#             llm = OpenAI(
#                 base_url="http://128.118.54.16:11434/v1",
#                 api_key="no-api",
#                 model_name=model_name,
#                 openai_api_key="no-api"
#             )
#         else:
#             try:
#                 from SpatialAnalysisAgent_ModelProvider import create_unified_client
#                 client, provider = create_unified_client(model_name)
#                 # Use OpenAI model
#                 OpenAI_key = load_OpenAI_key()
#                 # Generate response using the provider
#                 response = provider.generate_completion(
#                     client,
#                     model_name,
#                     user_query,
#                     stream=False
#                 )
#             except ImportError:
#                 # Fallback to basic OpenAI client
#                 client = create_openai_client()
#                 response = client.chat.completions.create(
#                     model=model_name,
#                     messages=[user_query]
#                 )
#             # Fallback to OpenAI
#             OpenAI_key = load_OpenAI_key()
#             llm = OpenAI(model_name=model_name, openai_api_key=OpenAI_key)
#
#     # Create the formatted prompt
#     formatted_prompt = constants.cot_description_prompt.format(query=user_query)
#
#     # Use the universal streaming function
#     return stream_llm_response(llm, formatted_prompt)


def generate_graph_response(request_id,task, task_explanation, data_path, graph_file_path, model_name='gpt-4o', stream=True, execute=True, reasoning_effort=None):
    """
    Generate LLM response for solution graph creation.
    This replaces solution.get_LLM_response_for_graph() with unified LLM call support.

    Args:
        task: The user's task description
        task_explanation: Detailed explanation/breakdown of the task
        data_path: List of data locations
        graph_file_path: Path where the graph file will be saved/loaded
        model_name: Model to use (supports proxy, GPT-5, Ollama, etc.)
        stream: Whether to stream the response
        streaming_callback: Optional callback function for streaming chunks
        execute: Whether to execute the generated code and load the graph

    Returns:
        tuple: (graph_response, code_for_graph, solution_graph)
               - graph_response: The LLM response text
               - code_for_graph: Extracted Python code from the response
               - solution_graph: The loaded NetworkX graph (None if execute=False or loading failed)
    """
    # Format data paths as numbered list
    data_path_str = '\n'.join([f"{idx + 1}. {line}" for idx, line in enumerate(data_path)])

    # Build graph requirements (from constants)
    graph_requirement = constants.graph_requirement.copy()
    graph_requirement_str = '\n'.join([f"{idx + 1}. {line}" for idx, line in enumerate(graph_requirement)])

    # Build the graph prompt
    graph_prompt = f'Your role: {constants.graph_role} \n\n' + \
                   f'Your task: {constants.graph_task_prefix} \n {task_explanation} \n\n' + \
                   f'Your reply needs to meet these requirements: \n {graph_requirement_str} \n\n' + \
                   f'Your reply example: {constants.graph_reply_exmaple} \n\n' + \
                   f'Data locations (each data is a node): {data_path_str} \n'

    # Use unified LLM call with streaming support
    messages = [
        {"role": "system", "content": constants.graph_role},
        {"role": "user", "content": graph_prompt}
    ]

    kwargs = {}
    # Only pass reasoning_effort for GPT-5 models
    if reasoning_effort and model_name in ['gpt-5', 'gpt-5.1', 'gpt-5.2']:
        kwargs['reasoning_effort'] = reasoning_effort

    response = unified_llm_call(
        request_id=request_id,
        messages=messages,
        model_name=model_name,
        stream=stream,
        temperature=1,
        **kwargs
    )
    graph_response = response

    # Extract code from LLM response (response is a string from unified_llm_call)
    try:
        code_for_graph = extract_code_from_str(LLM_reply_str=graph_response, verbose=False)
    except Exception as e:
        code_for_graph = ""
        print(f"Extract graph Python code from LLM failed: {e}")

    # Execute code and load graph if requested
    solution_graph = None
    if execute and code_for_graph:
        try:
            # Execute the code with a namespace to capture variables
            namespace = {'nx': nx}
            exec(code_for_graph, namespace)

            # The generated code typically creates a graph variable named 'G'
            if 'G' in namespace:
                G = namespace['G']
                # Save the graph to the GraphML file
                nx.write_graphml(G, graph_file_path)
                print(f"Graph saved to: {graph_file_path}")
            else:
                print("Warning: Graph variable 'G' not found in executed code")

            # Now load the graph file with metadata
            solution_graph = load_graph_file(graph_file_path)
        except Exception as e:
            print(f"Error executing graph code or loading graph: {e}")

    return graph_response, code_for_graph, solution_graph

def load_graph_file(graph_file_path):
    """
    Load a NetworkX graph from a GraphML file.
    This is a standalone function version of the kernel's load_graph_file method.

    Args:
        graph_file_path: Path to the .graphml file to load

    Returns:
        dict: A dictionary containing:
            - 'graph': The NetworkX graph object (None if file doesn't exist)
            - 'source_nodes': List of source nodes (nodes with no predecessors)
            - 'sink_nodes': List of sink nodes (nodes with no successors)
    """
    if not os.path.exists(graph_file_path):
        print(f"Graph file not found: {graph_file_path}")
        return {
            'graph': None,
            'source_nodes': [],
            'sink_nodes': []
        }

    try:
        # Load the graph from GraphML file
        G = nx.read_graphml(graph_file_path)

        # Find source and sink nodes
        source_nodes = find_source_node(G)
        sink_nodes = find_sink_node(G)

        return {
            'graph': G,
            'source_nodes': source_nodes,
            'sink_nodes': sink_nodes
        }
    except Exception as e:
        print(f"Error loading graph file {graph_file_path}: {e}")
        return {
            'graph': None,
            'source_nodes': [],
            'sink_nodes': []
        }

def OperationIdentification(request_id,OperationIdentification_prompt_str, model_name, stream, reasoning_effort=None):
    """Return a fine-tuned prompt using the selected model.
    Supports: OpenAI proxy, GPT-5, and normal OpenAI"""

    kwargs = {}
    if reasoning_effort and model_name in ['gpt-5', 'gpt-5.1', 'gpt-5.2']:
        kwargs['reasoning_effort'] = reasoning_effort

    return unified_llm_call(
        request_id=request_id,
        messages = [
        {"role": "user", "content": OperationIdentification_prompt_str},
    ],
    model_name=model_name,
    stream=stream,
    **kwargs
)


def tool_select(request_id,ToolSelect_prompt_str, model_name, stream, reasoning_effort=None):
    """Return a fine-tuned prompt using the selected model.
    Supports: OpenAI proxy, GPT-5, and normal OpenAI"""
    kwargs = {}
    kwargs = {}
    # Only pass reasoning_effort for GPT-5 models
    if reasoning_effort and model_name in ['gpt-5', 'gpt-5.1', 'gpt-5.2']:
        kwargs['reasoning_effort'] = reasoning_effort
    return unified_llm_call(
        request_id=request_id,
        messages = [
        {"role": "user", "content": ToolSelect_prompt_str},
    ],
    model_name=model_name,
    stream=stream,
    **kwargs
    )

# def get_combined_documentation_from_rag(tool_ids, model="gpt-4o", json_path = json_path ):
#     OpenAI_key = load_OpenAI_key()
#
#     # Load from JSON
#     with open(json_path, "r", encoding="utf-8") as f:
#         tool_data = json.load(f)
#     docs = []
#
#     for tool in tool_data:
#         tool_id = tool.get("tool_id", "")
#         name = tool.get("toolname", "")
#         desc = tool.get("tool_description", "")
#         params = tool.get("parameters", "")
#         example = tool.get("code_example", "")
#
#         page = f"""
#     Toolname: {name}
#     Tool ID: {tool_id}
#
#     Description:
#     {desc}
#
#     Parameters:
#     {params}
#
#     Code Example:
#     {example}
#     """
#         docs.append(Document(page_content=page, metadata={"tool_id": tool_id}))
#
#
#     # Create vector store
#     embeddings = OpenAIEmbeddings(openai_api_key=OpenAI_key)
#     vectorstore = FAISS.from_documents(docs, embeddings)
#     vectorstore.save_local("qgis_tool_documentation_faiss")
#
#     vectorstore = FAISS.load_local(
#         "qgis_tool_documentation_faiss",
#         embeddings,
#         allow_dangerous_deserialization=True
#     )
#     retriever = vectorstore.as_retriever()
#     llm = ChatOpenAI(model_name="gpt-4o", openai_api_key=OpenAI_key)
#
#     qa_chain = RetrievalQA.from_chain_type(
#         llm=llm,
#         retriever=retriever,
#         return_source_documents=True
#     )
#     doc_blocks = []
#     for tool_id in tool_ids:
#         query = f"Show full documentation for tool: {tool_id}"
#         response = qa_chain.invoke(query)
#         doc_blocks.append(response["result"])
#
#     return "\n\n".join(doc_blocks)





# def get_combined_documentation_with_fallback(tool_ids, all_documentation):
#     try:
#         combined_doc_rag = get_combined_documentation_from_rag(tool_ids)
#
#         # Check if RAG failed or returned generic apology
#         if not combined_doc_rag.strip() or "I don't have" in combined_doc_rag or "I'm sorry" in combined_doc_rag:
#             print("RAG did not return useful documentation. Switching to fallback (TOML).")
#             return '\n'.join(all_documentation)
#
#         return combined_doc_rag
#
#     except Exception as e:
#         print(f"RAG retrieval failed due to error: {e}")
#         print("Switching to fallback (TOML).")
#         return '\n'.join(all_documentation)


def get_openai_key(model_name: str):
    """
    Resolve the OpenAI key for the requested model.
    Uses ModelProvider to determine if the model is local (ollama) or remote.
    For remote models it loads the key from `helper.load_OpenAI_key()`
    and throws a ValueError if none is found.
    """
    try:
        # Import here to avoid import loops if helper.py is imported by other modules
        import SpatialAnalysisAgent_ModelProvider as ModelProvider
        provider_name = ModelProvider.ModelProviderFactory._model_providers.get(model_name, 'openai')
        if provider_name == 'ollama':
            return None  # Local models don't need an OpenAI key
        else:
            OpenAI_key = load_OpenAI_key()
            if not OpenAI_key:
                raise ValueError("Please enter a valid OpenAI API key for this model.")
            return OpenAI_key
    except Exception as e:
        # Fallback: try to load key, but catch errors gracefully
        try:
            return load_OpenAI_key()
        except Exception:
            print(f"Warning: Could not load OpenAI key - {e}")
            return None

def initialize_ai_model(model_name, reasoning_effort, OpenAI_key):
    print("=" * 56)
    print("MODEL CONFIGURATION INFO")
    print("=" * 56)
    print(f"Selected Model: {model_name}")

    # Import ModelProvider to determine the correct provider
    try:
        import SpatialAnalysisAgent_ModelProvider as ModelProvider
        # from langchain_openai import ChatOpenAI
        provider = ModelProvider.ModelProviderFactory.get_provider(model_name)
        provider_name = ModelProvider.ModelProviderFactory._model_providers.get(model_name, 'openai')

        if model_name == 'gpt-5':
            print("Model Type: GPT-5 (Specialized Provider)")
            reasoning_effort_value = globals().get('reasoning_effort', f'{reasoning_effort}')
            print(f"Reasoning Effort: {reasoning_effort_value}")
            print(f"Provider Class: {type(provider).__name__}")
            print(f"Provider Type: Specialized GPT-5 Provider")
            print(f"API Method: client.responses.create() with reasoning parameter")
            print(f"Reasoning Parameter: {{'effort': '{reasoning_effort_value}'}}")

            # Create model and store reasoning effort

            model = OpenAI(api_key=OpenAI_key, model=model_name, temperature=1)
            reasoning_effort = reasoning_effort_value

        elif provider_name == 'ollama':
            print(f"Using Ollama provider for local model")
            # 通过 Provider 获取 base_url 和 client，而不是硬编码
            from SpatialAnalysisAgent_ModelProvider import create_unified_client
            client, provider_instance = create_unified_client(model_name)
            model = client  # client 已经封装好 base_url 和 api_key

        else:
            print("Model Type: Standard OpenAI Model")
            print(f"Provider Class: {type(provider).__name__}")
            print("API Method: client.chat.completions.create()")
            model = OpenAI(api_key=OpenAI_key)

    except ImportError as e:
        print(f"WARNING: Could not import ModelProvider: {e}")
        print("Falling back to standard ChatOpenAI")
        model = OpenAI(api_key=OpenAI_key)

    # Display API Key status
    if 'gibd-services' in (OpenAI_key or ''):
        # print("API Key: ✓ Loaded (Provided by GIBD-services - http://128.118.54.16:3030/)")
        print("API Key (Provided by GIBD-services): ✓ Loaded")
    elif OpenAI_key:
        print("API Key: ✓ Loaded")
    else:
        print("API Key: Not required")
    return model

def ai_model(model_name, reasoning_effort_value, OpenAI_key):

    # Import ModelProvider to determine the correct provider
    try:
        import SpatialAnalysisAgent_ModelProvider as ModelProvider

        provider = ModelProvider.ModelProviderFactory.get_provider(model_name)
        provider_name = ModelProvider.ModelProviderFactory._model_providers.get(model_name, 'openai')

        if model_name == 'gpt-5':

            reasoning_effort_value = globals().get('reasoning_effort', f'{reasoning_effort_value}')
            # Create model and store reasoning effort
            model = OpenAI(api_key=OpenAI_key)
            reasoning_effort = reasoning_effort_value

        elif provider_name == 'ollama':
            # Create LangChain ChatOpenAI that points to local server
            from langchain_openai import OpenAI
            model =OpenAI(
                base_url="http://128.118.54.16:11434/v1",
                api_key="no-api",
            )

        else:
            model = OpenAI(api_key=OpenAI_key)

    except ImportError as e:
        model = OpenAI(api_key=OpenAI_key)
    return model





# **************************************************************************************
# DATA EYE
# *******************************************************************************

import sys
import os
import time
import pandas as pd
import geopandas as gpd
import rasterio
from openai import OpenAI
import configparser
import json


DataEye_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data_eye_constants')
if DataEye_path not in sys.path:
    sys.path.append(DataEye_path)

plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


#
def create_client():
    """Create OpenAI client with fresh API key from config file"""
    api_key = load_OpenAI_key()
    return OpenAI(api_key=api_key)


def get_data_overview(data_location_dict):
    data_locations = data_location_dict['data_locations']
    # print()
    for data in data_locations:
        try:
            meta_str = ''
            format_ = data['format']
            data_path = data['location']

            # print("data_path:", data_path)

            if format_ in constants.vector_formats:
                meta_str = see_vector(data_path)

            if format_ in constants.table_formats:
                meta_str = see_table(data_path)

            if format_ in constants.raster_formats:
                meta_str = see_raster(data_path)

            data['meta_str'] = meta_str

        except Exception as e:
            print("Error in get_data_overview()", data, e)
    return data_location_dict

#
def add_data_overview_to_data_location(task, data_location_list, model_name=r'gpt-4o-2024-08-06', stream=False):
    # Supports: OpenAI proxy, GPT-5, and normal OpenAI
    prompt = get_prompt_to_pick_up_data_locations(task=task,
                                                  data_locations=data_location_list)

    # Check if using proxy by examining API key
    api_key = load_OpenAI_key()

    if 'gibd-services' in (api_key or ''):
        # PROXY CASE: Use direct requests.post (same as data_eye.py)
        import requests
        url = f"https://www.gibd.online/api/openai/{api_key}"

        # Add JSON format instruction for proxy
        enhanced_prompt = prompt + "\n\nIMPORTANT: Respond with ONLY valid JSON matching this schema: {\"data_locations\": [{\"location\": \"path\", \"format\": \"format_type\"}, ...]}"

        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": constants.role},
                {"role": "user", "content": enhanced_prompt},
            ],
            "stream": False,
            "temperature": 1
        }
        response_req = requests.post(url, json=payload)
        data = response_req.json()

        # Extract and clean content (remove markdown code blocks)
        content = data['choices'][0]['message']['content']
        content = content.strip()
        if content.startswith('```json'):
            content = content[7:]
        if content.startswith('```'):
            content = content[3:]
        if content.endswith('```'):
            content = content[:-3]
        content = content.strip()

        attributes_json = json.loads(content)

    else:
        # NON-PROXY CASE: Use ModelProvider for GPT-5 and normal OpenAI
        try:
            from SpatialAnalysisAgent_ModelProvider import create_unified_client
            client, provider = create_unified_client(model_name)

            # Use beta.chat.completions.parse for structured output
            if client and hasattr(client, 'beta'):
                response = client.beta.chat.completions.parse(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": constants.role},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=1,
                    response_format=constants.Data_locations,
                )
                attributes_json = json.loads(response.choices[0].message.content)
            else:
                # Fallback for models that don't support beta API
                response = provider.generate_completion(
                    client,
                    model_name,
                    [
                        {"role": "system", "content": constants.role},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=1,
                    stream=False
                )
                # Clean markdown if present
                if hasattr(response, 'choices') and response.choices:
                    content = response.choices[0].message.content
                else:
                    content = str(response)
                content = content.strip()
                if content.startswith('```json'):
                    content = content[7:]
                if content.startswith('```'):
                    content = content[3:]
                if content.endswith('```'):
                    content = content[:-3]
                content = content.strip()
                attributes_json = json.loads(content)

        except ImportError:
            # Fallback to direct OpenAI client
            client = create_openai_client()
            response = client.beta.chat.completions.parse(
                model=model_name,
                messages=[
                    {"role": "system", "content": constants.role},
                    {"role": "user", "content": prompt},
                ],
                temperature=1,
                response_format=constants.Data_locations,
            )
            attributes_json = json.loads(response.choices[0].message.content)

    get_data_overview(attributes_json)

    for idx, data in enumerate(attributes_json.get('data_locations', [])):
        meta_str = data.get('meta_str','')
        if idx < len(data_location_list):  # Ensure index is valid
            if meta_str:  # Only append if meta_str is not empty
                data_location_list[idx] += ". Data overview: " + meta_str
        else:
            # Log or handle index out of range issue (optional)
            print(f"Index {idx} out of range for data_location_list.")
    return attributes_json, data_location_list





def get_prompt_to_pick_up_data_locations(task, data_locations):
    data_locations_str = '\n'.join([f"{idx + 1}. {line}" for idx, line in enumerate(data_locations)])
    prompt = f'Your mission: {constants.mission_prefix} \n\n' + \
             f'Given task description: {task} \n' + \
             f'Data location: \n{data_locations_str}'
    return prompt


def see_table(file_path):
    # print("OK")
    # print(file_path)
    # print(file_path[-3:])
    df = None
    if file_path[-4:].lower() == '.csv':
        # print(file_path)
        df = pd.read_csv(file_path)
        sample_df = pd.read_csv(file_path, dtype=str)

    # Use the formatted version for better readability
    column_lines = []
    for col, dtype in df.dtypes.items():
        sample_value = sample_df.iloc[0][col]
        column_lines.append(f"  - {col}: {dtype} (sample: {sample_value})")

    meta_str = "Columns:\n" + "\n".join(column_lines)
    return meta_str


def _get_df_types_str(df):
    samples = df.sample(1)
    # Format each column on a new line for better readability
    column_lines = []
    for col, dtype in df.dtypes.items():
        sample_value = samples.iloc[0][col]
        column_lines.append(f"  - {col}: {dtype} (sample: {sample_value})")

    types_str = "Columns:\n" + "\n".join(column_lines)
    return types_str


# see_vector 已统一为 QGIS 原生 API 版本（在文件末尾），避免 geopandas/pyarrow 崩溃。
# 如下是第一处旧定义（已删除），第二处在文件末尾。


def see_raster(file_path, statistics=False, approx=False):
    with rasterio.open(file_path) as dataset:
        raster_str = _get_raster_str(dataset, statistics=statistics, approx=approx)
    return raster_str


def _get_raster_str(dataset, statistics=False, approx=False):  # receive rasterio object
    raster_dict = dataset.meta
    raster_dict["band_count"] = raster_dict.pop("count")  # rename the key
    raster_dict["bounds"] = dataset.bounds
    if statistics:
        band_stat_dict = {}
        for i in range(1, raster_dict["band_count"] + 1):
            # need time to do that
            band_stat_dict[f"band_{i}"] = dataset.stats(indexes=i, approx=approx)
        raster_dict["statistics"] = band_stat_dict

    resolution = (dataset.transform[0], dataset.transform[4])
    raster_dict["resolution"] = resolution
    # print("dataset.crs:", dataset.crs)

    crs = dataset.crs

    if crs:
        if dataset.crs.is_projected:
            raster_dict["unit"] = dataset.crs.linear_units
        else:
            raster_dict["unit"] = "degree"
    else:
        raster_dict["Coordinate reference system"] = "unknown"
    # print("dataset.crs:", dataset.crs)

    raster_str = str(raster_dict)
    return raster_str


# beta vervsion of using structured output. # https://cookbook.openai.com/examples/structured_outputs_intro
# https://platform.openai.com/docs/guides/structured-outputs/examples
def get_LLM_reply(prompt,
                  model=r"gpt-4o",
                  verbose=True,
                  temperature=1,
                  stream=True,
                  retry_cnt=3,
                  sleep_sec=10,
                  ):

    count = 0
    isSucceed = False
    # response = None  # Initialize response variable
    while (not isSucceed) and (count < retry_cnt):
        try:
            count += 1
            # Create fresh client with updated API key
            client = create_client()
            response = client.beta.chat.completions.parse(model=model,
                                                          messages=[
                                                              {"role": "system", "content": constants.role},
                                                              {"role": "user", "content": prompt},
                                                          ],
                                                          temperature=temperature,
                                                          response_format=constants.Data_locations,
                                                          )
            isSucceed = True  # Mark as successful if we reach here
        except Exception as e:
            # logging.error(f"Error in get_LLM_reply(), will sleep {sleep_sec} seconds, then retry {count}/{retry_cnt}: \n", e)
            print(f"Error in get_LLM_reply(), will sleep {sleep_sec} seconds, then retry {count}/{retry_cnt}: \n",
                  e)
            time.sleep(sleep_sec)

    return response

def send_feedback(user_api_key, request_id, user_query, feedback, feedback_message, error_msg, error_traceback, generated_code, data_overview,
               task_breakdown=None, workflow_html_path=None, selected_tools=None):
# def send_error(user_api_key, request_id, user_query, feedback, feedback_message, error_msg, error_traceback, generated_code, data_overview,
#                task_breakdown=None, workflow_html_path=None, selected_tools=None):

    # Only send error reports if using gibd-services API key
    if 'gibd-services' not in (user_api_key or ''):
        # Return a mock response object for compatibility
        class MockResponse:
            status_code = 200
            text = "Error reporting skipped (not using gibd-services API key)"
            def json(self):
                return {}
        return MockResponse()

    url = f"https://www.gibd.online/api/feedback/{user_api_key}"

    # Data to send
    data = {
        "service": "GIS Copilot",
        "question_id": request_id,
        "question": user_query,
        "feedback": feedback,
        "feedback_message": feedback_message,
        "error_msg": str(error_msg),  # Convert error to string for JSON serialization
        "error_traceback": error_traceback,
        "generated_code": generated_code,
        "data_overview":data_overview,
        "task_breakdown": task_breakdown,
        "workflow": workflow_html_path,
        "selected_tools": selected_tools
    }
    # Send POST request
    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json=data
    )

    # # Handle response
    # if response.status_code == 201:
    #     result = response.json()
    #     print("Record created successfully!")
    #     print(json.dumps(result, indent=2))
    # else:
    #     print(f"Error {response.status_code}: {response.text}")

    return response






def get_model_for_operation(current_model):
    """
    Returns the current model if it's from Ollama provider, otherwise returns 'gpt-4o'
    """
    try:
        # from SpatialAnalysisAgent_ModelProvider import ModelProviderFactory
        # provider_name = ModelProviderFactory._model_providers.get(current_model, 'openai')
        # # If the model uses gpt-5, use gpt-4o as operation model
        if current_model == 'gpt-5':
            return 'gpt-4o'
        else:
            return current_model

        # return current_model if provider_name != 'gpt-5' else 'gpt-4o'
    except:
        # Fallback to gpt-4o if there's any issue
        return current_model


# *****************************************************************************************************************
# Data Eye
# ****************************************************************************************************************
def get_data_overview(data_location_dict):
    data_locations = data_location_dict['data_locations']
    # print()
    for data in data_locations:
        try:
            meta_str = ''
            format_ = data['format']
            data_path = data['location']

            # print("data_path:", data_path)

            if format_ in constants.vector_formats:
                meta_str = see_vector(data_path)

            if format_ in constants.table_formats:
                meta_str = see_table(data_path)

            if format_ in constants.raster_formats:
                meta_str = see_raster(data_path)

            data['meta_str'] = meta_str

        except Exception as e:
            print("Error in get_data_overview()", data, e)
    return data_location_dict


def add_data_overview_to_data_location(request_id, task, data_location_list, model_name, reasoning_effort=None):
    # Uses direct OpenAI client with structured output (works reliably)
    # The get_LLM_reply function uses client.beta.chat.completions.parse
    # which enforces JSON format via pydantic model
    prompt = get_prompt_to_pick_up_data_locations(task=task,
                                                  data_locations=data_location_list)
    enhanced_prompt = prompt + "\n\nIMPORTANT: Respond with ONLY valid JSON matching this schema: {\"data_locations\": [{\"location\": \"path\", \"format\": \"format_type\"}, ...]}"
    messages = [
        {"role": "system", "content": constants.eye_role},
        {"role": "user", "content": enhanced_prompt}
    ]

    kwargs = {}
    # Only pass reasoning_effort for GPT-5 models
    if reasoning_effort and model_name in ['gpt-5', 'gpt-5.1', 'gpt-5.2']:
        kwargs['reasoning_effort'] = reasoning_effort

    response = unified_llm_call(
        request_id=request_id,
        messages=messages,
        model_name=model_name,
        stream=False,
        temperature=1,
    **kwargs
    )
    # unified_llm_call returns a string, not a response object
    response_str = response.strip()
    # Clean markdown code blocks if present
    if response_str.startswith('```json'):
        response_str = response_str[7:]
    if response_str.startswith('```'):
        response_str = response_str[3:]
    if response_str.endswith('```'):
        response_str = response_str[:-3]
    response_str = response_str.strip()
    attributes_json = json.loads(response_str)
    get_data_overview(attributes_json)

    for idx, data in enumerate(attributes_json.get('data_locations', [])):
        meta_str = data.get('meta_str','')
        if idx < len(data_location_list):  # Ensure index is valid
            if meta_str:  # Only append if meta_str is not empty
                data_location_list[idx] += ". Data overview: " + meta_str
        else:
            # Log or handle index out of range issue (optional)
            print(f"Index {idx} out of range for data_location_list.")
    return attributes_json, data_location_list


def get_prompt_to_pick_up_data_locations(task, data_locations):
    data_locations_str = '\n'.join([f"{idx + 1}. {line}" for idx, line in enumerate(data_locations)])
    prompt = f'Your mission: {constants.mission_prefix} \n\n' + \
             f'Given task description: {task} \n' + \
             f'Data location: \n{data_locations_str}'
    return prompt
def see_table(file_path):
    # print("OK")
    # print(file_path)
    # print(file_path[-3:])
    df = None
    if file_path[-4:].lower() == '.csv':
        # print(file_path)
        df = pd.read_csv(file_path)
        sample_df = pd.read_csv(file_path, dtype=str)

    # Use the formatted version for better readability
    column_lines = []
    for col, dtype in df.dtypes.items():
        sample_value = sample_df.iloc[0][col]
        column_lines.append(f"  - {col}: {dtype} (sample: {sample_value})")

    meta_str = "Columns:\n" + "\n".join(column_lines)
    return meta_str

def _get_df_types_str(df):
    samples = df.sample(1)
    # Format each column on a new line for better readability
    column_lines = []
    for col, dtype in df.dtypes.items():
        sample_value = samples.iloc[0][col]
        column_lines.append(f"  - {col}: {dtype} (sample: {sample_value})")

    types_str = "Columns:\n" + "\n".join(column_lines)
    return types_str

def see_vector(file_path):
    """Read vector metadata using QGIS native API (avoids pyarrow/geopandas crash)."""
    try:
        from qgis.core import QgsVectorLayer, QgsWkbTypes
        from qgis.PyQt.QtCore import QVariant
        layer = QgsVectorLayer(file_path, "temp_inspect", "ogr")
        if not layer.isValid():
            return f"(Failed to open: {file_path})"

        fields = layer.fields()
        column_lines = []
        # Sample first feature for numeric/fallback values
        sample_feat = None
        for feat in layer.getFeatures():
            sample_feat = feat
            break

        for field in fields:
            name = field.name()
            dtype = field.typeName()
            field_idx = layer.fields().indexOf(name)
            if field.type() == QVariant.String:
                # Show up to 5 unique values so AI knows the exact string format
                raw_vals = layer.uniqueValues(field_idx, limit=5)
                unique_vals = [str(v) for v in raw_vals if v is not None and str(v) not in ('NULL', '')][:5]
                sample_str = f"unique samples: {unique_vals}"
            else:
                sample_value = str(sample_feat[name]) if sample_feat else ""
                sample_str = f"sample: {sample_value}"
            column_lines.append(f"  - {name}: {dtype} ({sample_str})")

        types_str = "Columns:\n" + "\n".join(column_lines)

        crs = layer.crs()
        crs_summary = crs.authid() if crs.isValid() else "unknown"

        geom_type = QgsWkbTypes.displayString(layer.wkbType())
        feature_count = layer.featureCount()

        meta_str = (f"{types_str}\n\n"
                    f"Geometry: {geom_type}, Features: {feature_count}\n"
                    f"Coordinate Reference System: {crs_summary}")
        return meta_str
    except Exception as e:
        return f"(Error reading vector: {e})"


# ============================================================================
# Phase 3: 新的步骤指令构建函数（用于 SessionContext.build_messages()）
# ============================================================================

def build_query_tuning_instruction(task: str) -> str:
    """
    构建 Query Tuning 步骤指令
    只返回该步骤特有的指令文本。
    角色、知识库、数据概览、对话历史由 SessionContext 统一注入。
    """
    Query_tuning_requirement_str = '\n'.join(
        [f"- {line}" for idx, line in enumerate(constants.Query_tuning_requirement)])

    Query_tuning_instructions_str = '\n'.join(
        [f"{idx + 1}. {line}" for idx, line in enumerate(constants.Query_tuning_instructions)])

    Output_Sample_str = '\n'.join(
        [f"{line}" for idx, line in enumerate(constants.Output_Sample)])

    instruction = f"""{constants.Query_tuning_prefix}

REQUIREMENTS:
{Query_tuning_requirement_str}

INSTRUCTIONS:
{Query_tuning_instructions_str}

User Query:
"{task}"

Output Sample:
{Output_Sample_str}
"""
    return instruction


def build_tool_selection_instruction(task_breakdown: str, candidate_tools_str: str = None) -> str:
    """
    构建 Tool Selection 步骤指令
    输出格式升级为结构化执行计划（JSON）
    """
    ToolSelect_requirement_str = '\n'.join(
        [f"{idx + 1}. {line}" for idx, line in enumerate(constants.ToolSelect_requirements)])

    # 如果传入了 embedding 检索的候选工具，用它；否则回退到全量 tools_index
    tools_str = candidate_tools_str if candidate_tools_str else str(constants.tools_index)

    instruction = f"""{constants.ToolSelect_prefix}

Task breakdown: {task_breakdown}

Requirements:
{ToolSelect_requirement_str}

Available tools:
{tools_str}

If none of the listed tools are suitable for a sub-task, respond with NEED_TOOL: <description of what you need>.

{constants.structured_tool_selection_output_format}

Example for simple task:
{constants.structured_tool_selection_example_simple}

Example for complex task:
{constants.structured_tool_selection_example_complex}
"""
    return instruction


def build_code_generation_instruction(
    task_description: str,
    data_path: str,
    selected_tool: str,
    selected_tool_ID: str,
    documentation_str: str
) -> str:
    """
    构建 Code Generation 步骤指令
    注意：current_plan（结构化执行计划）由 SessionContext 在 system message 的动态部分自动注入
    """
    operation_requirement_str = '\n'.join(
        [f"{idx + 1}. {line}" for idx, line in enumerate(constants.operation_requirement)])

    instruction = f"""{constants.operation_task_prefix}

Task: {task_description}

Data path: {data_path}

Selected tool: {selected_tool}
Tool ID: {selected_tool_ID}

Tool documentation:
{documentation_str}

Requirements:
{operation_requirement_str}
"""
    return instruction


def build_code_review_instruction(
    extracted_code: str,
    data_path: str,
    selected_tools: str,
    documentation_str: str
) -> str:
    """
    构建 Code Review 步骤指令
    """
    operation_code_review_requirement_str = '\n'.join(
        [f"{idx + 1}. {line}" for idx, line in enumerate(constants.operation_code_review_requirement)])

    instruction = f"""{constants.operation_code_review_task_prefix}

The code to review:
----------
{extracted_code}
----------

Data properties:
{data_path}

Selected tool(s): {selected_tools}

Tool documentation:
{documentation_str}

Requirements:
{operation_code_review_requirement_str}
"""
    return instruction


def build_debug_instruction(
    code: str,
    error_msg: str,
    documentation_str: str = ""
) -> str:
    """
    构建 Debug 步骤指令
    使用动态调试建议
    """
    debug_requirement_str = '\n'.join(
        [f"{idx + 1}. {line}" for idx, line in enumerate(constants.get_smart_debug_requirements(error_msg))])

    instruction = f"""{constants.debug_task_prefix}

Error message:
{error_msg}

Failed code:
```python
{code}
```

Tool documentation (if relevant):
{documentation_str}

Requirements:
{debug_requirement_str}
"""
    return instruction


def parse_structured_plan(llm_response: str) -> dict:
    """
    从 LLM 响应中提取结构化执行计划 JSON

    Args:
        llm_response: LLM 的原始响应文本

    Returns:
        解析后的 JSON 字典

    Raises:
        json.JSONDecodeError: JSON 解析失败
    """
    import json

    # 清理 markdown 代码块标记
    text = llm_response.strip()
    if text.startswith('```json'):
        text = text[7:]
    elif text.startswith('```'):
        text = text[3:]
    if text.endswith('```'):
        text = text[:-3]
    text = text.strip()

    # 解析 JSON
    plan = json.loads(text)
    return plan


def extract_tool_ids_from_plan(plan: dict) -> list:
    """
    从结构化计划中提取工具 ID 列表

    Args:
        plan: 结构化计划字典

    Returns:
        工具 ID 列表，如 ["native:buffer", "native:fieldcalculator"]
    """
    return [step["tool_id"] for step in plan.get("steps", [])]


# NOTE: A second `unified_llm_call` definition used to live here as part of a
# half-finished Phase 3 refactor. Its signature was
#     (request_id, messages, model_name, stream=True, reasoning_effort="medium")
# which silently overrode the feature-complete version at line ~1111 and broke
# every call site that passes `temperature=...` (the error shown in the chat
# window was: "unified_llm_call() got an unexpected keyword argument
# 'temperature'"). It also bypassed provider.generate_completion(), losing
# GIBD-proxy / GPT-5 / Ollama / non-standard streaming support. Removed — the
# original `unified_llm_call` above already covers the Phase 3 use cases.

