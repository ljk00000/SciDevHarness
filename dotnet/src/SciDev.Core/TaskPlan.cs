using System.Text.Json;

namespace SciDev.Core;

public enum PlanningMode { Off, Auto, Required }

public sealed record TaskPlanStep(string Step, string Status);
public sealed record TaskPlan(IReadOnlyList<TaskPlanStep> Steps, string Explanation, int Revision, DateTimeOffset UpdatedAt)
{
    private static readonly HashSet<string> Statuses = new(StringComparer.Ordinal)
    {
        "pending", "in_progress", "completed", "blocked", "skipped",
    };

    public bool Finished => Steps.All(step => IsDone(step.Status));
    public int Completed => Steps.Count(step => IsDone(step.Status));

    public static TaskPlan Update(TaskPlan? previous, string arguments)
    {
        using var document = JsonDocument.Parse(arguments);
        var value = document.RootElement;
        if (value.ValueKind != JsonValueKind.Object || value.EnumerateObject().Any(property => property.Name is not ("plan" or "explanation")) ||
            value.EnumerateObject().Select(property => property.Name).Distinct().Count() != value.EnumerateObject().Count() ||
            !value.TryGetProperty("plan", out var plan) || plan.ValueKind != JsonValueKind.Array || plan.GetArrayLength() is < 1 or > 12)
            throw Invalid("计划必须包含 1 到 12 个步骤。");
        var explanation = value.TryGetProperty("explanation", out var reason) ? Text(reason, 2000, "计划说明") : "";
        var steps = ValidateSteps(plan);
        if (explanation.Length == 0)
        {
            var old = previous?.Steps ?? [];
            var next = steps.ToDictionary(step => step.Step, step => step.Status, StringComparer.Ordinal);
            var newlySkipped = steps.Any(step => step.Status == "skipped" &&
                !old.Any(item => item.Step == step.Step && item.Status == "skipped"));
            if (newlySkipped || old.Any(step => !next.TryGetValue(step.Step, out var status) || IsDone(step.Status) && !IsDone(status)))
                throw Invalid("删除、重开已结束步骤或新增跳过步骤时需要填写说明。");
        }
        return new(steps, explanation, (previous?.Revision ?? 0) + 1, DateTimeOffset.UtcNow);
    }

    public static TaskPlan? ReadRecord(JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object || !value.TryGetProperty("steps", out var steps) ||
            steps.ValueKind != JsonValueKind.Array || steps.GetArrayLength() is < 1 or > 12)
            return null;
        try
        {
            var explanation = value.TryGetProperty("explanation", out var reason) ? Text(reason, 2000, "计划说明") : "";
            var revision = value.TryGetProperty("revision", out var number) && number.ValueKind == JsonValueKind.Number &&
                number.TryGetInt32(out var parsed) && parsed > 0 ? parsed : 1;
            var updatedAt = value.TryGetProperty("updated_at", out var time) && time.ValueKind == JsonValueKind.String &&
                time.TryGetDateTimeOffset(out var updated) ? updated : DateTimeOffset.UnixEpoch;
            return new(ValidateSteps(steps), explanation, revision, updatedAt);
        }
        catch (Exception ex) when (ex is ToolDeniedException or JsonException or InvalidOperationException) { return null; }
    }

    private static TaskPlanStep[] ValidateSteps(JsonElement plan)
    {
        var steps = plan.EnumerateArray().Select(ParseStep).ToArray();
        if (steps.Select(step => step.Step).Distinct(StringComparer.Ordinal).Count() != steps.Length)
            throw Invalid("步骤名称不能重复。");
        if (steps.Count(step => step.Status == "in_progress") > 1)
            throw Invalid("同一时刻最多只能有一个进行中的步骤。");
        return steps;
    }

    private static TaskPlanStep ParseStep(JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object || value.EnumerateObject().Count() != 2 ||
            !value.TryGetProperty("step", out var description) || !value.TryGetProperty("status", out var statusValue))
            throw Invalid("每个步骤只接受 step 与 status。");
        var name = Text(description, 240, "步骤");
        var status = Text(statusValue, 16, "步骤状态");
        if (name.Length == 0 || !Statuses.Contains(status))
            throw Invalid("步骤不能为空，状态必须使用指定值。");
        return new(name, status);
    }

    private static string Text(JsonElement value, int maximum, string name)
    {
        if (value.ValueKind != JsonValueKind.String || value.GetString() is not { } text || text.Length > maximum)
            throw Invalid($"{name}应为最多 {maximum} 个字符的文本。");
        return text.Trim();
    }

    private static bool IsDone(string status) => status is "completed" or "skipped";
    private static ToolDeniedException Invalid(string message) => new("invalid_plan", message);
}

public sealed record TaskPlanUpdate(TaskPlan Plan, bool IncludesExternalContent);
