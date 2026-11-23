namespace SciDev.Infrastructure;

internal static class ContainerVerificationTrace
{
    internal static (string Output, string[] Records) Read(string output, string marker)
    {
        var startToken = "\n" + marker + "\n";
        var start = output.LastIndexOf(startToken, StringComparison.Ordinal);
        if (start < 0)
            return (output, []);
        var endToken = marker + ".end\n";
        if (!output.EndsWith(endToken, StringComparison.Ordinal))
            return (output[..start], []);
        var trace = output[(start + startToken.Length)..^endToken.Length];
        return (output[..start], trace.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries));
    }
}
