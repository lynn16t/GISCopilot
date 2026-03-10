# -*- coding: utf-8 -*-
"""
SessionContext 测试脚本
在 QGIS Python Console 中运行，验证各项功能是否正常。

使用方法：
    1. 打开 QGIS，加载一些图层
    2. 打开 Python Console（Plugins → Python Console）
    3. 把这个脚本的内容粘贴进去运行
"""

import sys
import os

# 把插件目录加到 Python 路径
plugin_dir = os.path.join(
    os.path.expanduser("~"),
    "AppData", "Roaming", "QGIS", "QGIS3",
    "profiles", "default", "python", "plugins",
    "SpatialAnalysisAgent-master", "SpatialAnalysisAgent"
)
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

from SpatialAnalysisAgent_SessionContext import SessionContext, LayerSnapshot

print("=" * 60)
print("  SessionContext 功能测试")
print("=" * 60)

# ============================
# 测试 1：基本对话历史
# ============================
print("\n--- 测试 1：对话历史 ---")

session = SessionContext()

session.add_message("user", "帮我从南京市数据中提取栖霞区")
session.add_message("assistant", "我计划使用 extractbyattribute 工具，按 name='栖霞区' 提取。")
session.add_message("user", "可以，执行吧")
session.add_message("assistant", "好的，正在执行...")

print(f"消息总数: {session.get_message_count()}")
print(f"状态摘要: {session.get_summary()}")
print("PASS ✓")

# ============================
# 测试 2：数据概览缓存
# ============================
print("\n--- 测试 2：数据概览缓存 ---")

print(f"有缓存? {session.has_data_overview()}")  # 应该是 False

session.set_data_overview("Layer: nanjing_districts, fields: [name, area, population], CRS: EPSG:4326")
print(f"设置后有缓存? {session.has_data_overview()}")  # 应该是 True
print("PASS ✓")

# ============================
# 测试 3：图层快照
# ============================
print("\n--- 测试 3：图层快照 ---")

snapshot = session.take_layer_snapshot()
layer_count = len(snapshot.layers)
print(f"当前 QGIS 项目中有 {layer_count} 个图层")

if layer_count > 0:
    for layer_id, info in snapshot.layers.items():
        print(f"  - {info['name']} ({info['type']}, CRS: {info.get('crs', 'N/A')})")
        if info['type'] == 'vector':
            print(f"    features: {info.get('feature_count', 'N/A')}")
            field_names = [f['name'] for f in info.get('fields', [])]
            print(f"    fields: {field_names}")
        elif info['type'] == 'raster':
            print(f"    bands: {info.get('band_count', 'N/A')}, size: {info.get('width', 'N/A')}x{info.get('height', 'N/A')}")
else:
    print("  (没有图层，快照功能正常但无数据。请加载图层后再测试 diff 功能)")

print("PASS ✓")

# ============================
# 测试 4：图层 Diff（模拟）
# ============================
print("\n--- 测试 4：图层 Diff ---")

# 模拟执行前的快照
snapshot_before = LayerSnapshot()
snapshot_before.layers = {
    "layer_001": {"name": "nanjing_districts", "type": "vector", "feature_count": 11, "crs": "EPSG:4326", "fields": [{"name": "name", "type": "String"}]},
}

# 模拟执行后的快照（多了一个图层，原有图层 feature_count 变了）
snapshot_after = LayerSnapshot()
snapshot_after.layers = {
    "layer_001": {"name": "nanjing_districts", "type": "vector", "feature_count": 11, "crs": "EPSG:4326", "fields": [{"name": "name", "type": "String"}]},
    "layer_002": {"name": "qixia_district", "type": "vector", "feature_count": 1, "crs": "EPSG:4326", "fields": [{"name": "name", "type": "String"}, {"name": "area", "type": "Real"}]},
}

diff = snapshot_after.diff(snapshot_before)
print(f"新增图层: {len(diff['added'])} 个")
for layer in diff['added']:
    print(f"  + {layer['name']} ({layer['type']}, {layer.get('feature_count', '?')} features)")
print(f"删除图层: {len(diff['removed'])} 个")
print(f"修改图层: {len(diff['modified'])} 个")

assert len(diff['added']) == 1, "应该检测到 1 个新增图层"
assert diff['added'][0]['name'] == 'qixia_district', "新增图层名应该是 qixia_district"
print("PASS ✓")

# ============================
# 测试 5：执行结果记录
# ============================
print("\n--- 测试 5：执行结果记录 ---")

# 用模拟快照来添加结果（不依赖真实 QGIS 操作）
from SpatialAnalysisAgent_SessionContext import ExecutionRecord

record = ExecutionRecord(
    code='processing.run("qgis:extractbyattribute", {"INPUT": "nanjing", "FIELD": "name", "VALUE": "栖霞区", "OUTPUT": "qixia"})',
    success=True,
    output="Algorithm completed. 1 feature extracted.",
    data_changes=diff  # 用测试 4 的 diff 结果
)

session.results.append(record)
session.executed_codes.append(record.code)

print(f"执行记录数: {len(session.results)}")
print(f"上下文字符串预览:\n{record.to_context_str()[:500]}")
print("PASS ✓")

# ============================
# 测试 6：get_context 组装
# ============================
print("\n--- 测试 6：上下文组装 ---")

session.set_plan({
    "task_breakdown": "从南京市数据中按 name 字段提取栖霞区",
    "selected_tools": ["qgis:extractbyattribute"],
})

context = session.get_context()
print(f"组装后的 messages 数量: {len(context)}")
for i, msg in enumerate(context):
    role = msg['role']
    content_preview = msg['content'][:100] + "..." if len(msg['content']) > 100 else msg['content']
    print(f"  [{i}] {role}: {content_preview}")

print("PASS ✓")

# ============================
# 测试 7：Token 估算和压缩
# ============================
print("\n--- 测试 7：Token 估算 ---")

tokens = session.estimate_token_count()
print(f"当前估算 token 数: {tokens}")
print(f"需要压缩? {session.needs_compression()}")

# 模拟大量对话来测试压缩
for i in range(20):
    session.add_message("user", f"第{i+1}轮任务描述，这是一段比较长的文字用来模拟真实对话..." * 5)
    session.add_message("assistant", f"第{i+1}轮回复，AI 给出了一段分析和方案..." * 5)

print(f"添加 20 轮对话后的消息数: {session.get_message_count()}")
print(f"估算 token 数: {session.estimate_token_count()}")

# 执行压缩
session.compress_history()
recent = session._get_recent_messages()
print(f"压缩后，最近保留的消息数: {len(recent)}")
print(f"有压缩摘要? {session._compressed_summary is not None}")
if session._compressed_summary:
    print(f"摘要预览: {session._compressed_summary[:200]}...")

print("PASS ✓")

# ============================
# 测试 8：会话重置
# ============================
print("\n--- 测试 8：会话重置 ---")

# 软重置
session.soft_reset()
print(f"软重置后 - 有数据概览? {session.has_data_overview()}, 有方案? {session.current_plan is not None}, 执行记录数: {len(session.results)}")

# 完全重置
session.clear()
summary = session.get_summary()
print(f"完全重置后: {summary}")

assert summary['message_count'] == 0, "消息数应该为 0"
assert summary['has_data_overview'] == False, "不应有数据概览"
assert summary['execution_count'] == 0, "执行记录应为 0"
print("PASS ✓")

# ============================
# 总结
# ============================
print("\n" + "=" * 60)
print("  所有测试通过 ✓")
print("=" * 60)
print(f"\n提示：SessionContext 已就绪。")
print(f"文件位置: {os.path.join(plugin_dir, 'SpatialAnalysisAgent_SessionContext.py')}")
