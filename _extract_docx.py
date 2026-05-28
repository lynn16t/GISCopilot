from docx import Document
import sys
d = Document(r'C:/Users/indian/AppData/Roaming/QGIS/QGIS3/profiles/default/python/plugins/SpatialAnalysisAgent-master/初稿1.1.docx')
for p in d.paragraphs:
    sys.stdout.buffer.write((p.text + "\n").encode("utf-8"))
