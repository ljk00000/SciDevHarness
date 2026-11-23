namespace SciDev.Infrastructure;

/// <summary>Collect the test build's resolved project references inside the container.</summary>
internal static class DotnetReferenceTrace
{
    internal const string Targets = """
        <Project>
          <Target Name="_SciDevHarnessTraceProjectReference" BeforeTargets="CoreCompile"
                  Condition="'$(MSBuildProjectFullPath)' == '$(SciDevVerificationProject)'">
            <ItemGroup>
              <_SciDevMatchedReference Include="@(ReferencePath->WithMetadataValue('MSBuildSourceProjectFile', '$(SciDevVerificationSubject)')->WithMetadataValue('ReferenceSourceTarget', 'ProjectReference')->WithMetadataValue('ReferenceOutputAssembly', 'true')->WithMetadataValue('BuildReference', 'true'))" />
            </ItemGroup>
            <PropertyGroup>
              <_SciDevReferenceResult>no</_SciDevReferenceResult>
              <_SciDevReferenceResult Condition="'@(_SciDevMatchedReference)' != '' and '$(BuildProjectReferences)' == 'true'">yes</_SciDevReferenceResult>
              <_SciDevTraceFile>$([System.Guid]::NewGuid()).trace</_SciDevTraceFile>
            </PropertyGroup>
            <WriteLinesToFile File="$(SciDevVerificationTrace)/$(_SciDevTraceFile)" Lines="$(_SciDevReferenceResult)" Overwrite="true" />
          </Target>
        </Project>
        """;

    private const string ReadArguments = """
        marker="$1"
        subject="$2"
        shift 2
        """;

    internal static string Script(string preparation) => ReadArguments + "\n" + preparation + "\n" + """
        trace="/tmp/$marker"
        mkdir "$trace"
        if [ -z "${MSBuildUserExtensionsPath:-}" ]; then
            export MSBuildUserExtensionsPath="$HOME/scidev-msbuild"
            export SciDevVerificationProject="/tmp/project/$3"
            export SciDevVerificationSubject="/tmp/project/$subject"
            export SciDevVerificationTrace="$trace"
            imports="$MSBuildUserExtensionsPath/Current/Microsoft.Common.targets/ImportAfter"
            mkdir -p "$imports"
            cat > "$imports/SciDevHarness.targets" <<'SCIDEV_MSBUILD_TARGETS'
        """ + "\n" + Targets + "\n" + """
        SCIDEV_MSBUILD_TARGETS
        fi
        status=0
        "$@" || status=$?
        printf '\n%s\n' "$marker"
        for file in "$trace"/*.trace; do
            if [ -f "$file" ]; then cat "$file"; fi
        done
        printf '%s.end\n' "$marker"
        exit "$status"
        """;

    internal static (string Output, bool CoversSubject) Read(string output, string marker)
    {
        var (stdout, records) = ContainerVerificationTrace.Read(output, marker);
        return (stdout, records.Length > 0 && records.All(answer => answer == "yes"));
    }
}
