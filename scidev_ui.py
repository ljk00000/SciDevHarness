"""Switchable desktop workbench appearance profiles."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from pathlib import Path
from scidev_preferences import AppearancePreferences


ACTIVITY_RAIL_WIDTH = 76
ACTIVITY_BUTTON_SIZE = 60
ACTIVITY_ICON_SIZE = 30
UI_FONT_FAMILIES = (
    "Microsoft YaHei UI", "Noto Sans SC", "Microsoft YaHei", "PingFang SC",
    "WenQuanYi Micro Hei", "Segoe UI", "DejaVu Sans",
)


def ui_font_family(available_families=None, *, preferred: str = "") -> str:
    """Choose an installed CJK sans-serif face instead of relying on glyph fallback."""
    if available_families is None:
        from PySide6.QtGui import QFontDatabase, QGuiApplication

        if QGuiApplication.instance() is None:
            return UI_FONT_FAMILIES[0]
        available_families = QFontDatabase.families()
    available = {family.casefold(): family for family in available_families if family.strip()}
    if preferred and preferred.casefold() in available:
        return available[preferred.casefold()]
    for family in UI_FONT_FAMILIES:
        if family.casefold() in available:
            return available[family.casefold()]
    return "sans-serif"


def create_ui_font(point_size: float = 11, family: str = "", weight: int = 400):
    """Shared production/screenshot font; code editors keep their own monospace face."""
    from PySide6.QtGui import QFont

    font = QFont(ui_font_family(preferred=family))
    font.setPointSizeF(point_size)
    font.setWeight(QFont.Weight(weight))
    font.setStyleHint(QFont.StyleHint.SansSerif)
    return font


def editor_font_family(preferred: str = "Consolas") -> str:
    from PySide6.QtGui import QFontDatabase, QGuiApplication

    if QGuiApplication.instance() is None:
        return preferred
    available = {family.casefold(): family for family in QFontDatabase.families() if family.strip()}
    for candidate in (preferred, "Consolas", "Cascadia Code", "DejaVu Sans Mono"):
        family = available.get(candidate.casefold())
        if family and QFontDatabase.isFixedPitch(family):
            return family
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()


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
    activity_icon_colors: tuple[str, str] = ("#8492a1", "#79b4ff")


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
QPlainTextEdit#ProblemsOutput, QPlainTextEdit#ProjectContextOutput { background: #f7f9fc; color: #263346; }
QFrame#SummarySettings { background: #eef3f9; border-color: #dce4ee; }
QFrame#PatchReviewCard { background: #eef3f9; border-color: #cbd8e7; }
QLabel#PatchReviewTitle { color: #263346; }
QLabel#PatchReviewSummary, QLabel#PatchReviewHunkText { color: #596b80; }
QPlainTextEdit#PatchReviewDiff { background: #ffffff; color: #263346; border-color: #d4dde8; }
QFrame#TaskPlan { background: #ffffff; border-color: #dfe5ed; }
QFrame#TaskPlanStep { background: #f6f8fb; border-color: #e1e7ef; }
QLabel#TaskPlanTitle, QLabel#TaskPlanStepText { color: #263346; }
QLabel#TaskPlanProgress, QLabel#TaskPlanEmpty, QLabel#TaskPlanStepStatus { color: #65758a; }
QLabel#TaskPlanNumber { color: #38516d; background: #e8eef5; }
QToolButton#TaskPlanMode, QToolButton#TaskPlanCollapse { color: #42546a; border-color: #d4dde8; }
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
QLabel#StateWorking { color: #8a5600; }
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
QSplitter::handle { background: transparent; }
QSplitter#Workbench::handle { background: transparent; border-left: 1px solid #d6dee8; }
QSplitter::handle:hover, QSplitter#Workbench::handle:hover { background: #dce8f7; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal { background: #c3ceda; }
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover { background: #9eacbb; }
QStatusBar { color: #617086; border-top-color: #dfe5ed; }
QStatusBar QLabel, QStatusBar QLabel#StatusText { color: #526277; }
QFrame#UserBubble { background: #e7f0fb; border-color: #cfdef1; }
QFrame#AgentBubble { background: #f1f4f8; border-color: #e0e6ed; }
QFrame#ToolBubble { background: #eaf4ed; border-color: #d1e5d7; }
QFrame#MetaBubble { background: transparent; border: none; }
QFrame#SummaryBubble { background: #e8f4f8; border-color: #cfe6ee; }
QFrame#NotificationToast { background: #ffffff; border-color: #efd0d0; }
QLabel#NotificationBadge { background: #f9e7e7; color: #a83c40; }
QLabel#NotificationTitle { color: #253347; }
QLabel#NotificationBody { color: #53647a; }
QToolButton#NotificationClose { color: #708096; }
QToolButton#NotificationClose:hover { background: #f0f3f7; color: #263346; }
QLabel#BubbleRole { color: #647489; }
QLabel#BubbleText { color: #2d3d52; }
QFrame#SummaryBubble QLabel#BubbleRole { color: #176b83; }
QFrame#SummaryBubble QLabel#BubbleText { color: #234454; }
QLabel#ToolText { color: #41644d; }
QLabel#Chip { color: #315b89; background: #edf3fa; border-color: #d9e4f0; }
QLabel#UsageQuiet { color: #748296; background: transparent; border: none; padding: 2px 4px; font-size: 9pt; }
QLabel#GitDetailStatus { color: #167a68; }
QFrame#GitPageHeader { background: #ffffff; border-color: #dfe5ed; }
QFrame#GitDetails { background: #f2f5f9; border-left-color: #dce3eb; }
QFrame#GitDetailContent { background: #f2f5f9; }
QFrame#GitCommitFooter { background: #ffffff; border-top: 1px solid #dce3eb; }
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
QPlainTextEdit#ProblemsOutput, QPlainTextEdit#ProjectContextOutput { background: #0e1018; color: #e9e8f2; }
QFrame#SummarySettings { background: #1b1928; border-color: #39334d; }
QFrame#TaskPlan { background: #14131d; border-color: #39334d; }
QFrame#TaskPlanStep { background: #1b1928; border-color: #39334d; }
QLabel#TaskPlanTitle, QLabel#TaskPlanStepText { color: #e9e8f2; }
QLabel#TaskPlanProgress, QLabel#TaskPlanEmpty, QLabel#TaskPlanStepStatus { color: #b0afc2; }
QLabel#TaskPlanNumber { color: #e4def9; background: #343047; }
QToolButton#TaskPlanMode, QToolButton#TaskPlanCollapse { color: #d0c9e4; border-color: #464159; }
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
QSplitter::handle { background: transparent; }
QSplitter#Workbench::handle { background: transparent; border-left: 1px solid #2c2a3b; }
QSplitter::handle:hover, QSplitter#Workbench::handle:hover { background: #363047; }
QScrollBar::handle:vertical, QScrollBar::handle:horizontal { background: #48445c; }
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover { background: #696283; }
QStatusBar { color: #b0afc2; border-top-color: #2c2a3b; }
QStatusBar QLabel, QStatusBar QLabel#StatusText { color: #c7c4d4; }
QFrame#UserBubble { background: #21253a; border-color: #333b59; }
QFrame#AgentBubble { background: #191925; border-color: #343244; }
QFrame#ToolBubble { background: #172821; border-color: #2c493a; }
QFrame#MetaBubble { background: transparent; border: none; }
QFrame#SummaryBubble { background: #1b2533; border-color: #344a60; }
QFrame#NotificationToast { background: #211d2b; border-color: #51404b; }
QLabel#NotificationBadge { background: #492b38; color: #ffb4c4; }
QLabel#NotificationTitle { color: #f0eef8; }
QLabel#NotificationBody { color: #c7c3d3; }
QToolButton#NotificationClose { color: #b0afc2; }
QToolButton#NotificationClose:hover { background: #302b3b; color: #ffffff; }
QLabel#BubbleRole { color: #aaa8bc; }
QLabel#BubbleText { color: #eeedf5; }
QFrame#SummaryBubble QLabel#BubbleRole { color: #8bd4ed; }
QFrame#SummaryBubble QLabel#BubbleText { color: #e5f7ff; }
QLabel#ToolText { color: #b9d4c3; }
QLabel#Chip { color: #d7ceff; background: #201e2d; border-color: #39354e; }
QLabel#MetricValue, QLabel#GitDetailTitle { color: #f0eef8; }
QLabel#GitDetailStatus { color: #72dfd0; }
QFrame#GitDetails { background: #151620; border-left-color: #302e40; }
QFrame#GitDetailContent { background: #151620; }
QFrame#GitCommitFooter { background: #191925; border-top: 1px solid #302e40; }
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
        layout_ratios=(0.20, 0.50),
        tree_mode="studio",
        syntax_colors=("#c586c0", "#569cd6", "#b5cea8", "#6a9955", "#ce9178"),
        line_number_background="#1e1e1e",
        line_number_foreground="#858585",
        current_line_background="#252526",
        activity_icon_colors=("#8995a3", "#8bc1ff"),
    ),
    "paper": UIProfile(
        key="paper",
        label="Paper · 明亮浅色",
        description="柔和纸白底色与低饱和边框，适合明亮环境。",
        layout_ratios=(0.20, 0.50),
        tree_mode="paper",
        syntax_colors=("#7c3aed", "#0369a1", "#0f766e", "#697386", "#b45309"),
        line_number_background="#eef1f5",
        line_number_foreground="#738096",
        current_line_background="#edf2f7",
        stylesheet_overrides=_PAPER_STYLESHEET,
        activity_icon_colors=("#68788d", "#286bcf"),
    ),
    "focus": UIProfile(
        key="focus",
        label="Focus · 专注宽编辑器",
        description="保留项目导航空间并收窄对话栏，为代码留出更宽的中央编辑区。",
        layout_ratios=(0.20, 0.20),
        tree_mode="focus",
        syntax_colors=("#d8a7ff", "#79c9ff", "#bfe584", "#80c995", "#ffbd85"),
        line_number_background="#151620",
        line_number_foreground="#858399",
        current_line_background="#1b1a28",
        stylesheet_overrides=_FOCUS_STYLESHEET,
        activity_icon_colors=("#aaa8bc", "#b9a8ff"),
    ),
}

DEFAULT_UI_PROFILE = "studio"

_SCALABLE_STYLE_DECLARATION = re.compile(
    r"(?<![\w-])(?:font-size|padding(?:-(?:left|right|top|bottom))?|"
    r"margin(?:-(?:left|right|top|bottom))?|spacing|min-width|max-width|"
    r"min-height|max-height|width|height|border-radius|icon-size)\s*:\s*[^;{}]+"
)
_STYLE_DIMENSION = re.compile(r"(?P<number>-?\d+(?:\.\d+)?)(?P<unit>px|pt)\b")


def ui_scale_for_resolution(width: int, height: int) -> float:
    """Return a modest logical-UI scale based on the screen's logical size."""
    if width <= 0 or height <= 0:
        return 1.0
    size_factor = math.sqrt((width / 1280) * (height / 720))
    progress = min(1.0, max(0.0, (size_factor - 1.0) / 2.0))
    return round(1.0 + 0.30 * progress**1.1, 3)


def _scale_stylesheet_metrics(stylesheet: str, ui_scale: float) -> str:
    scale = min(1.5, max(0.9, float(ui_scale)))
    if abs(scale - 1.0) <= 0.001:
        return stylesheet

    def scale_declaration(match: re.Match[str]) -> str:
        def scale_dimension(dimension: re.Match[str]) -> str:
            value = float(dimension.group("number"))
            unit = dimension.group("unit")
            scaled = value * scale
            if unit == "px":
                rounded = math.floor(scaled + 0.5) if scaled >= 0 else math.ceil(scaled - 0.5)
                if value > 0:
                    rounded = max(1, rounded)
                elif value < 0:
                    rounded = min(-1, rounded)
                return f"{rounded}{unit}"
            formatted = f"{scaled:.1f}".rstrip("0").rstrip(".")
            return f"{formatted}{unit}"

        return _STYLE_DIMENSION.sub(scale_dimension, match.group(0))

    return _SCALABLE_STYLE_DECLARATION.sub(scale_declaration, stylesheet)


def ui_profile_for_key(key: object) -> UIProfile:
    """Return a valid appearance profile, falling back to the standard workbench."""
    return UI_PROFILES.get(str(key or ""), UI_PROFILES[DEFAULT_UI_PROFILE])


def _focus_ring_stylesheet(color: str) -> str:
    return f"""
QPushButton:focus, QToolButton:focus, QTabBar::tab:focus {{ border-color: {color}; }}
QPushButton#Primary:focus, QPushButton#GitPrimary:focus,
QPushButton#GitEntryButton:focus, QPushButton#Quiet:focus,
QToolButton#Quiet:focus, QToolButton#ActivityButton:focus,
QToolButton#IconButton:focus, QToolButton#ThemeSelector:focus,
QToolButton#TabCloseButton:focus, QToolButton#WindowButton:focus,
QToolButton#WindowClose:focus {{ border: 1px solid {color}; }}
QTreeView::item:focus {{ border: 1px solid {color}; }}
"""


def stylesheet_for_profile(
    base_stylesheet: str,
    profile: UIProfile | str,
    ui_scale: float = 1.0,
    preferences: AppearancePreferences | None = None,
) -> str:
    """Compose shared/profile rules and scale readable UI metrics for the screen."""
    selected = ui_profile_for_key(profile if isinstance(profile, str) else profile.key)
    preferences = preferences or AppearancePreferences()
    stylesheet = "\n".join(
        part
        for part in (
            base_stylesheet,
            selected.stylesheet_overrides,
            _complete_control_styles(selected.key),
            _readable_typography_styles(selected.key, preferences),
            _custom_appearance_styles(preferences),
            _focus_ring_stylesheet(preferences.accent_color or selected.activity_icon_colors[1]),
        )
        if part
    )
    return _scale_stylesheet_metrics(stylesheet, ui_scale)


def _readable_typography_styles(profile_key: str, preferences: AppearancePreferences) -> str:
    """Use one verified UI face and strengthen navigation without bolding code."""
    muted = {"studio": "#aab8c8", "paper": "#526277", "focus": "#bdbbd0"}[profile_key]
    size, weight = preferences.font_size, preferences.control_weight
    return f"""
* {{ font-family: "{ui_font_family(preferred=preferences.font_family)}"; font-size: {size}pt; font-weight: {preferences.font_weight}; }}
QPushButton, QToolButton, QComboBox, QTabBar,
QPushButton#Primary, QPushButton#GitPrimary, QToolButton#ThemeSelector,
QLabel#AppTitle, QLabel#WindowTitle, QLabel#SectionTitle, QLabel#WelcomeTitle,
QLabel#TaskPlanTitle, QLabel#TaskPlanProgress, QLabel#SummarySettingsTitle,
QLabel#StateReady, QLabel#StateWorking, QLabel#StateError {{ font-weight: {weight}; }}
QLabel#AppTitle, QLabel#WindowTitle, QLabel#TaskPlanTitle,
QLabel#BubbleText, QFrame#SummarySettings QLabel#SummarySettingsTitle {{ font-size: {size}pt; }}
QLabel#Overline {{ color: {muted}; font-size: {max(10, size - 1)}pt; font-weight: {weight}; }}
QLabel#Subtle, QLabel#Hint, QLabel#TaskPlanEmpty {{ font-weight: {max(500, preferences.font_weight)}; }}
QFrame#WelcomeCard QLabel#Subtle, QFrame#WelcomeCard QLabel#Hint {{ color: {muted}; }}
QTreeView {{ font-size: {size}pt; }}
QPlainTextEdit#CodeEditor {{ font-family: "{editor_font_family(preferences.editor_font_family)}"; font-size: {preferences.editor_font_size}pt; font-weight: 400; }}
QLabel#PreferencesTitle {{ font-size: 20pt; font-weight: {weight}; }}
QWidget#PreferencesPage[compact="true"] QLabel#PreferencesTitle {{ font-size: 16pt; }}
QLabel#PreferenceName {{ font-weight: {weight}; }}
QLabel#PreferenceDescription {{ color: {muted}; }}
QFrame#WelcomeCard[compact="true"] QLabel#WelcomeTitle {{ font-size: 18pt; }}
QFrame#WelcomeCard[compact="true"] QLabel#WelcomeShortcut {{ font-size: 10pt; }}
"""


def accent_text_color(color: str) -> str:
    """Choose the higher-contrast black/white label for a custom accent."""
    rgb = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in rgb]
    luminance = sum(channel * factor for channel, factor in zip(linear, (0.2126, 0.7152, 0.0722)))
    return "#ffffff" if luminance <= 0.179 else "#10151b"


def _custom_appearance_styles(preferences: AppearancePreferences) -> str:
    styles = ""
    if preferences.density == "compact":
        styles += "QPushButton { padding: 5px 8px; } QTreeView::item { padding: 3px 5px; }\n"
    if preferences.accent_color:
        accent = preferences.accent_color
        foreground = accent_text_color(accent)
        styles += f"""
QPushButton#Primary, QPushButton#Primary:hover {{ background: {accent}; color: {foreground}; border-color: {accent}; }}
QTabBar::tab:selected, QToolButton#ActivityButton:checked {{ border-color: {accent}; }}
"""
    return styles


def _complete_control_styles(profile_key: str) -> str:
    """Keep native form/pop-up colors consistent, with explicit specificity."""
    background, foreground, border, muted, accent, panel = {
        "studio": ("#18212b", "#dce4ed", "#334353", "#97a8ba", "#75b6ff", "#11171e"),
        "paper": ("#ffffff", "#263346", "#d4dde8", "#65758a", "#3278df", "#f7f9fc"),
        "focus": ("#191925", "#eeedf5", "#46445a", "#aaa8bc", "#a58cff", "#12141d"),
    }[profile_key]
    progress_background = {
        "studio": "#293643",
        "paper": "#e5ebf2",
        "focus": "#302e40",
    }[profile_key]
    arrow_root = (Path(__file__).resolve().parent / "ui" / "icons").as_posix()
    return f"""
QFrame#SummarySettings QLabel#SummarySettingsTitle {{ color: {foreground}; font-size: 11pt; }}
QFrame#SummarySettings QLabel#SummaryState {{ color: {accent}; }}
QFrame#SummarySettings QLabel#Hint {{ color: {muted}; }}
QScrollArea#SettingsScroll, QScrollArea#SettingsScroll > QWidget {{ background: {panel}; border: none; }}
QScrollArea#TaskPlanScroll, QScrollArea#TaskPlanScroll > QWidget,
QWidget#TaskPlanBody, QScrollArea#WelcomeScroll, QScrollArea#WelcomeScroll > QWidget,
QWidget#WelcomeSurface {{ background: {panel}; border: none; }}
QComboBox {{
    color: {foreground}; background: {background}; border: 1px solid {border};
    border-radius: 6px; min-height: 28px; padding: 3px 26px 3px 8px;
}}
QComboBox:focus {{ border-color: {accent}; }}
QComboBox::drop-down {{ width: 22px; border: none; background: transparent; }}
QComboBox::down-arrow {{ image: url("{arrow_root}/chevron-down.svg"); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: {background}; color: {foreground}; border: 1px solid {border};
    selection-background-color: {accent}; selection-color: #ffffff;
    padding: 4px; outline: none;
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {muted}; border-color: {border}; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 18px; border: none; background: transparent; }}
QSpinBox::up-arrow {{ image: url("{arrow_root}/chevron-up.svg"); width: 10px; height: 10px; }}
QSpinBox::down-arrow {{ image: url("{arrow_root}/chevron-down.svg"); width: 10px; height: 10px; }}
QSplitter#Workbench::handle {{ background: transparent; border-left: 1px solid {border}; }}
QSplitter#Workbench::handle:hover {{ background: transparent; border-left-color: {accent}; }}
QLabel#Chip {{ padding: 3px 5px; }}
QLabel#ContextUsageLabel {{ color: {muted}; font-size: 9pt; font-weight: 600; }}
QProgressBar#ContextUsageBar {{ background: {progress_background}; border: none; border-radius: 3px; }}
QProgressBar#ContextUsageBar::chunk {{ background: {accent}; border-radius: 3px; }}
QProgressBar#ContextUsageBar[level="warning"]::chunk {{ background: #d89c32; }}
QProgressBar#ContextUsageBar[level="critical"]::chunk {{ background: #d76567; }}
QFrame#ExecutionGroup {{ background: {panel}; border: 1px solid {border}; border-radius: 8px; }}
QToolButton#ExecutionToggle {{ color: {foreground}; border: none; padding: 5px 4px; font-weight: 600; }}
QToolButton#ExecutionToggle:hover {{ color: {accent}; background: transparent; }}
QLabel#ExecutionStatus, QLabel#ExecutionCount {{ color: {muted}; }}
QLabel#ExecutionCount {{ font-size: 9pt; }}
QLabel#ExecutionStatus[state="running"] {{ color: {accent}; }}
QLabel#ExecutionStatus[state="waiting"] {{ color: #d89c32; }}
QLabel#ExecutionStatus[state="completed"] {{ color: #31a46c; }}
QLabel#ExecutionStatus[state="failed"] {{ color: #d76567; }}
QWidget#ExecutionBody {{ background: transparent; border-top: 1px solid {border}; }}
QFrame#WelcomeCard {{ background: transparent; border: none; }}
QLabel#WelcomeTitle {{ color: {foreground}; font-size: 24pt; font-weight: 600; }}
QFrame#WelcomeCard QLabel#Subtle, QFrame#WelcomeCard QLabel#Hint {{ color: {muted}; }}
QFrame#WelcomeCard QPushButton {{ min-height: 30px; text-align: left; }}
QLabel#WelcomeShortcut {{ color: {muted}; }}
QWidget#PreferencesPage, QWidget#PreferencesSurface,
QScrollArea#PreferencesScroll, QScrollArea#PreferencesScroll > QWidget {{ background: {panel}; border: none; }}
QListWidget#PreferencesCategories {{ background: {panel}; color: {foreground}; border: none; outline: none; }}
QListWidget#PreferencesCategories::item {{ padding: 10px 8px; border-radius: 5px; }}
QListWidget#PreferencesCategories::item:selected {{ background: {background}; color: {accent}; }}
QFrame#PreferenceCard {{ background: {background}; border: 1px solid {border}; border-radius: 8px; }}
QLabel#PreferencesTitle, QLabel#PreferenceName {{ color: {foreground}; }}
QLineEdit[invalid="true"] {{ border-color: #c64444; }}
"""
