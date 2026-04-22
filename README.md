 找到了核心函数，位于 SpatialAnalysisAgent_helper.py:1536。
                                                                                                                                                                                                                                        
  ---                                                                                                                                                                                                                                   
  get_layer_info() 函数的处理逻辑                                                                                                                                                                                                       
                                                                                                                                                                                                                                        
  矢量图层 (QgsVectorLayer)，处理更丰富：                                                                                                                                                                                               
  geom = QgsWkbTypes.displayString(layer.wkbType())   # 获取几何类型（Point/Line/Polygon等）                                                                                                                                            
  fields = [f.name() for f in layer.fields()][:10]    # 获取字段名，最多10个
  lines.append(f"- {name} (Vector/{geom}, Fields: {', '.join(fields)})")
  # 输出例：- 道路网 (Vector/LineString, Fields: id, name, type, length)

  栅格图层 (QgsRasterLayer)，处理极简：
  lines.append(f"- {name} (Raster)")
  # 输出例：- DEM高程 (Raster)
  没有任何额外信息——没有波段数、分辨率、数据类型等。

  ---
  差异对比

  ┌───────────────┬─────────────────┬──────┐
  │     信息      │      矢量       │ 栅格 │
  ├───────────────┼─────────────────┼──────┤
  │ 图层名        │ ✅              │ ✅   │
  ├───────────────┼─────────────────┼──────┤
  │ 几何类型      │ ✅ (如 Polygon) │ ❌   │
  ├───────────────┼─────────────────┼──────┤
  │ 字段名列表    │ ✅ (最多10个)   │ ❌   │
  ├───────────────┼─────────────────┼──────┤
  │ 波段/分辨率等 │ ❌              │ ❌   │
  └───────────────┴─────────────────┴──────┘

  潜在改进点

  栅格图层目前信息量很少，如果需要 LLM 更好地理解栅格数据，可以补充：
  - layer.bandCount() — 波段数
  - layer.width() / layer.height() — 像素尺寸
  - layer.dataProvider().dataType(1) — 数据类型
