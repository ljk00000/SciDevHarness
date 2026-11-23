using System.Text;
using System.Windows.Input;
using SciDev.Infrastructure;

namespace SciDev.Desktop;

internal static class TerminalKeys
{
    internal static string? Encode(Key key, ModifierKeys modifiers, TerminalScreen.InputModes modes)
    {
        var shift = modifiers.HasFlag(ModifierKeys.Shift);
        var alt = modifiers.HasFlag(ModifierKeys.Alt);
        var control = modifiers.HasFlag(ModifierKeys.Control);
        var modifier = 1 + (shift ? 1 : 0) + (alt ? 2 : 0) + (control ? 4 : 0);
        var cursor = key switch
        {
            Key.Up => "A", Key.Down => "B", Key.Right => "C", Key.Left => "D", Key.Home => "H", Key.End => "F",
            _ => null,
        };
        if (cursor is not null)
            return modifier > 1 ? $"\u001b[1;{modifier}{cursor}" : (modes.ApplicationCursor ? "\u001bO" : "\u001b[") + cursor;
        if (modes.ApplicationKeypad && modifier == 1 && Keypad(key) is { } keypad)
            return "\u001bO" + keypad;
        if (key is >= Key.F1 and <= Key.F4)
        {
            var function = (char)('P' + (key - Key.F1));
            return modifier > 1 ? $"\u001b[1;{modifier}{function}" : "\u001bO" + function;
        }

        var tilde = key switch
        {
            Key.Insert => 2, Key.Delete => 3, Key.PageUp => 5, Key.PageDown => 6,
            Key.F5 => 15, Key.F6 => 17, Key.F7 => 18, Key.F8 => 19, Key.F9 => 20, Key.F10 => 21,
            Key.F11 => 23, Key.F12 => 24, _ => 0,
        };
        if (tilde != 0)
            return modifier > 1 ? $"\u001b[{tilde};{modifier}~" : $"\u001b[{tilde}~";
        if (control && !alt && key is >= Key.A and <= Key.Z)
            return ((char)(key - Key.A + 1)).ToString();
        if (alt && !control && key is >= Key.A and <= Key.Z)
            return "\u001b" + (char)((shift ? 'A' : 'a') + key - Key.A);
        if (control && !alt && key == Key.Space)
            return "\0";
        var simple = key switch
        {
            Key.Enter => "\r", Key.Escape => "\u001b", Key.Back => control ? "\b" : "\u007f",
            Key.Tab => shift ? "\u001b[Z" : "\t", _ => null,
        };
        return alt && simple is not null ? "\u001b" + simple : simple;
    }

    internal static string Paste(string value, bool bracketed)
    {
        var text = new StringBuilder(Math.Min(value.Length, 16_384));
        foreach (var rune in value.EnumerateRunes())
        {
            if (text.Length + rune.Utf16SequenceLength > 16_384)
                break;
            if (rune.Value is '\t' or '\r' or '\n' || !Rune.IsControl(rune))
                text.Append(rune.ToString());
        }
        var clean = text.ToString().Replace("\r\n", "\r").Replace('\n', '\r');
        return bracketed && clean.Length > 0 ? "\u001b[200~" + clean + "\u001b[201~" : clean;
    }

    private static char? Keypad(Key key) => key switch
    {
        >= Key.NumPad0 and <= Key.NumPad9 => (char)('p' + (key - Key.NumPad0)),
        Key.Decimal => 'n', Key.Divide => 'o', Key.Multiply => 'j', Key.Subtract => 'm', Key.Add => 'k',
        _ => null,
    };
}
