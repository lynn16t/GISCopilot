"""
SpatialAnalysisAgent_KnowledgeUI.py

Builds the Project Knowledge tab UI programmatically.
Call `setup_knowledge_tab(dockwidget)` from dockwidget.__init__()
after the .ui file is loaded.

This replaces the empty Reports tab with:
  - Top: editable notes area (auto-save on 2s debounce)
  - Bottom: document list with add/remove

Style: flat, modern, no borders, inspired by Claude's project panel.
"""

import os
from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QListWidget, QListWidgetItem, QWidget,
    QFileDialog, QSplitter, QFrame, QAbstractItemView,
    QSizePolicy,
)
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QFont, QColor

from SpatialAnalysisAgent_KnowledgeManager import ProjectKnowledgeManager


# ---------------------------------------------------------------------------
# Stylesheet – flat, minimal, neutral palette
# ---------------------------------------------------------------------------

try:
    # Reuse the dock-wide Claude theme so this tab matches the rest of the UI.
    from theme import (
        BG_PRIMARY, BG_SECONDARY, BG_INPUT,
        TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED,
        BORDER, BORDER_STRONG, BORDER_FOCUS,
        ACCENT, ACCENT_SUBTLE,
        DANGER, DANGER_HOVER,
        NEUTRAL_BG, NEUTRAL_HOVER, NEUTRAL_PRESSED,
        BORDER_RADIUS_PX,
    )
except Exception:
    BG_PRIMARY = "#FAF9F5"; BG_SECONDARY = "#F0EEE6"; BG_INPUT = "#FFFFFF"
    TEXT_PRIMARY = "#3D3929"; TEXT_SECONDARY = "#6B6759"; TEXT_MUTED = "#9C9789"
    BORDER = "#E5E4DD"; BORDER_STRONG = "#D4D1C4"; BORDER_FOCUS = "#D97757"
    ACCENT = "#D97757"; ACCENT_SUBTLE = "#F5E6DD"
    DANGER = "#B26A60"; DANGER_HOVER = "#9A5950"
    NEUTRAL_BG = "#EBE9DF"; NEUTRAL_HOVER = "#DFDCCE"; NEUTRAL_PRESSED = "#CFCCBC"
    BORDER_RADIUS_PX = 6

_r = BORDER_RADIUS_PX

KNOWLEDGE_TAB_STYLE = f"""
/* ---- Global for the knowledge tab ---- */
QWidget#knowledge_container {{
    background: {BG_PRIMARY};
}}

/* ---- Section headers ---- */
QLabel.section-header {{
    font-size: 13px;
    font-weight: 600;
    color: {TEXT_PRIMARY};
    padding: 0px;
    margin: 0px;
}}
QLabel.section-hint {{
    font-size: 11px;
    color: {TEXT_MUTED};
    padding: 0px;
    margin: 0px;
}}
QLabel.status-label {{
    font-size: 11px;
    color: {TEXT_MUTED};
    padding: 0px;
}}

/* ---- Notes text editor ---- */
QPlainTextEdit#knowledge_notes_editor {{
    background: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: {_r}px;
    padding: 10px;
    font-size: 12px;
    font-family: 'Consolas', 'SF Mono', 'Menlo', monospace;
    color: {TEXT_PRIMARY};
    selection-background-color: {ACCENT_SUBTLE};
    selection-color: {TEXT_PRIMARY};
}}
QPlainTextEdit#knowledge_notes_editor:focus {{
    border: 1px solid {BORDER_FOCUS};
}}

/* ---- Document list ---- */
QListWidget#knowledge_doc_list {{
    background: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: {_r}px;
    padding: 4px;
    font-size: 12px;
    color: {TEXT_PRIMARY};
    outline: none;
}}
QListWidget#knowledge_doc_list::item {{
    padding: 6px 8px;
    border-bottom: 1px solid {BG_SECONDARY};
    border-radius: 0px;
}}
QListWidget#knowledge_doc_list::item:last-child {{ border-bottom: none; }}
QListWidget#knowledge_doc_list::item:selected {{
    background: {ACCENT_SUBTLE};
    color: {TEXT_PRIMARY};
}}
QListWidget#knowledge_doc_list::item:hover {{
    background: {BG_SECONDARY};
}}

/* ---- Flat buttons ---- */
QPushButton.flat-btn {{
    background: {NEUTRAL_BG};
    border: 1px solid {BORDER};
    border-radius: {_r}px;
    padding: 6px 14px;
    font-size: 12px;
    font-weight: 500;
    color: {TEXT_PRIMARY};
}}
QPushButton.flat-btn:hover {{
    background: {NEUTRAL_HOVER};
    border: 1px solid {BORDER_STRONG};
}}
QPushButton.flat-btn:pressed {{
    background: {NEUTRAL_PRESSED};
}}

QPushButton.flat-btn-danger {{
    background: {NEUTRAL_BG};
    border: 1px solid {BORDER};
    border-radius: {_r}px;
    padding: 6px 14px;
    font-size: 12px;
    font-weight: 500;
    color: {DANGER};
}}
QPushButton.flat-btn-danger:hover {{
    background: #F4E2DF;
    border: 1px solid {DANGER};
    color: {DANGER_HOVER};
}}
QPushButton.flat-btn-danger:pressed {{
    background: {DANGER};
    color: #FFFFFF;
}}

/* ---- Splitter handle ---- */
QSplitter::handle:vertical {{
    height: 1px;
    background: {BORDER};
    margin: 8px 0px;
}}
"""


# ---------------------------------------------------------------------------
# UI builder
# ---------------------------------------------------------------------------

def setup_knowledge_tab(dw):
    """
    Replace the Reports tab content with the Knowledge panel.

    Parameters
    ----------
    dw : SpatialAnalysisAgentDockWidget
        The main dock widget instance.  Expected to have:
        - dw.report_widget   (the empty QWidget from .ui)
        - dw.tabWidget        (the QTabWidget)
    """

    # ---- Initialise the knowledge manager ----
    dw.knowledge_manager = ProjectKnowledgeManager()

    # ---- Rename the tab ----
    tab_idx = dw.tabWidget.indexOf(dw.tab_4)  # tab_4 = Reports
    if tab_idx >= 0:
        dw.tabWidget.setTabText(tab_idx, "知识库")

    # ---- Remove old content from report_widget ----
    old_layout = dw.report_widget.layout()
    if old_layout:
        # Clear existing layout
        while old_layout.count():
            item = old_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        # Remove the old layout by reparenting
        QWidget().setLayout(old_layout)

    # ---- Build new layout ----
    container = QWidget()
    container.setObjectName("knowledge_container")
    container.setStyleSheet(KNOWLEDGE_TAB_STYLE)

    main_layout = QVBoxLayout(container)
    main_layout.setContentsMargins(12, 12, 12, 12)
    main_layout.setSpacing(6)

    # === Top section: Notes ===
    notes_widget = QWidget()
    notes_layout = QVBoxLayout(notes_widget)
    notes_layout.setContentsMargins(0, 0, 0, 0)
    notes_layout.setSpacing(4)

    # Header row
    header_row = QHBoxLayout()
    header_row.setSpacing(8)

    notes_header = QLabel("项目笔记")
    notes_header.setProperty("class", "section-header")
    header_row.addWidget(notes_header)

    notes_hint = QLabel("数据字典、字段说明、处理规则")
    notes_hint.setProperty("class", "section-hint")
    header_row.addWidget(notes_hint)
    header_row.addStretch()

    dw._knowledge_save_status = QLabel("")
    dw._knowledge_save_status.setProperty("class", "status-label")
    header_row.addWidget(dw._knowledge_save_status)

    notes_layout.addLayout(header_row)

    # Text editor
    dw.knowledge_notes_editor = QPlainTextEdit()
    dw.knowledge_notes_editor.setObjectName("knowledge_notes_editor")
    dw.knowledge_notes_editor.setPlaceholderText(
        "在此填写项目相关的知识。\n\n"
        "示例:\n"
        "  DLBM: 地类编码 (第三次全国国土调查)\n"
        "    01xx = 耕地\n"
        "    03xx = 林地\n"
        "  TBMJ: 图斑面积 (平方米)\n"
        "  面积请使用 TBMJ 字段,不要根据几何重新计算"
    )
    dw.knowledge_notes_editor.setTabStopDistance(28)
    notes_layout.addWidget(dw.knowledge_notes_editor)

    # === Bottom section: Documents ===
    docs_widget = QWidget()
    docs_layout = QVBoxLayout(docs_widget)
    docs_layout.setContentsMargins(0, 0, 0, 0)
    docs_layout.setSpacing(4)

    docs_header_row = QHBoxLayout()
    docs_header_row.setSpacing(8)

    docs_header = QLabel("参考文档")
    docs_header.setProperty("class", "section-header")
    docs_header_row.addWidget(docs_header)

    docs_hint = QLabel("PDF、DOCX、XLSX、CSV、TXT")
    docs_hint.setProperty("class", "section-hint")
    docs_header_row.addWidget(docs_hint)
    docs_header_row.addStretch()
    docs_layout.addLayout(docs_header_row)

    # Document list
    dw.knowledge_doc_list = QListWidget()
    dw.knowledge_doc_list.setObjectName("knowledge_doc_list")
    dw.knowledge_doc_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    dw.knowledge_doc_list.setMaximumHeight(180)
    docs_layout.addWidget(dw.knowledge_doc_list)

    # Buttons row
    btn_row = QHBoxLayout()
    btn_row.setSpacing(8)

    dw.knowledge_add_btn = QPushButton("添加文档")
    dw.knowledge_add_btn.setProperty("class", "flat-btn")
    dw.knowledge_add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn_row.addWidget(dw.knowledge_add_btn)

    dw.knowledge_remove_btn = QPushButton("移除所选")
    dw.knowledge_remove_btn.setProperty("class", "flat-btn-danger")
    dw.knowledge_remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    dw.knowledge_remove_btn.setEnabled(False)
    btn_row.addWidget(dw.knowledge_remove_btn)

    btn_row.addStretch()
    docs_layout.addLayout(btn_row)

    # === Assemble with splitter ===
    splitter = QSplitter(Qt.Orientation.Vertical)
    splitter.addWidget(notes_widget)
    splitter.addWidget(docs_widget)
    splitter.setStretchFactor(0, 3)  # notes get more space
    splitter.setStretchFactor(1, 1)
    splitter.setChildrenCollapsible(False)

    main_layout.addWidget(splitter)

    # Place container into the report_widget's slot
    wrapper_layout = QVBoxLayout(dw.report_widget)
    wrapper_layout.setContentsMargins(0, 0, 0, 0)
    wrapper_layout.addWidget(container)

    # ---- Auto-save timer (debounce) ----
    dw._knowledge_save_timer = QTimer()
    dw._knowledge_save_timer.setSingleShot(True)
    dw._knowledge_save_timer.setInterval(2000)  # 2 seconds after last keystroke
    dw._knowledge_save_timer.timeout.connect(lambda: _save_notes(dw))

    # ---- Connections ----
    dw.knowledge_notes_editor.textChanged.connect(
        lambda: _on_notes_changed(dw)
    )
    dw.knowledge_add_btn.clicked.connect(lambda: _add_document(dw))
    dw.knowledge_remove_btn.clicked.connect(lambda: _remove_document(dw))
    dw.knowledge_doc_list.itemSelectionChanged.connect(
        lambda: dw.knowledge_remove_btn.setEnabled(
            len(dw.knowledge_doc_list.selectedItems()) > 0
        )
    )


# ---------------------------------------------------------------------------
# Lifecycle: call when project/workspace changes
# ---------------------------------------------------------------------------

def init_knowledge_for_workspace(dw, workspace_dir: str, qgis_project_path: str = None):
    """
    Bind the knowledge manager to the current workspace/project.
    Call this on plugin startup and whenever the workspace changes.
    """
    km = dw.knowledge_manager
    root = km.get_knowledge_root_for_project(workspace_dir, qgis_project_path)
    km.set_root(root)

    # Load notes into editor
    dw.knowledge_notes_editor.blockSignals(True)
    dw.knowledge_notes_editor.setPlainText(km.get_notes())
    dw.knowledge_notes_editor.blockSignals(False)
    dw._knowledge_save_status.setText("")

    # Load document list
    _refresh_doc_list(dw)


# ---------------------------------------------------------------------------
# Internal handlers
# ---------------------------------------------------------------------------

def _on_notes_changed(dw):
    """Restart the debounce timer on every keystroke."""
    dw._knowledge_save_status.setText("编辑中...")
    dw._knowledge_save_status.setStyleSheet(f"color: {TEXT_MUTED};")
    dw._knowledge_save_timer.start()


def _save_notes(dw):
    """Actually persist the notes to disk."""
    text = dw.knowledge_notes_editor.toPlainText()
    dw.knowledge_manager.save_notes(text)
    dw._knowledge_save_status.setText("已保存")
    dw._knowledge_save_status.setStyleSheet(f"color: {ACCENT};")

    # Fade out the status after 3 seconds
    QTimer.singleShot(3000, lambda: dw._knowledge_save_status.setText(""))


def _add_document(dw):
    """Open file dialog and add selected document."""
    file_path, _ = QFileDialog.getOpenFileName(
        dw,
        "添加参考文档",
        "",
        "全部支持的格式 (*.pdf *.docx *.xlsx *.xls *.csv *.tsv *.txt *.md *.json *.toml);;"
        "PDF (*.pdf);;"
        "Word (*.docx);;"
        "Excel (*.xlsx *.xls);;"
        "文本 (*.txt *.md *.csv *.tsv *.json *.toml);;"
        "所有文件 (*)"
    )
    if not file_path:
        return

    try:
        filename = dw.knowledge_manager.add_document(file_path)
        _refresh_doc_list(dw)
    except Exception as e:
        from qgis.PyQt.QtWidgets import QMessageBox
        QMessageBox.warning(dw, "错误", f"添加文档失败:\n{e}")


def _remove_document(dw):
    """Remove the currently selected document."""
    items = dw.knowledge_doc_list.selectedItems()
    if not items:
        return

    filename = items[0].data(Qt.ItemDataRole.UserRole)
    if not filename:
        return

    dw.knowledge_manager.remove_document(filename)
    _refresh_doc_list(dw)


def _refresh_doc_list(dw):
    """Reload the document list widget from the knowledge manager."""
    dw.knowledge_doc_list.clear()
    docs = dw.knowledge_manager.list_documents()

    if not docs:
        item = QListWidgetItem("暂无文档")
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setForeground(QColor(TEXT_MUTED))
        dw.knowledge_doc_list.addItem(item)
        return

    for doc in docs:
        display = f"{doc['filename']}    {doc['size_kb']} KB"
        item = QListWidgetItem(display)
        item.setData(Qt.ItemDataRole.UserRole, doc["filename"])
        dw.knowledge_doc_list.addItem(item)
