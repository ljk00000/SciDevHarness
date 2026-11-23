using System.Text;
using System.Threading.Channels;
using SciDev.Infrastructure;

internal static class TerminalScreenChecks
{
    internal static async Task RunAsync(string workspace)
    {
        ScreenCommands();
        await ConPtyAsync(workspace);
    }

    private static void ScreenCommands()
    {
        var screen = new TerminalScreen(20, 5);
        var replies = new List<string>();
        screen.Read("draft---\rOK\u001b[0");
        screen.Read("K\u001b[2;3H\u001b[1;38;2;19;82;77m鹈\uD83D");
        screen.Read("\uDEB2e\u0301\u001b[0m\u001b[6n", replies.Add);
        var frame = Capture(screen);
        Assert(Line(frame, 0) == "OK", "carriage return and split line erase");
        Assert(Line(frame, 1) == "  鹈🚲e\u0301", "Chinese, split surrogate and combining character");
        var cells = frame.Lines[1].Span;
        Assert(cells[2] is { Width: 2, Style: { Foreground: 0x13524d, Bold: true } } && cells[3].Width == 0,
            "wide glyph and RGB styling occupy the intended cells");
        Assert(replies.SequenceEqual(["\u001b[2;8R"]), "cursor response reports terminal cell coordinates");
        Assert(screen.Capture(frame.Revision, 0) is null, "unchanged frames are not rebuilt");

        screen.Read("\u001b[?1049h\u001b[?1;1002;1006;2004h\u001b[?25lFULL\u001b[5;1HREDRAW\rDONE\u001b[K");
        var alternate = Capture(screen);
        Assert(alternate.Alternate && !alternate.CursorVisible && alternate.Scrollback == 0 &&
            Line(alternate, 0) == "FULL" && Line(alternate, 4) == "DONE", "alternate viewport and cursor visibility");
        Assert(screen.Modes == new TerminalScreen.InputModes(true, true, 1002, true), "application input modes parsed");
        screen.Read("\u001b=");
        Assert(screen.Modes.ApplicationKeypad, "application keypad mode");
        screen.Read("\u001b>");
        Assert(!screen.Modes.ApplicationKeypad, "numeric keypad mode");
        screen.Read("\u001b]52;c;Q2xpcGJvYXJk");
        screen.Read("\u001b\\\u001bPprivate\u001b\\\u001b[?" + new string('9', 140) + "h\u001b[?25;1049l");
        var main = Capture(screen);
        Assert(!main.Alternate && Line(main, 0) == "OK" && Line(main, 1) == "  鹈🚲e\u0301" &&
            main.CursorColumn == frame.CursorColumn && main.CursorRow == frame.CursorRow, "alternate exit restores main cursor and text");
        Assert(Line(alternate, 4) == "DONE", "published frames keep their own contents");

        var margins = new TerminalScreen(20, 5);
        margins.Read("Header\r\none\r\ntwo\r\nthree\r\nFooter\u001b[2;4r\u001b[4;1H\nreplaced");
        var scoped = Capture(margins);
        Assert(Line(scoped, 0) == "Header" && Line(scoped, 1) == "two" && Line(scoped, 2) == "three" &&
            Line(scoped, 3) == "replaced" && Line(scoped, 4) == "Footer" && scoped.Scrollback == 0, "scroll margins");
        margins.Read("\u001b[2;1Habc中Z\u001b[K\u001b[2;4H\u001b[P");
        Assert(Line(Capture(margins), 1) == "abc Z", "deleting half a wide glyph clears its other cell");

        var history = new TerminalScreen(20, 4);
        history.Read(string.Concat(Enumerable.Range(0, 2500).Select(index => $"{index}\r\n")));
        var latest = Capture(history);
        var older = history.Capture(-1, 80)!;
        Assert(latest.Scrollback == 2000 && latest.CursorVisible && !older.CursorVisible && older.ScrollOffset == 80 &&
            Line(older, 0) == "2417", "bounded main scrollback and hidden cursor while browsing history");
        history.Resize(80, 9);
        latest = Capture(history);
        Assert(latest.Columns == 80 && latest.Rows == 9 && Line(latest, 2) == "2499", "resize retains live-screen content");
    }

    private static async Task ConPtyAsync(string workspace)
    {
        const string script = """
            [Console]::OutputEncoding = [Text.Encoding]::UTF8
            $e=[char]27
            [Console]::Write("MAIN_READY"+[Environment]::NewLine+"$e[?1049h$e[2J$e[H$e[32mALT_READY$e[0m")
            $key=[Console]::ReadKey($true)
            $value=[Text.StringBuilder]::new()
            while (($character=[Console]::ReadKey($true)).Key -ne [ConsoleKey]::Enter) {
                [void]$value.Append($character.KeyChar)
            }
            [Console]::WriteLine("$e[?1049lKEY_$($key.Key):$value")
            exit 7
            """;
        var screen = new TerminalScreen(72, 18);
        var input = Channel.CreateUnbounded<TerminalSession.Input>();
        var plain = new TerminalText();
        var transcript = new StringBuilder();
        var sawApplicationFrame = false;
        using var stop = new CancellationTokenSource(TimeSpan.FromSeconds(18));
        TerminalSession.Result result;
        try
        {
            result = await TerminalSession.RunCoreAsync(workspace, 72, 18, input.Reader, text =>
            {
                screen.Read(text);
                transcript.Append(plain.Read(text));
                if (sawApplicationFrame || !transcript.ToString().Contains("ALT_READY", StringComparison.Ordinal))
                    return;
                var frame = Capture(screen);
                // Some Console Host versions redraw the entire screen for a buffer switch.
                sawApplicationFrame = Line(frame, 0).Contains("ALT_READY", StringComparison.Ordinal);
                if (!sawApplicationFrame)
                    stop.Cancel();
                input.Writer.TryWrite(new TerminalSession.Input.Resize(84, 26));
                screen.Resize(84, 26);
                input.Writer.TryWrite(new TerminalSession.Input.Text("\u001b[D"));
                input.Writer.TryWrite(new TerminalSession.Input.Text("鹈鹕输入\r"));
            }, stop.Token, script, rawOutput: true);
        }
        catch (OperationCanceledException error)
        {
            throw new InvalidOperationException("ConPTY fixture did not reach its expected state. Output: " + transcript, error);
        }
        Assert(result is { ExitCode: 7, Resized: true } && sawApplicationFrame && !Capture(screen).Alternate &&
            transcript.ToString().Contains("KEY_LeftArrow:鹈鹕输入", StringComparison.Ordinal),
            "real ConPTY full-screen redraw, editing key, Chinese, resize and process exit. Output: " + transcript);
    }

    private static TerminalScreen.Frame Capture(TerminalScreen screen) => screen.Capture(-1, 0)!;

    private static string Line(TerminalScreen.Frame frame, int row)
    {
        var text = new StringBuilder();
        foreach (var cell in frame.Lines[row].Span)
            if (cell.Width != 0)
                text.Append(cell.Text ?? " ");
        return text.ToString().TrimEnd();
    }

    private static void Assert(bool condition, string message)
    {
        if (!condition)
            throw new InvalidOperationException(message);
    }
}
