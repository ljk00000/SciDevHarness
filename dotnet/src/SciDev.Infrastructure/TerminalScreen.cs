using System.Globalization;
using System.Text;

namespace SciDev.Infrastructure;

/// <summary>VT viewport and bounded scrollback for one ConPTY session.</summary>
public sealed partial class TerminalScreen
{
    public readonly record struct Style(int? Foreground = null, int? Background = null, bool Bold = false,
        bool Underline = false, bool Inverse = false, bool Hidden = false);
    public readonly record struct Cell(string? Text, Style Style, byte Width = 1);

    public sealed record Frame(int Columns, int Rows, IReadOnlyList<ReadOnlyMemory<Cell>> Lines,
        int CursorColumn, int CursorRow, bool CursorVisible, int ScrollOffset, int Scrollback,
        bool Alternate, long Revision);

    public readonly record struct InputModes(bool ApplicationCursor, bool BracketedPaste, int MouseMode, bool SgrMouse,
        bool ApplicationKeypad = false);

    private sealed class Buffer(int columns, int rows)
    {
        public Cell[][] Lines = NewLines(columns, rows);
        public int Column, Row, Top, Bottom = rows - 1;
        public bool WrapPending;
        public (int Column, int Row, Style Style) Saved;
    }

    private const int HistoryCellLimit = 160_000;
    private readonly object _gate = new();
    private readonly TerminalSequences _parser = new();
    private readonly Queue<Cell[]> _history = new();
    private Buffer _main;
    private Buffer _alternate;
    private Buffer Current => _isAlternate ? _alternate : _main;
    private Style _style;
    private int _columns, _rows, _historyCells;
    private bool _isAlternate, _cursorVisible = true, _autoWrap = true, _origin, _insert;
    private InputModes _modes;
    private long _revision;

    public TerminalScreen(int columns, int rows)
    {
        (_columns, _rows) = Dimensions(columns, rows);
        _main = new(_columns, _rows);
        _alternate = new(_columns, _rows);
    }

    public static (int Columns, int Rows) Dimensions(int columns, int rows) =>
        (Math.Clamp(columns, 20, 500), Math.Clamp(rows, 4, 200));

    public InputModes Modes { get { lock (_gate) return _modes; } }

    public void Read(string value, Action<string>? respond = null)
    {
        lock (_gate)
        {
            _parser.Read(value, this, respond);
            _revision++;
        }
    }

    public void Resize(int columns, int rows)
    {
        (columns, rows) = Dimensions(columns, rows);
        lock (_gate)
        {
            if (_columns == columns && _rows == rows)
                return;
            ResizeBuffer(_main, columns, rows, saveHistory: true);
            ResizeBuffer(_alternate, columns, rows, saveHistory: false);
            (_columns, _rows) = (columns, rows);
            _revision++;
        }
    }

    public Frame? Capture(long revision, int scrollOffset)
    {
        lock (_gate)
        {
            if (revision == _revision)
                return null;
            var screen = Current;
            var history = _isAlternate ? 0 : _history.Count;
            var offset = Math.Clamp(scrollOffset, 0, history);
            var lines = new ReadOnlyMemory<Cell>[_rows];
            var past = offset > 0 ? _history.ToArray() : null;
            for (var row = 0; row < _rows; row++)
            {
                var index = row + history - offset;
                lines[row] = index < history ? past![index].AsMemory(0, Math.Min(_columns, past[index].Length))
                    : screen.Lines[index - history].ToArray();
            }
            return new(_columns, _rows, lines, screen.Column, screen.Row, _cursorVisible && offset == 0,
                offset, history, _isAlternate, _revision);
        }
    }

    internal void Print(Rune value)
    {
        var screen = Current;
        var width = CellWidth(value);
        if (width == 0)
        {
            var row = screen.Row;
            var index = screen.WrapPending ? screen.Column : screen.Column - 1;
            if (index < 0 && row > 0)
            {
                index = _columns - 1;
                row--;
            }
            if (index >= 0)
            {
                var line = screen.Lines[row];
                if (line[index].Width == 0 && index > 0 && line[index - 1].Width == 2)
                    index--;
                if (line[index].Text is { Length: < 24 } text)
                    line[index] = line[index] with { Text = text + value };
            }
            return;
        }

        if (screen.WrapPending || width > _columns - screen.Column)
        {
            if (_autoWrap)
            {
                screen.Column = 0;
                Index();
            }
            else if (width > _columns - screen.Column)
                return;
            screen.WrapPending = false;
        }

        var cells = screen.Lines[screen.Row];
        if (_insert)
            ShiftCells(insert: true, width);
        ClearGlyph(cells, screen.Column, Blank);
        if (width == 2)
            ClearGlyph(cells, screen.Column + 1, Blank);
        cells[screen.Column] = new(value.ToString(), _style, width);
        if (width == 2)
            cells[screen.Column + 1] = new(null, _style, 0);
        screen.WrapPending = screen.Column + width == _columns && _autoWrap;
        screen.Column = Math.Min(_columns - 1, screen.Column + width);
    }

    internal void Control(char value)
    {
        var screen = Current;
        switch (value)
        {
            case '\r': Move(screen.Row, 0); break;
            case '\n' or '\v' or '\f': Index(); break;
            case '\b': Move(screen.Row, screen.Column - 1); break;
            case '\t': Move(screen.Row, (screen.Column / 8 + 1) * 8); break;
        }
    }

    internal void Escape(char value)
    {
        switch (value)
        {
            case '7': SaveCursor(); break;
            case '8': RestoreCursor(); break;
            case 'D': Index(); break;
            case 'E': Control('\r'); Index(); break;
            case 'M': Index(reverse: true); break;
            case '=' or '>': _modes = _modes with { ApplicationKeypad = value == '=' }; break;
            case 'c': Reset(); break;
        }
    }

    private void Index(bool reverse = false)
    {
        var screen = Current;
        screen.WrapPending = false;
        if (reverse && screen.Row == screen.Top)
            Scroll(scrollUp: false, screen.Top, screen.Bottom, 1);
        else if (!reverse && screen.Row == screen.Bottom)
            Scroll(scrollUp: true, screen.Top, screen.Bottom, 1);
        else
            screen.Row = Math.Clamp(screen.Row + (reverse ? -1 : 1), 0, _rows - 1);
    }

    private void Scroll(bool scrollUp, int top, int bottom, int count, bool record = true)
    {
        var lines = Current.Lines;
        var height = bottom - top + 1;
        count = Math.Min(count, height);
        if (scrollUp)
        {
            if (record && !_isAlternate && top == 0 && bottom == _rows - 1)
                for (var index = top; index < top + count; index++)
                    AddHistory(lines[index]);
            Array.Copy(lines, top + count, lines, top, height - count);
        }
        else
            Array.Copy(lines, top, lines, top + count, height - count);
        var start = scrollUp ? bottom - count + 1 : top;
        for (var index = start; index < start + count; index++)
            lines[index] = NewLine(_columns, Blank);
    }

    private void AddHistory(Cell[] line)
    {
        _history.Enqueue(line);
        _historyCells += line.Length;
        while (_historyCells > HistoryCellLimit || _history.Count > 2000)
            _historyCells -= _history.Dequeue().Length;
    }

    private void ResizeBuffer(Buffer buffer, int columns, int rows, bool saveHistory)
    {
        var skip = Math.Max(0, buffer.Row - rows + 1);
        if (saveHistory)
            for (var row = 0; row < skip; row++)
                AddHistory(buffer.Lines[row]);
        var next = NewLines(columns, rows);
        for (var row = 0; row < rows && row + skip < _rows; row++)
        {
            Array.Copy(buffer.Lines[row + skip], next[row], Math.Min(columns, _columns));
            RepairWideCells(next[row]);
        }
        buffer.Lines = next;
        buffer.Row = Math.Clamp(buffer.Row - skip, 0, rows - 1);
        buffer.Column = Math.Min(buffer.Column, columns - 1);
        (buffer.Top, buffer.Bottom, buffer.WrapPending) = (0, rows - 1, false);
    }

    private void Move(int row, int column, bool boundedByMargins = false)
    {
        var screen = Current;
        screen.Row = boundedByMargins ? Math.Clamp(row, screen.Top, screen.Bottom) : Math.Clamp(row, 0, _rows - 1);
        screen.Column = Math.Clamp(column, 0, _columns - 1);
        screen.WrapPending = false;
    }

    private void SaveCursor() => Current.Saved = (Current.Column, Current.Row, _style);

    private void RestoreCursor()
    {
        var saved = Current.Saved;
        Move(saved.Row, saved.Column);
        _style = saved.Style;
    }

    private Cell Blank => new(null, new(Background: _style.Inverse ? _style.Foreground : _style.Background));

    private static Cell[][] NewLines(int columns, int rows) =>
        Enumerable.Range(0, rows).Select(_ => NewLine(columns, new(null, default))).ToArray();

    private static Cell[] NewLine(int columns, Cell cell)
    {
        var line = new Cell[columns];
        Array.Fill(line, cell);
        return line;
    }

    private static void ClearGlyph(Cell[] line, int column, Cell blank)
    {
        if (line[column].Width == 2 && column + 1 < line.Length)
            line[column + 1] = blank;
        if (line[column].Width == 0 && column > 0 && line[column - 1].Width == 2)
            line[column - 1] = blank;
        line[column] = blank;
    }

    private static void RepairWideCells(Cell[] line)
    {
        for (var column = 0; column < line.Length; column++)
        {
            var width = line[column].Width;
            if ((width == 2 && (column == line.Length - 1 || line[column + 1].Width != 0)) ||
                (width == 0 && (column == 0 || line[column - 1].Width != 2)))
                line[column] = new(null, line[column].Style);
        }
    }

    private static byte CellWidth(Rune rune)
    {
        var value = rune.Value;
        if (value < 0x7f)
            return 1;
        if (Rune.GetUnicodeCategory(rune) is UnicodeCategory.NonSpacingMark or UnicodeCategory.EnclosingMark or UnicodeCategory.Format)
            return 0;
        return value is >= 0x1100 and <= 0x115f or >= 0x2329 and <= 0x232a or >= 0x2e80 and <= 0xa4cf
            or >= 0xac00 and <= 0xd7a3 or >= 0xf900 and <= 0xfaff or >= 0xfe10 and <= 0xfe19
            or >= 0xfe30 and <= 0xfe6f or >= 0xff00 and <= 0xff60 or >= 0xffe0 and <= 0xffe6
            or >= 0x1f300 and <= 0x1faff or >= 0x20000 and <= 0x3fffd ? (byte)2 : (byte)1;
    }
}
