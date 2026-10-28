"""Dependency-free, file-aware highlighting for the desktop editor."""

from __future__ import annotations
import re
from pathlib import Path
from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat
from scidev_ui import DEFAULT_UI_PROFILE, ui_profile_for_key


class PythonHighlighter(QSyntaxHighlighter):
    _PATTERNS = (
        r"\b(and|as|assert|async|await|break|case|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|match|not|or|pass|raise|return|try|while|with|yield)\b",
        r"\b(True|False|None|self)\b", r"\b\d+(\.\d+)?\b", r"#[^\n]*",
        r'''"[^"\n]*"|'[^'\n]*' ''',
    )

    def __init__(self, document, profile_key=DEFAULT_UI_PROFILE):
        super().__init__(document)
        self.rules = []
        self.profile_key = DEFAULT_UI_PROFILE
        self.set_ui_profile(profile_key)

    def set_ui_profile(self, profile_key):
        profile = ui_profile_for_key(profile_key)
        self.profile_key = profile.key
        self.rules = []
        for index, (pattern, color) in enumerate(zip(self._PATTERNS, profile.syntax_colors, strict=True)):
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if index == 0:
                fmt.setFontWeight(QFont.Weight.Bold)
            self.rules.append((QRegularExpression(pattern.strip()), fmt))
        self.rehighlight()

    def highlightBlock(self, text):
        for expression, fmt in self.rules:
            matches = expression.globalMatch(text)
            while matches.hasNext():
                match = matches.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)


class LanguageHighlighter(PythonHighlighter):
    """Lexical coloring, including HTML's embedded CSS/JS and block comments.

    Not a parser or language server. Unknown file types stay plain; comment
    tokens are language-specific, so CSS IDs/colors are not Python comments.
    """
    LANGUAGES = {
        ".py": "python", ".pyw": "python",
        ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
        ".ts": "javascript", ".tsx": "javascript", ".cjs": "javascript",
        ".css": "css", ".scss": "css", ".html": "html", ".htm": "html",
        ".json": "json", ".md": "markdown", ".yaml": "yaml", ".yml": "yaml",
    }
    _CODE_TOKEN = re.compile(
        r'''(?P<comment>/\*|//[^\n]*)|(?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\x60(?:\\.|[^\x60\\])*\x60)'''
        r"|(?P<number>\b\d+(?:\.\d+)?(?:px|rem|em|vh|vw|ms|s|%)?\b)"
        r"|(?P<selector>[#.][\w-]+)|(?P<word>[$\w-]+)"
    )
    _PY_TOKEN = re.compile(
        r'''(?P<comment>\#.*)|(?P<string>"""|''' + "'''" +
        r'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(?P<number>\b\d+(?:\.\d+)?\b)|(?P<word>\b\w+\b)'''
    )
    _JS_KEYWORDS = frozenset(
        "const let var function return if else for while class extends import export "
        "from async await new try catch finally throw switch case break continue "
        "typeof instanceof delete void yield interface type public private readonly".split()
    )
    _PY_KEYWORDS = frozenset(
        "and as assert async await break case class continue def del elif else except "
        "finally for from global if import in is lambda match not or pass raise return "
        "try while with yield".split()
    )

    def __init__(self, document, profile_key=DEFAULT_UI_PROFILE, path: Path | None = None):
        self.language = self.LANGUAGES.get(path.suffix.lower(), "plain") if path else "plain"
        super().__init__(document, profile_key)

    def set_path(self, path: Path | None):
        language = self.LANGUAGES.get(path.suffix.lower(), "plain") if path else "plain"
        if language != self.language:
            self.language = language
            self.rehighlight()

    def _color(self, start, length, index):
        if length > 0:
            begin = self._utf16_offsets[start]
            end = self._utf16_offsets[start + length]
            self.setFormat(begin, end - begin, self.rules[index][1])

    def _code(self, text, language, offset=0, state=0):
        position = 0
        if state:
            terminator = "*/" if state == 1 else ('"""' if state == 2 else "'''")
            end = text.find(terminator)
            if end < 0:
                self._color(offset, len(text), 3 if state == 1 else 4)
                return state
            position = end + len(terminator)
            self._color(offset, position, 3 if state == 1 else 4)
        pattern = self._PY_TOKEN if language == "python" else self._CODE_TOKEN
        while position < len(text):
            match = pattern.search(text, position)
            if not match:
                break
            start, end = match.span()
            token, kind = match.group(), match.lastgroup
            index = None
            if kind == "comment" and language != "json":
                index = 3
                if token == "/*":
                    close = text.find("*/", end)
                    if close < 0:
                        self._color(offset + start, len(text) - start, 3)
                        return 1
                    end = close + 2
            elif kind == "string":
                index = 4
                if language == "python" and token in ('"""', "'''"):
                    close = text.find(token, end)
                    if close < 0:
                        self._color(offset + start, len(text) - start, 4)
                        return 2 if token == '"""' else 3
                    end = close + 3
            elif kind == "number":
                index = 2
            elif kind == "selector":
                index = 1
            elif kind == "word":
                if language == "python":
                    index = 0 if token in self._PY_KEYWORDS else 1 if token in {"True", "False", "None", "self"} else None
                elif language == "css":
                    index = 1 if re.match(r"\s*:", text[end:]) else 2 if token.startswith("--") else None
                else:
                    index = 0 if token in self._JS_KEYWORDS else 1 if token in {"true", "false", "null", "undefined", "this"} else None
            if index is not None:
                self._color(offset + start, end - start, index)
            position = end
        return 0

    def _html(self, text):
        state = max(0, self.previousBlockState())
        position = 0
        while position < len(text):
            if state == 30:
                close = text.find("-->", position)
                end = len(text) if close < 0 else close + 3
                self._color(position, end - position, 3)
                if close < 0:
                    break
                state, position = 0, end
            elif 10 <= state < 30:
                language = "css" if state < 20 else "javascript"
                base = 10 if language == "css" else 20
                closing = re.search(r"</\s*" + ("style" if language == "css" else "script") + r"\s*>", text[position:], re.I)
                end = position + closing.start() if closing else len(text)
                child_state = self._code(text[position:end], language, position, state - base)
                if not closing:
                    state = base + child_state
                    break
                self._color(end, closing.end() - closing.start(), 1)
                state, position = 0, position + closing.end()
            else:
                tag = re.search(r"<!--|</?[A-Za-z][^>]*>", text[position:])
                if not tag:
                    break
                start, end = position + tag.start(), position + tag.end()
                token = tag.group()
                if token == "<!--":
                    state, position = 30, start
                    continue
                self._color(start, end - start, 1)
                for attr in re.finditer(r"([\w:-]+)\s*=\s*(\"[^\"]*\"|'[^']*')", token):
                    self._color(start + attr.start(1), len(attr.group(1)), 0)
                    self._color(start + attr.start(2), len(attr.group(2)), 4)
                if re.match(r"<style\b", token, re.I):
                    state = 10
                elif re.match(r"<script\b", token, re.I):
                    state = 20
                position = end
        self.setCurrentBlockState(state)

    def highlightBlock(self, text):
        # QTextDocument positions are UTF-16 offsets, not Python code points.
        self._utf16_offsets = [0]
        for character in text:
            self._utf16_offsets.append(self._utf16_offsets[-1] + (2 if ord(character) > 0xFFFF else 1))
        self.setCurrentBlockState(0)
        if self.language == "html":
            self._html(text)
        elif self.language in {"python", "javascript", "css", "json"}:
            state = self._code(text, self.language, state=max(0, self.previousBlockState()))
            self.setCurrentBlockState(state)
        elif self.language == "markdown":
            if re.match(r"^\s*#{1,6}\s", text):
                self._color(0, len(text), 0)
            for match in re.finditer(r"\x60[^\x60]+\x60|\[[^\]]+\]\([^)]+\)", text):
                self._color(match.start(), len(match.group()), 4)
        elif self.language == "yaml":
            for match in re.finditer(r"#.*|^[\w .-]+(?=:)|\b(?:true|false|null|\d+)\b", text):
                self._color(match.start(), len(match.group()), 3 if match.group().startswith("#") else 1)
