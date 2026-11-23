using System.Text;

namespace SciDev.Infrastructure;

// Retains the printable transcript while discarding terminal control sequences.
internal sealed class TerminalText
{
    private enum State { Text, Escape, EscapeIntermediate, Csi, ControlString, ControlEscape }
    private State _state;

    public string Read(ReadOnlySpan<char> input)
    {
        var output = new StringBuilder(input.Length);
        foreach (var ch in input)
        {
            switch (_state)
            {
                case State.Text:
                    if (ch == '\x1b')
                        _state = State.Escape;
                    else if (ch == '\u009b')
                        _state = State.Csi;
                    else if (ch is '\u0090' or '\u009d' or '\u009e' or '\u009f')
                        _state = State.ControlString;
                    else if (!char.IsControl(ch) || ch is '\n' or '\t')
                        output.Append(ch);
                    break;
                case State.Escape:
                    _state = ch switch
                    {
                        '[' => State.Csi,
                        ']' or 'P' or 'X' or '^' or '_' => State.ControlString,
                        >= ' ' and <= '/' => State.EscapeIntermediate,
                        '\x1b' => State.Escape,
                        _ => State.Text,
                    };
                    break;
                case State.Csi:
                    if (ch is >= '@' and <= '~' or '\x18' or '\x1a')
                        _state = State.Text;
                    else if (ch == '\x1b')
                        _state = State.Escape;
                    break;
                case State.EscapeIntermediate:
                    if (ch >= '0')
                        _state = State.Text;
                    break;
                case State.ControlString:
                    if (ch is '\a' or '\u009c' or '\x18' or '\x1a')
                        _state = State.Text;
                    else if (ch == '\x1b')
                        _state = State.ControlEscape;
                    break;
                case State.ControlEscape:
                    _state = ch == '\\' ? State.Text : State.ControlString;
                    break;
            }
        }
        return output.ToString();
    }
}
