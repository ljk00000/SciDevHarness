using SciDev.Core;
using SciDev.Infrastructure;

internal static class TestEvidenceChecks
{
    internal static void Run(DirectoryInfo workspace)
    {
        var files = new WorkspaceFiles(workspace.FullName);
        void Write(string relative, string contents)
        {
            var target = Path.Combine(workspace.FullName, relative);
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            File.WriteAllText(target, contents);
        }
        VerificationCandidate[] Candidates(string path) => files.FindVerificationPlan([new(path, "edit")], default).Candidates.ToArray();

        Write("python/pyproject.toml", "[tool.pytest.ini_options]\n");
        Write("python/tests/test_empty.py", "import pytest\n# No tests are defined here.\n");
        Write("python/nested/pyproject.toml", "[project]\nname='nested'\n");
        Write("python/nested/tests/nested_test.py", "from unittest import TestCase\nclass Sample(TestCase):\n    def test_true(self):\n        self.assertTrue(True)\n");
        Assert(Candidates("python/app.py").Length == 0, "pytest declaration and an import-only file yield no tests; nested tests are separate");
        var unittest = Candidates("python/nested/app.py").Single();
        Assert(unittest.Directory == "python/nested" && unittest.Arguments.SequenceEqual(
            ["python", "-m", "unittest", "discover", "-s", "tests", "-p", "nested_test.py", "-v"]),
            "unittest uses the actual directory and filename, including suffix-named tests");
        Write("python/tests/test_run.py", "def test_actual():\n    assert True\n");
        Assert(Candidates("python/app.py").Single().Arguments.SequenceEqual(["python", "-m", "pytest", "tests/test_run.py"]),
            "pytest receives only locally owned test files");
        Write("examples/test_example.py", "def test_manifestless():\n    assert True\n");
        Assert(Candidates("loose.py").Single().Arguments.SequenceEqual(["python", "-m", "pytest", "examples/test_example.py"]),
            "Python syntax can provide test evidence in a workspace without manifests");

        Write("rust/Cargo.toml", "[package]\nname='root'\n");
        Write("rust/src/lib.rs", "pub fn answer() -> i32 { 42 }\n");
        Write("rust/child/Cargo.toml", "[package]\nname='child'\n");
        Write("rust/child/src/lib.rs", "#[cfg(test)]\nmod tests { #[test] fn it_works() {} }\n");
        Assert(Candidates("rust/src/lib.rs").Length == 0, "nested Rust tests do not verify the parent package");
        Assert(Candidates("rust/child/src/lib.rs").Single().Arguments.SequenceEqual(["cargo", "test"]), "Rust test attributes yield a local candidate");

        Write("rust/Cargo.toml", "[workspace]\nmembers=['crates/*']\ndefault-members=['crates/other']\nexclude=['crates/excluded']\n");
        Write("rust/crates/other/Cargo.toml", "[package]\nname='other'\n");
        Write("rust/crates/other/src/lib.rs", "#[test]\nfn check() {}\n");
        Write("rust/crates/excluded/Cargo.toml", "[package]\nname='excluded'\n");
        Write("rust/crates/excluded/src/lib.rs", "pub fn excluded() {}\n");
        var rootCheck = Candidates("rust/Cargo.lock").Single();
        Assert(rootCheck.Directory == "rust" && rootCheck.Arguments.SequenceEqual(["cargo", "test", "--workspace", "--offline"]) &&
            rootCheck.TargetDirectories!.SequenceEqual(["rust"]), "virtual workspace changes target all Cargo members, including non-default ones");
        var combinedRust = files.FindVerificationPlan([new("rust/crates/excluded/src/lib.rs", "edit"),
            new("rust/crates/other/src/lib.rs", "edit")], default);
        Assert(combinedRust.Candidates[0].TargetDirectories!.SequenceEqual(["rust/crates/excluded", "rust/crates/other"]) &&
            combinedRust.Candidates[1].Arguments.SequenceEqual(["cargo", "test"]), "aggregate Cargo candidates retain individual fallbacks");
        CheckCargoMetadata();

        Write("java/pom.xml", "<project />");
        Write("kotlin/build.gradle.kts", "plugins {}\n");
        Write("java/src/main/App.java", "class App {}\n");
        Write("kotlin/src/main/App.kt", "class App\n");
        Assert(Candidates("java/src/main/App.java").Length == 0 && Candidates("kotlin/src/main/App.kt").Length == 0,
            "Maven and Gradle manifests require test sources");
        Write("java/src/test/java/AppTest.java", "class AppTest {}\n");
        Write("kotlin/src/test/kotlin/AppTest.kt", "class AppTest\n");
        Assert(Candidates("java/src/main/App.java").Single().Arguments.SequenceEqual(["mvn", "test"]) &&
            Candidates("kotlin/src/main/App.kt").Single().Arguments.SequenceEqual(["gradle", "test"]), "JVM tests stay scoped by project");

        Write("dotnet/Samples.Tests.csproj", "<Project Sdk='Microsoft.NET.Sdk' />");
        Assert(Candidates("dotnet/App.cs").Length == 0, "project name alone does not prove a .NET test runner");
        Write("dotnet/Samples.Tests.csproj", "<Project><ItemGroup><PackageReference Include='Microsoft.NET.Test.Sdk' /></ItemGroup></Project>");
        Assert(Candidates("dotnet/App.cs").Single().Arguments.SequenceEqual(["dotnet", "test", "Samples.Tests.csproj", "--no-restore"]),
            "test SDK produces a scoped .NET candidate");
        Write("dotnet/Samples.Tests.csproj", "<Project Sdk='MSTest.Sdk/4.0.0'><PropertyGroup><IsTestProject>false</IsTestProject></PropertyGroup></Project>");
        Assert(Candidates("dotnet/App.cs").Length == 0, "a project can explicitly disable its test target");
        Write("product/Lib/Lib.csproj", "<Project Sdk='Microsoft.NET.Sdk' />");
        Write("product/API/API.csproj", "<Project><ItemGroup><ProjectReference Include='../Lib/Lib.csproj' /></ItemGroup></Project>");
        var suite = "product/Suite/Suite.csproj";
        var library = "product/Lib/Lib.csproj";
        const string suitePrefix = "<Project Sdk='MSTest.Sdk/4.0.0'><ItemGroup>";
        const string suiteSuffix = "</ItemGroup></Project>";
        Write(suite, suitePrefix + "<ProjectReference Include='..\\API\\API.csproj' />" + suiteSuffix);
        var dependent = Candidates("product/Lib/Feature.cs").Single();
        Assert(dependent.ReferencedProject == library && dependent.Directory == "product/Lib" && dependent.Manifest == suite &&
            dependent.Arguments.SequenceEqual(["dotnet", "test", suite, "--no-restore"]), "verified transitive projects find a test suite from the workspace root");
        Assert(DotnetTestProjects.SnapshotHasProjects(workspace.FullName, suite, library), "both projects are present in the copied workspace");
        Write(suite, suitePrefix + "<ProjectReference Include='../API/API.csproj' ReferenceOutputAssembly='false' />" + suiteSuffix);
        Assert(Candidates("product/Lib/Feature.cs").Length == 0, "explicitly disabled references do not suggest a candidate");
        Write(suite, suitePrefix + "<ProjectReference Include='../API/API.csproj' /><ProjectReference Remove='../API/API.csproj' />" + suiteSuffix);
        Assert(Candidates("product/Lib/Feature.cs").Single().ReferencedProject == library,
            "a potential reference with an override is deferred to the actual test build");
        Write(suite, suitePrefix + "<ProjectReference Include='../API/API.csproj' Condition=\"'$(Configuration)' == 'Debug'\" />" + suiteSuffix);
        Assert(Candidates("product/Lib/Feature.cs").Single().ReferencedProject == library,
            "conditional references can propose a test without approving its coverage");
        Write(suite, suitePrefix + "<ProjectReference Include='../api/API.csproj' />" + suiteSuffix);
        Assert(!DotnetTestProjects.SnapshotHasProjects(workspace.FullName, suite, "product/lib/Lib.csproj") &&
            Candidates("product/Lib/Feature.cs").Length == 0, "case mismatches cannot verify a Linux snapshot");
        Write(suite, suitePrefix + "<ProjectReference Include='../Lib/Lib.csproj' />" + suiteSuffix);
        Write("product/Lib/Sibling.csproj", "<Project Sdk='Microsoft.NET.Sdk' />");
        Assert(!DotnetTestProjects.SnapshotHasProjects(workspace.FullName, suite, library) &&
            Candidates("product/Lib/Feature.cs").Length == 0, "ambiguous project directory does not acquire cross-project evidence");

        Write("imports/Lib/Lib.csproj", "<Project Sdk='Microsoft.NET.Sdk' />");
        Write("imports/Suite/Suite.csproj", "<Project Sdk='MSTest.Sdk/4.0.0' />");
        Write("imports/Directory.Build.props", """
            <Project><PropertyGroup><ConfigPath>$(MSBuildThisFileDirectory)config/</ConfigPath></PropertyGroup>
            <Import Project="$(ConfigPath)**/*.props" Condition="'$(MSBuildProjectName)' == 'Suite'" /></Project>
            """);
        Write("imports/config/nested/references.props", """
            <Project><PropertyGroup><ReferencedLibrary>$(MSBuildThisFileDirectory)../../Lib/Lib.csproj</ReferencedLibrary></PropertyGroup>
            <ItemGroup><ProjectReference Include="$(ReferencedLibrary)" /></ItemGroup></Project>
            """);
        Assert(Candidates("imports/Lib/Feature.cs").Single().Manifest == "imports/Suite/Suite.csproj",
            "workspace imports and expanded references can propose the corresponding suite");
        Write("imports/Directory.Build.props", "<Project><Import Project='../product/does-not-exist.props' /></Project>");
        Write("imports/config/nested/references.props", "<Project><ItemGroup><ProjectReference Include='../../Lib/Lib.csproj' /></ItemGroup></Project>");
        Write("imports/Suite/Directory.Build.targets", "<Project><Import Project='../config/nested/references.props' /></Project>");
        Assert(Candidates("imports/Lib/Feature.cs").Length == 0,
            "a relative import uses its declaring file, but a relative project reference uses the test project directory");
        Write("imports/config/nested/references.props", "<Project><ItemGroup><ProjectReference Include='../Lib/Lib.csproj' /></ItemGroup></Project>");
        Assert(Candidates("imports/Lib/Feature.cs").Single().ReferencedProject == "imports/Lib/Lib.csproj",
            "Directory.Build.targets provides a project-relative reference hint");

        const string testOutput = "Passed! - Failed: 0, Passed: 1, Skipped: 0, Total: 1";
        const string marker = "trace-test";
        Assert(DotnetReferenceTrace.Read(testOutput + "\ntrace-test\nyes\nyes\r\ntrace-test.end\n", marker) == (testOutput, true),
            "resolved references in all observed test builds provide separate evidence");
        Assert(!DotnetReferenceTrace.Read(testOutput + "\ntrace-test\nyes\nno\ntrace-test.end\n", marker).CoversSubject &&
            !DotnetReferenceTrace.Read(testOutput + "\ntrace-test\nyes\n", marker).CoversSubject &&
            !DotnetReferenceTrace.Read(testOutput, marker).CoversSubject, "mixed, missing and partial build traces do not obtain scope");

        CheckOutput(["python", "-m", "unittest"], "Ran 0 tests in 0.001s\nOK\n", "no_tests");
        CheckOutput(["python", "-m", "unittest"], "Ran 2 tests in 0.001s\nOK (skipped=1, expected failures=1)\n", "no_tests");
        CheckOutput(["python", "-m", "unittest"], "Ran 1 test in 0.001s\nOK\n", null);
        CheckOutput(["python", "-m", "pytest"], "===== 1 skipped in 0.1s =====\n", "no_tests");
        CheckOutput(["python", "-m", "pytest"], "===== 1 passed, 1 skipped in 0.1s =====\n", null);
        CheckOutput(["cargo", "test"], "test result: ok. 0 passed; 0 failed; 0 ignored\n", "no_tests");
        CheckOutput(["cargo", "test"], "test result: ok. 0 passed;\ntest result: ok. 2 passed; 0 failed\n", null);
        CheckOutput(["dotnet", "test"], "", "test_result_unconfirmed");
        CheckOutput(["dotnet", "test"], "No test is available in Sample.dll.\n", "no_tests");
        CheckOutput(["dotnet", "test"], "Passed! - Failed: 0, Passed: 2, Skipped: 0, Total: 2\n", null);
        CheckOutput(["dotnet", "test"], "已通过! - 失败: 0，通过: 1，已跳过: 0，总计: 1\n", null);
        CheckOutput(["dotnet", "test"], "Test run summary: Passed! - succeeded: 1, skipped: 0\n", null);
        CheckOutput(["node", "--test"], "TAP version 13\n1..0\n# pass 0\n", "no_tests");
        CheckOutput(["node", "--test"], "ℹ tests 0\nℹ suites 0\nℹ pass 0\nℹ fail 0\n", "no_tests");
        CheckOutput(["node", "--test"], "ℹ tests 1\r\nℹ suites 0\r\nℹ pass 1\r\nℹ fail 0\r\n", null);
        CheckOutput(["node", "--test"], "ℹ pass 1\n", "test_result_unconfirmed");
        CheckOutput(["go", "test", "./..."], "?   demo      [no test files]\nok  demo/pkg  0.004s [no tests to run]\n", "no_tests");
        CheckOutput(["mvn", "test"], "[INFO] No tests to run.\n[INFO] BUILD SUCCESS\n", "no_tests");
        CheckOutput(["gradle", "test"], "> Task :app:test NO-SOURCE\nBUILD SUCCESSFUL\n", "no_tests");
        CheckOutput(["gradle", "test"], "> Task :test\nBUILD SUCCESSFUL\n", "test_result_unconfirmed");
        var gradleOutput = ContainerVerificationTrace.Read("BUILD SUCCESSFUL\ntrace-test\n3,0\n0,0\ntrace-test.end\n", marker);
        Assert(gradleOutput.Output == "BUILD SUCCESSFUL" &&
            GradleTestTrace.UnsuccessfulTest(gradleOutput.Output, gradleOutput.Records, ["gradle", "test"]) is null &&
            GradleTestTrace.UnsuccessfulTest("BUILD SUCCESSFUL", ["0,0"], ["gradle", "test"]) == "no_tests" &&
            GradleTestTrace.UnsuccessfulTest("BUILD SUCCESSFUL", ["1,1"], ["gradle", "test"]) == "test_result_unconfirmed",
            "Gradle evidence uses actual successful test counts, including when the process otherwise succeeds");
    }

    private static void CheckOutput(string[] args, string output, string? expected) =>
        Assert(TestRunEvidence.UnsuccessfulTest(args, output) == expected, $"unexpected test evidence from {string.Join(' ', args)}");

    private static void CheckCargoMetadata()
    {
        const string json = """
            { "version": 1, "workspace_root": "/tmp/project/rust", "workspace_members": ["real", "implicit", "outside"],
              "workspace_default_members": ["real"], "packages": [
                { "id": "real", "manifest_path": "/tmp/project/rust/crates/other/Cargo.toml" },
                { "id": "implicit", "manifest_path": "/tmp/project/rust/crates/dependency/Cargo.toml" },
                { "id": "outside", "manifest_path": "/tmp/project-tmp/outside/Cargo.toml" },
                { "id": "excluded", "manifest_path": "/tmp/project/rust/crates/excluded/Cargo.toml" }
              ] }
            """;
        Assert(CargoWorkspaceTests.ReadMetadata(json).SequenceEqual(["rust", "rust/crates/dependency", "rust/crates/other"]),
            "only Cargo's actual in-copy workspace members and root may obtain coverage");
        var missingMember = json.Replace("[\"real\", \"implicit\", \"outside\"]", "[\"missing\"]", StringComparison.Ordinal);
        Assert(CargoWorkspaceTests.ReadMetadata(missingMember).Length == 0, "a member ID must have matching package metadata");
        Assert(CargoWorkspaceTests.ReadMetadata("{\"workspace_root\":\"/tmp/project\"}").Length == 0, "incomplete metadata does not verify scopes");
        Assert(CargoWorkspaceTests.ReadMetadata(json[..(json.Length / 2)]).Length == 0, "truncated metadata does not verify scopes");
        Assert(SandboxVerification.Classify(["cargo", "test", "--workspace", "--offline"]) == "test" &&
            SandboxVerification.Classify(["cargo", "test", "--workspace", "--exclude", "other"]) is null,
            "workspace evidence cannot be requested with a package exclusion or other coverage filters");
    }

    private static void Assert(bool condition, string message)
    {
        if (!condition)
            throw new InvalidOperationException(message);
    }
}
