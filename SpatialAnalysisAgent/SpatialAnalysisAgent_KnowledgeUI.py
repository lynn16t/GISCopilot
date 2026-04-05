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

KNOWLEDGE_TAB_STYLE = """
/* ---- Global for the knowledge tab ---- */
QWidget#knowledge_container {
    background: #ffffff;
}

/* ---- Section headers ---- */
QLabel.section-header {
    font-size: 13px;
    font-weight: 600;
    color: #1a1a1a;
    padding: 0px;
    margin: 0px;
}

QLabel.section-hint {
    font-size: 11px;
    font-weight: 400;
    color: #8c8c8c;
    padding: 0px;
    margin: 0px;
}

QLabel.status-label {
    font-size: 11px;
    font-weight: 400;
    color: #8c8c8c;
    padding: 0px;
}

/* ---- Notes text editor ---- */
QPlainTextEdit#knowledge_notes_editor {
    background: #fafafa;
    border: 1px solid #e5e5e5;
    border-radius: 6px;
    padding: 10px;
    font-size: 12px;
    font-family: 'Consolas', 'SF Mono', 'Menlo', monospace;
    color: #1a1a1a;
    selection-background-color: #d4e4f7;
}
QPlainTextEdit#knowledge_notes_editor:focus {
    border: 1px solid #b0b0b0;
}

/* ---- Document list ---- */
QListWidget#knowledge_doc_list {
    background: #fafafa;
    border: 1px solid #e5e5e5;
    border-radius: 6px;
    padding: 4px;
    font-size: 12px;
    color: #1a1a1a;
    outline: none;
}
QListWidget#knowledge_doc_list::item {
    padding: 6px 8px;
    border-bottom: 1px solid #f0f0f0;
    border-radius: 0px;
}
QListWidget#knowledge_doc_list::item:last-child {
    border-bottom: none;
}
QListWidget#knowledge_doc_list::item:selected {
    background: #f0f4f8;
    color: #1a1a1a;
}
QListWidget#knowledge_doc_list::item:hover {
    background: #f5f5f5;
}

/* ---- Flat buttons ---- */
QPushButton.flat-btn {
    background: #f5f5f5;
    border: 1px solid #e0e0e0;
    border-radius: 6px;
    padding: 6px 14px;
    font-size: 12px;
    font-weight: 500;
    color: #1a1a1a;
}
QPushButton.flat-btn:hover {
    background: #ebebeb;
    border: 1px solid #d0d0d0;
}
QPushButton.flat-btn:pressed {
    background: #e0e0e0;
}

QPushButton.flat-btn-danger {
    background: #f5f5f5;
    border: 1px solid #e0e0e0;
    border-radius: 6px;
    padding: 6px 14px;
    font-size: 12px;
    font-weight: 500;
    color: #c0392b;
}
QPushButton.flat-btn-danger:hover {
    background: #fdf0ef;
    border: 1px solid #e8c4c0;
}
QPushButton.flat-btn-danger:pressed {
    background: #f5dbd8;
}

/* ---- Splitter handle ---- */
QSplitter::handle:vertical {
    height: 1px;
    background: #e5e5e5;
    margin: 8px 0px;
}
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
        dw.tabWidget.setTabText(tab_idx, "Knowledge")

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

    notes_header = QLabel("Notes")
    notes_header.setProperty("class", "section-header")
    header_row.addWidget(notes_header)

    notes_hint = QLabel("Data dictionaries, field descriptions, processing rules")
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
        "Write project-specific knowledge here.\n\n"
        "Example:\n"
        "  DLBM: Land use code (Third National Land Survey)\n"
        "    01xx = Cultivated land\n"
        "    03xx = Forest land\n"
        "  TBMJ: Parcel area in square meters\n"
        "  Use TBMJ field for area, not geometry calculation"
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

    docs_header = QLabel("Reference Documents")
    docs_header.setProperty("class", "section-header")
    docs_header_row.addWidget(docs_header)

    docs_hint = QLabel("PDF, DOCX, XLSX, CSV, TXT")
    docs_hint.setProperty("class", "section-hint")
    docs_header_row.addWidget(docs_hint)
    docs_header_row.addStretch()
    docs_layout.addLayout(docs_header_row)

    # Document list
    dw.knowledge_doc_list = QListWidget()
    dw.knowledge_doc_list.setObjectName("knowledge_doc_list")
    dw.knowledge_doc_list.setSelectionMode(QAbstractItemView.SingleSelection)
    dw.knowledge_doc_list.setMaximumHeight(180)
    docs_layout.addWidget(dw.knowledge_doc_list)

    # Buttons row
    btn_row = QHBoxLayout()
    btn_row.setSpacing(8)

    dw.knowledge_add_btn = QPushButton("Add document")
    dw.knowledge_add_btn.setProperty("class", "flat-btn")
    dw.knowledge_add_btn.setCursor(Qt.PointingHandCursor)
    btn_row.addWidget(dw.knowledge_add_btn)

    dw.knowledge_remove_btn = QPushButton("Remove selected")
    dw.knowledge_remove_btn.setProperty("class", "flat-btn-danger")
    dw.knowledge_remove_btn.setCursor(Qt.PointingHandCursor)
    dw.knowledge_remove_btn.setEnabled(False)
    btn_row.addWidget(dw.knowledge_remove_btn)

    btn_row.addStretch()
    docs_layout.addLayout(btn_row)

    # === Assemble with splitter ===
    splitter = QSplitter(Qt.Vertical)
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
    dw._knowledge_save_status.setText("Editing...")
    dw._knowledge_save_status.setStyleSheet("color: #b0b0b0;")
    dw._knowledge_save_timer.start()


def _save_notes(dw):
    """Actually persist the notes to disk."""
    text = dw.knowledge_notes_editor.toPlainText()
    dw.knowledge_manager.save_notes(text)
    dw._knowledge_save_status.setText("Saved")
    dw._knowledge_save_status.setStyleSheet("color: #27ae60;")

    # Fade out the status after 3 seconds
    QTimer.singleShot(3000, lambda: dw._knowledge_save_status.setText(""))


def _add_document(dw):
    """Open file dialog and add selected document."""
    file_path, _ = QFileDialog.getOpenFileName(
        dw,
        "Add reference document",
        "",
        "All supported (*.pdf *.docx *.xlsx *.xls *.csv *.tsv *.txt *.md *.json *.toml);;"
        "PDF (*.pdf);;"
        "Word (*.docx);;"
        "Excel (*.xlsx *.xls);;"
        "Text (*.txt *.md *.csv *.tsv *.json *.toml);;"
        "All files (*)"
    )
    if not file_path:
        return

    try:
        filename = dw.knowledge_manager.add_document(file_path)
        _refresh_doc_list(dw)
    except Exception as e:
        from PyQt5.QtWidgets import QMessageBox
        QMessageBox.warning(dw, "Error", f"Failed to add document:\n{e}")


def _remove_document(dw):
    """Remove the currently selected document."""
    items = dw.knowledge_doc_list.selectedItems()
    if not items:
        return

    filename = items[0].data(Qt.UserRole)
    if not filename:
        return

    dw.knowledge_manager.remove_document(filename)
    _refresh_doc_list(dw)


def _refresh_doc_list(dw):
    """Reload the document list widget from the knowledge manager."""
    dw.knowledge_doc_list.clear()
    docs = dw.knowledge_manager.list_documents()

    if not docs:
        item = QListWidgetItem("No documents added")
        item.setFlags(Qt.NoItemFlags)
        item.setForeground(QColor("#b0b0b0"))
        dw.knowledge_doc_list.addItem(item)
        return

    for doc in docs:
        display = f"{doc['filename']}    {doc['size_kb']} KB"
        item = QListWidgetItem(display)
        item.setData(Qt.UserRole, doc["filename"])
        dw.knowledge_doc_list.addItem(item)
