namespace SciDev.Infrastructure;

public sealed partial class TerminalScreen
{
    internal void Command(char command, string parameters, Action<string>? reply)
    {
        var screen = Current;
        var privateMode = parameters.StartsWith('?');
        var softReset = parameters == "!" && command == 'p';
        if (softReset)
        {
            ResetModes();
            (screen.Top, screen.Bottom) = (0, _rows - 1);
            return;
        }

        Span<int> values = stackalloc int[64];
        var count = 0;
        var number = 0;
        foreach (var ch in parameters.AsSpan(privateMode ? 1 : 0))
        {
            if (ch is >= '0' and <= '9')
                number = Math.Min(32_767, number * 10 + ch - '0');
            else if (ch == ';' && count < values.Length - 1)
            {
                values[count++] = number;
                number = 0;
            }
            else
                return;
        }
        values[count++] = number;
        var first = values[0];
        var one = Math.Max(first, 1);
        var second = count > 1 ? values[1] : 0;
        if (command is 'h' or 'l')
        {
            Configure(values[..count], privateMode, command == 'h');
            return;
        }
        if (privateMode)
            return;

        switch (command)
        {
            case 'A': Move(screen.Row - one, screen.Column, InScrollingRegion()); break;
            case 'B' or 'e': Move(screen.Row + one, screen.Column, InScrollingRegion()); break;
            case 'C' or 'a': Move(screen.Row, screen.Column + one); break;
            case 'D': Move(screen.Row, screen.Column - one); break;
            case 'E': Move(screen.Row + one, 0, InScrollingRegion()); break;
            case 'F': Move(screen.Row - one, 0, InScrollingRegion()); break;
            case 'G': Move(screen.Row, one - 1); break;
            case 'd': Move(one - 1 + (_origin ? screen.Top : 0), screen.Column, _origin); break;
            case 'H' or 'f': Move(one - 1 + (_origin ? screen.Top : 0), Math.Max(second, 1) - 1, _origin); break;
            case 'I': Move(screen.Row, (screen.Column / 8 + one) * 8); break;
            case 'Z': Move(screen.Row, ((screen.Column + 7) / 8 - one) * 8); break;
            case 'J': EraseDisplay(first); break;
            case 'K': EraseLine(first); break;
            case 'X': EraseCells(screen.Row, screen.Column, Math.Min(_columns - 1, screen.Column + one - 1)); break;
            case '@': ShiftCells(insert: true, one); break;
            case 'P': ShiftCells(insert: false, one); break;
            case 'L' or 'M' when InScrollingRegion():
                Scroll(command == 'M', screen.Row, screen.Bottom, one, record: false);
                Control('\r');
                break;
            case 'S' or 'T': Scroll(command == 'S', screen.Top, screen.Bottom, one); break;
            case 'r':
                var top = one - 1;
                var bottom = second == 0 ? _rows - 1 : Math.Min(_rows - 1, second - 1);
                if (top < bottom)
                {
                    (screen.Top, screen.Bottom) = (top, bottom);
                    Move(_origin ? top : 0, 0);
                }
                break;
            case 's': SaveCursor(); break;
            case 'u': RestoreCursor(); break;
            case 'm': SetGraphics(values[..count]); break;
            case 'n' when first == 5: reply?.Invoke("\u001b[0n"); break;
            case 'n' when first == 6:
                reply?.Invoke(FormattableString.Invariant($"\u001b[{screen.Row + 1 - (_origin ? screen.Top : 0)};{screen.Column + 1}R"));
                break;
            case 'c' when first == 0: reply?.Invoke("\u001b[?1;0c"); break;
        }
    }

    private bool InScrollingRegion() => Current.Row >= Current.Top && Current.Row <= Current.Bottom;

    private void Configure(ReadOnlySpan<int> modes, bool privateMode, bool enabled)
    {
        foreach (var mode in modes)
        {
            if (!privateMode)
            {
                if (mode == 4)
                    _insert = enabled;
                continue;
            }
            switch (mode)
            {
                case 1: _modes = _modes with { ApplicationCursor = enabled }; break;
                case 6: _origin = enabled; Move(enabled ? Current.Top : 0, 0); break;
                case 7: _autoWrap = enabled; Current.WrapPending = false; break;
                case 25: _cursorVisible = enabled; break;
                case 47 or 1047 or 1049: SwitchScreen(enabled, mode); break;
                case 1048: if (enabled) SaveCursor(); else RestoreCursor(); break;
                case 9 or 1000 or 1002 or 1003:
                    if (enabled || _modes.MouseMode == mode)
                        _modes = _modes with { MouseMode = enabled ? mode : 0 };
                    break;
                case 1006: _modes = _modes with { SgrMouse = enabled }; break;
                case 2004: _modes = _modes with { BracketedPaste = enabled }; break;
            }
        }
    }

    private void SwitchScreen(bool alternate, int mode)
    {
        if (alternate == _isAlternate)
            return;
        if (alternate)
        {
            if (mode == 1049)
                SaveCursor();
            if (mode != 47)
                _alternate = new(_columns, _rows);
        }
        _isAlternate = alternate;
        if (!alternate && mode == 1049)
            RestoreCursor();
    }

    private void EraseDisplay(int mode)
    {
        var screen = Current;
        switch (mode)
        {
            case 0:
                EraseLine(0);
                for (var row = screen.Row + 1; row < _rows; row++)
                    Array.Fill(screen.Lines[row], Blank);
                break;
            case 1:
                for (var row = 0; row < screen.Row; row++)
                    Array.Fill(screen.Lines[row], Blank);
                EraseLine(1);
                break;
            case 2:
                foreach (var line in screen.Lines)
                    Array.Fill(line, Blank);
                break;
            case 3 when !_isAlternate:
                _history.Clear();
                _historyCells = 0;
                break;
        }
    }

    private void EraseLine(int mode)
    {
        var screen = Current;
        switch (mode)
        {
            case 0: EraseCells(screen.Row, screen.Column, _columns - 1); break;
            case 1: EraseCells(screen.Row, 0, screen.Column); break;
            case 2: Array.Fill(screen.Lines[screen.Row], Blank); break;
        }
    }

    private void EraseCells(int row, int start, int end)
    {
        var line = Current.Lines[row];
        var blank = Blank;
        ClearGlyph(line, start, blank);
        ClearGlyph(line, end, blank);
        Array.Fill(line, blank, start, end - start + 1);
    }

    private void ShiftCells(bool insert, int count)
    {
        var screen = Current;
        var line = screen.Lines[screen.Row];
        var column = screen.Column;
        count = Math.Min(count, _columns - column);
        var start = insert ? column : column + count;
        var target = insert ? column + count : column;
        Array.Copy(line, start, line, target, _columns - column - count);
        Array.Fill(line, Blank, insert ? column : _columns - count, count);
        RepairWideCells(line);
    }

    private void SetGraphics(ReadOnlySpan<int> values)
    {
        for (var i = 0; i < values.Length; i++)
        {
            var value = values[i];
            _style = value switch
            {
                0 => default,
                1 => _style with { Bold = true },
                4 or 21 => _style with { Underline = true },
                7 => _style with { Inverse = true },
                8 => _style with { Hidden = true },
                22 => _style with { Bold = false },
                24 => _style with { Underline = false },
                27 => _style with { Inverse = false },
                28 => _style with { Hidden = false },
                >= 30 and <= 37 => _style with { Foreground = -(value - 30 + 1) },
                >= 40 and <= 47 => _style with { Background = -(value - 40 + 1) },
                >= 90 and <= 97 => _style with { Foreground = -(value - 90 + 9) },
                >= 100 and <= 107 => _style with { Background = -(value - 100 + 9) },
                39 => _style with { Foreground = null },
                49 => _style with { Background = null },
                _ => _style,
            };
            if (value is not (38 or 48) || i + 1 == values.Length)
                continue;
            int? color = null;
            if (values[i + 1] == 5 && i + 2 < values.Length)
            {
                color = -Math.Clamp(values[i + 2], 0, 255) - 1;
                i += 2;
            }
            else if (values[i + 1] == 2 && i + 4 < values.Length)
            {
                color = (Byte(values[i + 2]) << 16) | (Byte(values[i + 3]) << 8) | Byte(values[i + 4]);
                i += 4;
            }
            else
                break;
            _style = value == 38 ? _style with { Foreground = color } : _style with { Background = color };
        }
    }

    private static int Byte(int value) => Math.Min(value, 255);

    private void ResetModes()
    {
        _style = default;
        _modes = default;
        _cursorVisible = _autoWrap = true;
        _origin = _insert = false;
        Current.WrapPending = false;
    }

    private void Reset()
    {
        _main = new(_columns, _rows);
        _alternate = new(_columns, _rows);
        _isAlternate = false;
        ResetModes();
    }
}
