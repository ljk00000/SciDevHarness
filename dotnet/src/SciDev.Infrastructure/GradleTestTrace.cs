using System.Globalization;

namespace SciDev.Infrastructure;

internal static class GradleTestTrace
{
    internal static bool IsTest(string[] argv) => argv is ["gradle" or "./gradlew", "test", ..];

    internal const string Script = """
        marker="$1"
        launcher="$2"
        shift 2
        export SCIDEV_GRADLE_TRACE="/tmp/$marker"
        mkdir "$SCIDEV_GRADLE_TRACE"
        cat > "$SCIDEV_GRADLE_TRACE/test-counts.gradle" <<'SCIDEV_GRADLE_TEST_COUNTS'
        gradle.allprojects {
            tasks.withType(org.gradle.api.tasks.testing.Test).configureEach {
                afterSuite { suite, result ->
                    if (suite.parent == null) {
                        def counts = new File(System.getenv('SCIDEV_GRADLE_TRACE'), java.util.UUID.randomUUID().toString() + '.count')
                        counts.text = result.successfulTestCount.toString() + ',' + result.failedTestCount.toString() + '\n'
                    }
                }
            }
        }
        SCIDEV_GRADLE_TEST_COUNTS
        status=0
        "$launcher" --init-script "$SCIDEV_GRADLE_TRACE/test-counts.gradle" -Dorg.gradle.configuration-cache=false "$@" || status=$?
        printf '\n%s\n' "$marker"
        for file in "$SCIDEV_GRADLE_TRACE"/*.count; do
            if [ -f "$file" ]; then cat "$file"; fi
        done
        printf '%s.end\n' "$marker"
        exit "$status"
        """;

    internal static string? UnsuccessfulTest(string output, string[] records, string[] argv)
    {
        if (records.Length == 0)
            return TestRunEvidence.UnsuccessfulTest(argv, output);
        var executed = false;
        foreach (var record in records)
        {
            var fields = record.Split(',');
            if (fields.Length != 2 || !long.TryParse(fields[0], NumberStyles.None, CultureInfo.InvariantCulture, out var succeeded) ||
                !long.TryParse(fields[1], NumberStyles.None, CultureInfo.InvariantCulture, out var failed) || failed > 0)
                return "test_result_unconfirmed";
            executed |= succeeded > 0;
        }
        return executed ? null : "no_tests";
    }
}
