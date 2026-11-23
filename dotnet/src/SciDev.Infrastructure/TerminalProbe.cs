using System.Diagnostics;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Channels;

namespace SciDev.Infrastructure;

public static partial class TerminalProbe
{
    public sealed record Result(bool EchoedChinese, bool Resized, bool Cancelled, int ExitCode, bool ChildExited);

    public static async Task<Result> CheckAsync(CancellationToken token)
    {
        var workspace = Directory.GetCurrentDirectory();
        const string sample = "鹈鹕输入";
        var input = Channel.CreateUnbounded<TerminalSession.Input>();
        var output = new StringBuilder();
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(TimeSpan.FromSeconds(15));
        var sent = false;
        var echo = await TerminalSession.RunCoreAsync(workspace, 80, 25, input.Reader, text =>
        {
            output.Append(text);
            if (!sent && output.ToString().Contains("SCIDEV_READY", StringComparison.Ordinal))
            {
                sent = true;
                input.Writer.TryWrite(new TerminalSession.Input.Resize(120, 35));
                input.Writer.TryWrite(new TerminalSession.Input.Text(sample + "\r"));
            }
        }, deadline.Token, "Write-Host 'SCIDEV_READY'; $value=Read-Host; Write-Host ('SCIDEV_ECHO:'+ $value); exit 7");
        var (cancelled, childExited) = await CheckCancellationAsync(workspace, token);
        return new(output.ToString().Contains("SCIDEV_ECHO:" + sample, StringComparison.Ordinal),
            echo.Resized, cancelled, echo.ExitCode, childExited);
    }

    private static async Task<(bool Cancelled, bool ChildExited)> CheckCancellationAsync(string workspace, CancellationToken token)
    {
        const string script = """
            $child = Start-Process (Join-Path $PSHOME 'powershell.exe') -WindowStyle Hidden -PassThru -ArgumentList '-NoProfile -Command Start-Sleep -Seconds 30'
            Write-Host ('SCIDEV_CHILD:' + $child.Id + ':READY')
            Start-Sleep -Seconds 30
            """;
        using var stop = CancellationTokenSource.CreateLinkedTokenSource(token);
        stop.CancelAfter(TimeSpan.FromSeconds(12));
        var input = Channel.CreateUnbounded<TerminalSession.Input>();
        var captured = new StringBuilder();
        Process? child = null;
        var cancelled = false;
        try
        {
            await TerminalSession.RunCoreAsync(workspace, 80, 25, input.Reader, text =>
            {
                captured.Append(text);
                if (child is not null)
                    return;
                var marker = ChildMarker().Match(captured.ToString());
                if (!marker.Success)
                    return;
                child = Process.GetProcessById(int.Parse(marker.Groups[1].Value, System.Globalization.CultureInfo.InvariantCulture));
                _ = child.SafeHandle; // Hold the actual process reference before teardown.
                stop.Cancel();
            }, stop.Token, script);
        }
        catch (OperationCanceledException) when (!token.IsCancellationRequested)
        {
            cancelled = true;
        }
        using (child)
        {
            if (child is null)
                return (cancelled, false);
            using var wait = CancellationTokenSource.CreateLinkedTokenSource(token);
            wait.CancelAfter(TimeSpan.FromSeconds(3));
            await child.WaitForExitAsync(wait.Token);
            return (cancelled, child.HasExited);
        }
    }

    [GeneratedRegex("SCIDEV_CHILD:([0-9]+):READY")]
    private static partial Regex ChildMarker();
}
