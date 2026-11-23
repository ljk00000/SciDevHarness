using SciDev.Core;

namespace SciDev.Cli;

internal sealed record FailureReport(string ErrorKind, string UserMessage, bool Retryable, string Detail)
{
    internal static FailureReport For(RunResult result)
    {
        var (kind, retryable) = result.ModelFailure switch
        {
            ModelFailureKind.Authentication => ("auth", false),
            ModelFailureKind.RateLimit => ("rate_limit", true),
            ModelFailureKind.Timeout => ("timeout", true),
            ModelFailureKind.Network => ("network", true),
            ModelFailureKind.Protocol => ("tool", true),
            ModelFailureKind.RejectedRequest => ("internal", false),
            _ => ClassifyStatus(result.Status),
        };
        var detail = kind == "no_changes" ? "no_changes_detected" : CleanDetail(result.Text);
        var message = kind switch
        {
            "auth" => "模型服务认证或权限失败，请检查 API Key、模型权限和 endpoint。",
            "rate_limit" => "模型服务限流或额度不足，稍后重试或切换模型。",
            "timeout" => "操作超时。请核对执行状态，再决定是否重试。",
            "network" => "连接失败或服务暂时不可用，请检查服务地址与连接状态。",
            "approval" => "操作需要本次人工审批，请检查当前模式和审批结果。",
            "tool" => "请检查参数或修正模型返回的工具调用。",
            "verification" => "请检查验证诊断，修复问题后重新验证。",
            "workspace" => "请检查工作区路径、权限、Git 或会话状态。",
            "no_changes" => "任务结束但没有修改文件；如只是问答或分析请加 --allow-no-changes。",
            "cancelled" => "运行已取消。请核对运行记录中的改动。",
            _ when result.ModelFailure == ModelFailureKind.RejectedRequest => "模型服务拒绝了请求，请检查 API 地址、模型 ID 和请求设置。",
            _ => "任务未完成，请查看运行记录和错误详情。",
        };
        return new(kind, message, retryable, detail);
    }

    private static (string Kind, bool Retryable) ClassifyStatus(string status) => status switch
    {
        "cancelled" => ("cancelled", false),
        "no_changes" => ("no_changes", false),
        "model_timeout" or "sandbox_timeout" or "host_command_timeout" or "network_timeout" or "mcp_timeout" => ("timeout", true),
        "network_fetch_failed" or "mcp_http_failed" => ("network", true),
        "approval_denied" or "approval_required" or "host_approval_required" or "network_policy_denied" or
            "review_unavailable" or "timed_out" or "denied" or "read_only" or "host_command_blocked" or
            "host_output_not_shared" or "mcp_output_not_shared" => ("approval", false),
        "invalid_input" or "unsupported_tool" or "model_token_limit" or "turn_limit" => ("tool", false),
        "invalid_tool" or "invalid_plan" or "plan_required" or "plan_incomplete" or "model_incomplete" or
            "mcp_invalid_arguments" => ("tool", true),
        "verification_required" or "verification_directory" or "verification_policy" or "command_failed" or
            "artifact_validation_failed" or "artifact_validation_error" => ("verification", true),
        "credential_not_found" => ("auth", false),
        "io_error" or "file_error" or "external_file_error" or "edit_conflict" or "path_denied" or "editor_draft" or
            "sandbox_unavailable" or "sandbox_error" or "sandbox_limit" or "host_command_error" => ("workspace", false),
        _ when status.EndsWith("_credential", StringComparison.Ordinal) || status == "sandbox_content_denied" => ("approval", false),
        _ when status.StartsWith("session_", StringComparison.Ordinal) || status.StartsWith("legacy_", StringComparison.Ordinal) ||
            status.StartsWith("git_", StringComparison.Ordinal) || status.StartsWith("checkpoint_", StringComparison.Ordinal) ||
            status.StartsWith("snapshot_", StringComparison.Ordinal) => ("workspace", false),
        _ => ("internal", false),
    };

    private static string CleanDetail(string text)
    {
        const int maxChars = 4000;
        var length = Math.Min(text.Length, maxChars);
        if (length > 0 && char.IsHighSurrogate(text[length - 1]))
            length--;
        return new string(text.Take(length).Select(ch => char.IsControl(ch) ? ' ' : ch).ToArray()).Trim();
    }
}
