using System.Text;

namespace SciDev.Infrastructure;

internal sealed class TerminalSequences
{
    private enum State { Text, Escape, EscapeIntermediate, Charset, Csi, IgnoreCsi, String, StringEscape }
    private readonly StringBuilder _parameters = new(32);
    private State _state;
    private char _highSurrogate;
    private bool _lineDrawing;

    public void Read(ReadOnlySpan<char> source, TerminalScreen screen, Action<string>? reply)
    {
        foreach (var ch in source)
        {
            if (_highSurrogate != '\0')
            {
                var high = _highSurrogate;
                _highSurrogate = '\0';
                if (Rune.TryCreate(high, ch, out var pair))
                {
                    screen.Print(pair);
                    continue;
                }
                screen.Print(Rune.ReplacementChar);
            }

            if (ch is '\x18' or '\x1a')
            {
                _state = State.Text;
                continue;
            }
            switch (_state)
            {
                case State.Text:
                    ReadText(ch, screen);
                    break;
                case State.Escape:
                    ReadEscape(ch, screen);
                    break;
                case State.Charset:
                    _lineDrawing = ch == '0';
                    _state = State.Text;
                    break;
                case State.EscapeIntermediate:
                    if (ch == '\x1b')
                        _state = State.Escape;
                    else if (ch >= '0')
                        _state = State.Text;
                    break;
                case State.Csi:
                    if (ch is >= '@' and <= '~')
                    {
                        _state = State.Text;
                        screen.Command(ch, _parameters.ToString(), reply);
                    }
                    else if (ch == '\x1b')
                        _state = State.Escape;
                    else if (ch < ' ')
                        screen.Control(ch);
                    else if (_parameters.Length == 128)
                        _state = State.IgnoreCsi;
                    else
                        _parameters.Append(ch);
                    break;
                case State.IgnoreCsi:
                    if (ch == '\x1b')
                        _state = State.Escape;
                    else if (ch is >= '@' and <= '~')
                        _state = State.Text;
                    break;
                case State.String:
                    if (ch is '\a' or '\u009c')
                        _state = State.Text;
                    else if (ch == '\x1b')
                        _state = State.StringEscape;
                    break;
                case State.StringEscape:
                    _state = ch == '\\' ? State.Text : State.String;
                    break;
            }
        }
    }

    private void ReadText(char ch, TerminalScreen screen)
    {
        switch (ch)
        {
            case '\x1b': _state = State.Escape; return;
            case '\u009b': BeginCsi(); return;
            case '\u0090' or '\u0098' or '\u009d' or '\u009e' or '\u009f': _state = State.String; return;
        }
        if (char.IsControl(ch))
            screen.Control(ch);
        else if (char.IsHighSurrogate(ch))
            _highSurrogate = ch;
        else if (char.IsLowSurrogate(ch))
            screen.Print(Rune.ReplacementChar);
        else
            screen.Print(new Rune(_lineDrawing && ch is >= (char)0x60 and <= '~'
                ? "◆▒␉␌␍␊°±␤␋┘┐┌└┼⎺⎻─⎼⎽├┤┴┬│≤≥π≠£·"[ch - 0x60] : ch));
    }

    private void ReadEscape(char ch, TerminalScreen screen)
    {
        _state = ch switch
        {
            '[' => State.Csi,
            ']' or 'P' or 'X' or '^' or '_' => State.String,
            '(' or ')' => State.Charset,
            >= ' ' and <= '/' => State.EscapeIntermediate,
            '\x1b' => State.Escape,
            _ => State.Text,
        };
        if (ch == '[')
            BeginCsi();
        else if (_state == State.Text)
        {
            if (ch == 'c')
                _lineDrawing = false;
            screen.Escape(ch);
        }
    }

    private void BeginCsi()
    {
        _parameters.Clear();
        _state = State.Csi;
    }
}
