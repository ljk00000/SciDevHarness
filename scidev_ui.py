"""Switchable desktop workbench appearance profiles."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UIProfile:
    key: str
    label: str
    description: str
    layout_ratios: tuple[float, float]
    tree_mode: str
    syntax_colors: tuple[str, str, str, str, str]
    line_number_background: str
    line_number_foreground: str
    current_line_background: str
    stylesheet_overrides: str = ""


_PAPER_STYLESHEET = """
QMainWindow, QWidget#Root { background: #f4f6f9; color: #263346; }
QFrame#TitleBar, QFrame#ActivityRail, QFrame#ExplorerPane, QFrame#ChatPane,
QFrame#ChatHeader, QFrame#Composer, QStatusBar { background: #ffffff; }
QFrame#TitleBar, QFrame#ActivityRail, QFrame#ExplorerPane,
QFrame#ChatPane, QFrame#ChatHeader, QFrame#Composer {
    border-color: #dfe5ed;
}
QFrame#WorkspacePane, QFrame#WorkspaceToolbar, QFrame#EditorToolbar,
QFrame#GitPage, QTabWidget::pane, QTabWidget#BottomTabs::pane,
QPlainTextEdit#CodeEditor, QPlainTextEdit#TerminalOutput,
QPlainTextEdit#ProblemsOutput { background: #f7f9fc; color: #263346; }
QFrame#SummarySettings { background: #eef3f9; border-color: #dce4ee; }
QCheckBox { color: #33445a; }
QCheckBox::indicator:unchecked { background: #ffffff; border-color: #aebccd; }
QCheckBox::indicator:checked { background: #3278df; border-color: #3278df; }
QFrame#SummarySettings QLabel, QFrame#SummarySettings QLabel#Hint,
QLabel#Subtle, QLabel#Hint, QLabel#StatusText,
QLabel#Overline, QLabel#BubbleRole, QLabel#TreeLegend,
QLabel#FindStatus { color: #65758a; }
QLabel#AppTitle, QLabel#WindowTitle, QLabel#SectionTitle,
QLabel#SummarySettingsTitle, QLabel#MetricValue,
QLabel#GitDetailTitle { color: #202c3b; }
QLabel#Logo { background: #3278df; color: #ffffff; }
QLabel#AgentLogo { background: #167f91; color: #ffffff; }
QLabel#StateReady, QLabel#SummaryState { color: #17784f; }
QLabel#StateWorking { color: #9b650e; }
QLabel#StateError { color: #b43c37; }
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox {
    background: #ffffff; color: #263346; border-color: #d4dde8;
    selection-background-color: #c9dcf7; selection-color: #172b49;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QSpinBox:focus { border-color: #4d83d1; }
QLineEdit#CommandSearch { background: #f3f6fa; color: #263346; border-color: #d7e0ea; }
QTextEdit#ChatInput { background: #ffffff; border-color: #d2dce7; }
QPlainTextEdit#CodeEditor { border: none; }
QFrame#FindBar { background: #eef3f9; border-color: #dce4ee; }
QTabWidget#BottomTabs::pane, QTabWidget#BottomTabs QTabBar::tab,
QTabBar { background: #ffffff; }
QTabBar::tab { background: #ffffff; color: #69788b; border-color: #e2e7ed; }
QTabBar::tab:selected { background: #f7f9fc; color: #20324a; border-top-color: #3278df; }
QScrollArea, QScrollArea#ChatScroll, QScrollArea#ChatScroll > QWidget,
QWidget#ChatContent, QTreeView { background: #ffffff; color: #39495d; }
QTreeView { alternate-background-color: #f6f8fb; }
QTreeView::item:hover { background: #eef3f9; }
QTreeView::item:selected { background: #dce9fa; color: #193c68; }
QMenu, QDialog { background: #ffffff; color: #263346; border-color: #d7e0ea; }
QMenu::item:selected { background: #e8f0fb; color: #1d3b63; }
QMenu::separator { background: #e3e8ef; }
QPushButton, QToolButton {
    background: #f2f5f9; color: #2c3b50; border-color: #d8e0e9;
}
QPushButton:hover, QToolButton:hover { background: #e8eef5; border-color: #c6d1de; }
QPushButton:pressed, QToolButton:pressed { background: #dce5ef; }
QPushButton:disabled, QToolButton:disabled { background: #f1f3f6; color: #9aa5b2; border-color: #e4e8ed; }
QPushButton#Primary { background: #3278df; color: #ffffff; border-color: #3278df; }
QPushButton#Primary:hover { background: #286bcf; }
QPushButton#GitPrimary { background: #128575; color: #ffffff; border-color: #128575; }
QPushButton#GitPrimary:hover { background: #0e7668; }
QPushButton#Primary:disabled, QPushButton#GitPrimary:disabled { background: #e4e8ed; color: #98a2af; border-color: #e4e8ed; }
QPushButton#GitEntryButton { color: #284054; }
QPushButton#GitEntryButton:hover { background: #eaf1f6; }
QPushButton#Quiet:hover, QToolButton#Quiet:hover,
QToolButton#IconButton:hover { background: #edf2f7; color: #263346; }
QToolButton#ActivityButton { color: #718096; }
QToolButton#ActivityButton:hover { background: #edf2f8; color: #273a52; }
QToolButton#ActivityButton:checked { color: #286bcf; background: #e8f0fb; border-left-color: #3278df; }
QToolButton#ThemeSelector { background: #eef3fa; color: #315b89; border-color: #d8e3f0; }
QToolButton#ThemeSelector:hover { background: #e1ebf7; border-color: #c4d5e9; }
QToolButton#WindowButton { color: #617086; }
QToolButton#WindowButton:hover { background: #e9eef4; color: #25364c; }
QToolButton#WindowClose:hover { background: #c42b1c; color: #ffffff; }
QSplitter::handle { background: #e7ebf0; }
QSplitter::handle:hover, QSplitter#Workbench::handle:hover { background: #9ab8df; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal { background: #c3ceda; }
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover { background: #9eacbb; }
QStatusBar { color: #617086; border-top-color: #dfe5ed; }
QStatusBar QLabel, QStatusBar QLabel#StatusText { color: #526277; }
QFrame#UserBubble { background: #e7f0fb; border-color: #cfdef1; }
QFrame#AgentBubble { background: #f1f4f8; border-color: #e0e6ed; }
QFrame#ToolBubble { background: #eaf4ed; border-color: #d1e5d7; }
QFrame#MetaBubble { background: transparent; border: none; }
QFrame#SummaryBubble { background: #e8f4f8; border-color: #cfe6ee; }
QLabel#BubbleRole { color: #647489; }
QLabel#BubbleText { color: #2d3d52; }
QFrame#SummaryBubble QLabel#BubbleRole { color: #176b83; }
QFrame#SummaryBubble QLabel#BubbleText { color: #234454; }
QLabel#ToolText { color: #41644d; }
QLabel#Chip { color: #315b89; background: #edf3fa; border-color: #d9e4f0; }
QLabel#GitDetailStatus { color: #167a68; }
QFrame#GitPageHeader { background: #ffffff; border-color: #dfe5ed; }
QFrame#GitDetails { background: #f2f5f9; border-left-color: #dce3eb; }
QScrollArea#TreeScroll, QFrame#DevelopmentTree { background: #f5f8fb; }
QScrollArea#GitDetailsScroll, QScrollArea#GitDetailsScroll > QWidget { background: #f2f5f9; }
QFrame#MetricCard { background: #ffffff; border-color: #dce3eb; }
QFrame#GitEntry { background: #f2f7f6; border-color: #d5e5e2; border-left-color: #13907f; }
QFrame#GitEntry:hover { background: #ebf4f2; border-color: #b9d8d2; }
QTreeWidget { background: #ffffff; color: #263346; border-color: #d4dde8; }
QHeaderView::section { background: #f1f4f8; color: #33445a; border-bottom-color: #d4dde8; }
"""


_FOCUS_STYLESHEET = """
QMainWindow, QWidget#Root { background: #0b0d14; color: #e9e8f2; }
QFrame#TitleBar, QFrame#ActivityRail, QFrame#ExplorerPane,
QFrame#ChatPane, QFrame#ChatHeader, QFrame#Composer,
QFrame#GitPageHeader, QStatusBar { background: #12141d; }
QFrame#TitleBar, QFrame#ActivityRail, QFrame#ExplorerPane,
QFrame#ChatPane, QFrame#ChatHeader, QFrame#Composer,
QFrame#GitPageHeader { border-color: #2c2a3b; }
QFrame#WorkspacePane, QFrame#WorkspaceToolbar, QFrame#EditorToolbar,
QFrame#GitPage, QTabWidget::pane, QTabWidget#BottomTabs::pane,
QPlainTextEdit#CodeEditor, QPlainTextEdit#TerminalOutput,
QPlainTextEdit#ProblemsOutput { background: #0e1018; color: #e9e8f2; }
QFrame#SummarySettings { background: #1b1928; border-color: #39334d; }
QCheckBox { color: #e9e8f2; }
QCheckBox::indicator:unchecked { background: #20202e; border-color: #55506e; }
QCheckBox::indicator:checked { background: #7658df; border-color: #a58cff; }
QLabel#SummarySettingsTitle, QLabel#AppTitle, QLabel#WindowTitle,
QLabel#SectionTitle, QLabel#MetricValue, QLabel#GitDetailTitle,
QLabel#BubbleText { color: #f0eef8; }
QLabel#Subtle, QLabel#Hint, QLabel#StatusText, QLabel#Overline,
QLabel#BubbleRole, QLabel#TreeLegend, QLabel#FindStatus { color: #b0afc2; }
QLabel#StateReady, QLabel#SummaryState { color: #72dfb1; }
QLabel#StateWorking { color: #f1cb7c; }
QLabel#StateError { color: #ff8c92; }
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox {
    background: #191925; color: #edebf5; border-color: #39384c;
    selection-background-color: #554879; selection-color: #ffffff;
    border-radius: 9px;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QSpinBox:focus { border-color: #a58cff; }
QLineEdit#CommandSearch { background: #191925; color: #f0eef8; border-color: #39384c; }
QTextEdit#ChatInput { background: #171722; border-color: #46445a; }
QPlainTextEdit#CodeEditor { background: #0e1018; border: none; }
QFrame#FindBar { background: #1a1925; border-color: #39384c; }
QTabWidget#BottomTabs QTabBar::tab, QTabBar { background: #12141d; }
QTabBar::tab { background: #12141d; color: #aaa8bc; border-color: #2c2a3b; }
QTabBar::tab:selected { background: #0e1018; color: #f0eef8; border-top-color: #a58cff; }
QScrollArea, QScrollArea#ChatScroll, QScrollArea#ChatScroll > QWidget,
QWidget#ChatContent, QTreeView { background: #12141d; color: #dfdeeb; }
QTreeView { alternate-background-color: #171923; }
QTreeView::item:hover { background: #242333; }
QTreeView::item:selected { background: #332e49; color: #ffffff; }
QMenu, QDialog { background: #191925; color: #edebf5; border-color: #39384c; }
QMenu::item:selected { background: #332e49; color: #ffffff; }
QMenu::separator { background: #39384c; }
QPushButton, QToolButton { background: #20202e; color: #edebf5; border-color: #39384c; border-radius: 9px; }
QPushButton:hover, QToolButton:hover { background: #2b293b; border-color: #55506e; }
QPushButton:pressed, QToolButton:pressed { background: #302b43; }
QPushButton:disabled, QToolButton:disabled { background: #171720; color: #78768a; border-color: #282735; }
QPushButton#Primary { background: #7658df; color: #ffffff; border-color: #9b83f4; }
QPushButton#Primary:hover { background: #876ced; }
QPushButton#GitPrimary { background: #147f78; color: #ffffff; border-color: #36b3a4; }
QPushButton#GitPrimary:hover { background: #1b978d; }
QPushButton#Primary:disabled, QPushButton#GitPrimary:disabled { background: #171720; color: #78768a; border-color: #282735; }
QPushButton#GitEntryButton { color: #e5e2f0; }
QPushButton#GitEntryButton:hover { background: #242333; }
QPushButton#Quiet:hover, QToolButton#Quiet:hover,
QToolButton#IconButton:hover { background: #282637; color: #ffffff; }
QToolButton#ActivityButton { color: #aaa8bc; }
QToolButton#ActivityButton:hover { background: #242333; color: #ffffff; }
QToolButton#ActivityButton:checked { color: #b9a8ff; background: #242137; border-left-color: #a58cff; }
QToolButton#ThemeSelector { background: #211f2f; color: #d7ceff; border-color: #3f3a56; }
QToolButton#ThemeSelector:hover { background: #2c293e; border-color: #5a5278; }
QToolButton#WindowButton { color: #c0bdd0; }
QToolButton#WindowButton:hover { background: #2b293b; color: #ffffff; }
QToolButton#WindowClose:hover { background: #c42b1c; color: #ffffff; }
QSplitter::handle { background: #242333; }
QSplitter::handle:hover, QSplitter#Workbench::handle:hover { background: #a58cff; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal { background: #48445c; }
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover { background: #696283; }
QStatusBar { color: #b0afc2; border-top-color: #2c2a3b; }
QStatusBar QLabel, QStatusBar QLabel#StatusText { color: #c7c4d4; }
QFrame#UserBubble { background: #21253a; border-color: #333b59; }
QFrame#AgentBubble { background: #191925; border-color: #343244; }
QFrame#ToolBubble { background: #172821; border-color: #2c493a; }
QFrame#MetaBubble { background: transparent; border: none; }
QFrame#SummaryBubble { background: #1b2533; border-color: #344a60; }
QLabel#BubbleRole { color: #aaa8bc; }
QLabel#BubbleText { color: #eeedf5; }
QFrame#SummaryBubble QLabel#BubbleRole { color: #8bd4ed; }
QFrame#SummaryBubble QLabel#BubbleText { color: #e5f7ff; }
QLabel#ToolText { color: #b9d4c3; }
QLabel#Chip { color: #d7ceff; background: #201e2d; border-color: #39354e; }
QLabel#MetricValue, QLabel#GitDetailTitle { color: #f0eef8; }
QLabel#GitDetailStatus { color: #72dfd0; }
QFrame#GitDetails { background: #151620; border-left-color: #302e40; }
QScrollArea#TreeScroll, QFrame#DevelopmentTree { background: #11131b; }
QScrollArea#GitDetailsScroll, QScrollArea#GitDetailsScroll > QWidget { background: #151620; }
QFrame#MetricCard { background: #191925; border-color: #302e40; }
QFrame#GitEntry { background: #1a2027; border-color: #313c46; border-left-color: #53cbb7; }
QFrame#GitEntry:hover { background: #202832; border-color: #435565; }
QTreeWidget { background: #11131b; color: #e9e8f2; border-color: #39384c; }
QHeaderView::section { background: #191925; color: #edebf5; border-bottom-color: #39384c; }
"""


UI_PROFILES: dict[str, UIProfile] = {
    "studio": UIProfile(
        key="studio",
        label="Studio · 深色均衡",
        description="蓝青点缀的三栏深色工作台，保持当前熟悉的布局。",
        layout_ratios=(0.245, 0.275),
        tree_mode="studio",
        syntax_colors=("#c586c0", "#569cd6", "#b5cea8", "#6a9955", "#ce9178"),
        line_number_background="#1e1e1e",
        line_number_foreground="#858585",
        current_line_background="#252526",
    ),
    "paper": UIProfile(
        key="paper",
        label="Paper · 明亮浅色",
        description="柔和纸白底色与低饱和边框，适合明亮环境。",
        layout_ratios=(0.235, 0.265),
        tree_mode="paper",
        syntax_colors=("#7c3aed", "#0369a1", "#0f766e", "#697386", "#b45309"),
        line_number_background="#eef1f5",
        line_number_foreground="#738096",
        current_line_background="#edf2f7",
        stylesheet_overrides=_PAPER_STYLESHEET,
    ),
    "focus": UIProfile(
        key="focus",
        label="Focus · 专注宽编辑器",
        description="保留项目导航空间并收窄对话栏，为代码留出更宽的中央编辑区。",
        layout_ratios=(0.22, 0.20),
        tree_mode="focus",
        syntax_colors=("#d8a7ff", "#79c9ff", "#bfe584", "#80c995", "#ffbd85"),
        line_number_background="#151620",
        line_number_foreground="#858399",
        current_line_background="#1b1a28",
        stylesheet_overrides=_FOCUS_STYLESHEET,
    ),
}

DEFAULT_UI_PROFILE = "studio"


def ui_profile_for_key(key: object) -> UIProfile:
    """Return a valid appearance profile, falling back to the standard workbench."""
    return UI_PROFILES.get(str(key or ""), UI_PROFILES[DEFAULT_UI_PROFILE])


def stylesheet_for_profile(base_stylesheet: str, profile: UIProfile | str) -> str:
    """Compose the shared widget rules with the selected profile's overrides."""
    selected = ui_profile_for_key(profile if isinstance(profile, str) else profile.key)
    if not selected.stylesheet_overrides:
        return base_stylesheet
    return f"{base_stylesheet}\n{selected.stylesheet_overrides}"
