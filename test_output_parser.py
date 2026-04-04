#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试 OutputParser 和 GuardGate 功能
"""

import sys
import os

# 添加模块路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'SpatialAnalysisAgent'))

from SpatialAnalysisAgent_OutputParser import AgentOutputParser, OutputType
from SpatialAnalysisAgent_GuardGate import GuardGate


def test_code_detection():
    """测试代码检测"""
    parser = AgentOutputParser()

    # 测试1：包含 Python 代码块
    response1 = """Here is the code:
```python
import processing
result = processing.run("native:buffer", {...})
```
This code creates a buffer."""

    parsed = parser.parse(response1)
    assert parsed.output_type == OutputType.CODE, f"Expected CODE, got {parsed.output_type}"
    assert "import processing" in parsed.content
    print("[PASS] Code detection test 1 passed")

    # 测试2：不带 python 标记的代码块
    response2 = """Here is the code:
```
import processing
result = processing.run("native:buffer", {...})
```"""

    parsed = parser.parse(response2)
    assert parsed.output_type == OutputType.CODE, f"Expected CODE, got {parsed.output_type}"
    print("[PASS] Code detection test 2 passed")


def test_plan_detection():
    """测试计划检测"""
    parser = AgentOutputParser()

    # 测试：结构化 JSON 计划
    response = """{
  "steps": [
    {
      "step_number": 1,
      "operation": "Create 500m buffer around schools",
      "tool_id": "native:buffer",
      "input_layer": "schools.shp",
      "key_parameters": {
        "DISTANCE": 500
      },
      "output_description": "Buffer zones around all schools"
    }
  ]
}"""

    parsed = parser.parse(response)
    assert parsed.output_type == OutputType.PLAN, f"Expected PLAN, got {parsed.output_type}"
    assert "steps" in parsed.content
    assert len(parsed.content["steps"]) == 1
    assert parsed.content["steps"][0]["tool_id"] == "native:buffer"
    print("[PASS] Plan detection test passed")


def test_knowledge_update_detection():
    """测试知识库更新检测"""
    parser = AgentOutputParser()

    # 测试：包含知识库相关关键词和建议语气
    response = "Would you like to add this field description to the project knowledge base?"

    parsed = parser.parse(response)
    assert parsed.output_type == OutputType.KNOWLEDGE_UPDATE, \
        f"Expected KNOWLEDGE_UPDATE, got {parsed.output_type}"
    print("[PASS] Knowledge update detection test passed")


def test_question_detection():
    """测试提问检测"""
    parser = AgentOutputParser()

    # 测试1：以问号结尾
    response1 = "Which layer do you need for analysis?"
    parsed = parser.parse(response1)
    assert parsed.output_type == OutputType.QUESTION, \
        f"Expected QUESTION, got {parsed.output_type}"
    print("[PASS] Question detection test 1 passed")

    # 测试2：包含提问模式
    response2 = "Could you specify which buffer distance you need?"
    parsed = parser.parse(response2)
    assert parsed.output_type == OutputType.QUESTION, \
        f"Expected QUESTION, got {parsed.output_type}"
    print("[PASS] Question detection test 2 passed")


def test_chat_detection():
    """测试普通聊天检测"""
    parser = AgentOutputParser()

    # 测试：普通文本
    response = "Analysis completed. Results saved to output folder."

    parsed = parser.parse(response)
    assert parsed.output_type == OutputType.CHAT, \
        f"Expected CHAT, got {parsed.output_type}"
    print("[PASS] Chat detection test passed")


def test_guard_gate():
    """测试 GuardGate 决策"""
    from SpatialAnalysisAgent_OutputParser import ParsedOutput

    gate = GuardGate()

    # 测试 CODE 类型
    parsed_code = ParsedOutput(OutputType.CODE, "raw", content="print('hello')")
    action = gate.decide(parsed_code)
    assert action.action_type == "show_code"
    assert action.data["code"] == "print('hello')"
    print("[PASS] GuardGate CODE action test passed")

    # 测试 PLAN 类型
    plan_dict = {"steps": [{"step_number": 1, "operation": "test"}]}
    parsed_plan = ParsedOutput(OutputType.PLAN, "raw", content=plan_dict)
    action = gate.decide(parsed_plan)
    assert action.action_type == "confirm_plan"
    assert action.data["plan"] == plan_dict
    assert "plan_text" in action.data
    print("[PASS] GuardGate PLAN action test passed")

    # 测试 QUESTION 类型
    parsed_q = ParsedOutput(OutputType.QUESTION, "What do you need?",
                           content="What do you need?")
    action = gate.decide(parsed_q)
    assert action.action_type == "show_message"
    assert action.data["is_question"] == True
    print("[PASS] GuardGate QUESTION action test passed")

    # 测试 KNOWLEDGE_UPDATE 类型
    parsed_ku = ParsedOutput(OutputType.KNOWLEDGE_UPDATE, "raw",
                            content="Suggestion for knowledge base")
    action = gate.decide(parsed_ku)
    assert action.action_type == "confirm_knowledge"
    print("[PASS] GuardGate KNOWLEDGE_UPDATE action test passed")


def test_priority():
    """测试优先级规则"""
    parser = AgentOutputParser()

    # 测试：代码块中包含 JSON，应该识别为 CODE 而不是 PLAN
    response = """Here is the code:
```python
plan = {
  "steps": [{"tool_id": "native:buffer"}]
}
```"""

    parsed = parser.parse(response)
    assert parsed.output_type == OutputType.CODE, \
        f"Priority test failed: Expected CODE, got {parsed.output_type}"
    print("[PASS] Priority test passed")


if __name__ == "__main__":
    print("=" * 60)
    print("Testing OutputParser and GuardGate")
    print("=" * 60)

    try:
        test_code_detection()
        test_plan_detection()
        test_knowledge_update_detection()
        test_question_detection()
        test_chat_detection()
        test_guard_gate()
        test_priority()

        print("\n" + "=" * 60)
        print("[SUCCESS] All tests passed!")
        print("=" * 60)
    except AssertionError as e:
        print("\n" + "=" * 60)
        print(f"[FAIL] Test failed: {e}")
        print("=" * 60)
        sys.exit(1)
    except Exception as e:
        print("\n" + "=" * 60)
        print(f"[ERROR] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        print("=" * 60)
        sys.exit(1)
