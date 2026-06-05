using System.CommandLine;
using System.CommandLine.Invocation;
using System.Diagnostics;

namespace Hermes.Memory.Cli;

/// <summary>
/// `hermes-memory install` — run the interactive install wizard.
///
/// Delegates to the Python orchestrator at <repo>/install/steps/_step_run.py
/// via install.sh. C# provides the canonical entry point; all logic is Python.
///
/// Flags forwarded:
///   --check / --update     idempotent: only run unrun steps
///   --uninstall            reverse every step
///   --from N               start from step N (0..10)
///   --step N               run a single step
///   --yes                  non-interactive defaults
/// </summary>
public static class InstallCommand
{
    public static Command Build()
    {
        var cmd = new Command("install", "Interactive install/update/uninstall wizard (delegates to ./install.sh)");

        cmd.Add(new Option<bool>("--check"));
        cmd.Add(new Option<bool>("--update"));
        cmd.Add(new Option<bool>("--uninstall"));
        cmd.Add(new Option<int?>("--from"));
        cmd.Add(new Option<int?>("--step"));
        cmd.Add(new Option<bool>("--yes"));

        // Match the existing handler style: CommandHandler.Create<...>(async (args) => ...)
        cmd.Handler = CommandHandler.Create<bool, bool, bool, int?, int?, bool>(Run);
        return cmd;
    }

    private static int Run(bool check, bool update, bool uninstall, int? from, int? step, bool yes)
    {
        var repoRoot = Environment.GetEnvironmentVariable("HERMES_REPO_ROOT");
        if (string.IsNullOrEmpty(repoRoot) || !File.Exists(Path.Combine(repoRoot, "install.sh")))
        {
            repoRoot = FindRepoRoot(Directory.GetCurrentDirectory()) ?? "";
        }
        if (string.IsNullOrEmpty(repoRoot) || !File.Exists(Path.Combine(repoRoot, "install.sh")))
        {
            Console.Error.WriteLine("install: could not locate repo root (no install.sh in CWD or ancestors; set HERMES_REPO_ROOT)");
            return 2;
        }

        var installSh = Path.Combine(repoRoot, "install.sh");
        var psi = new ProcessStartInfo
        {
            FileName = "/usr/bin/env",
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        psi.ArgumentList.Add("bash");
        psi.ArgumentList.Add(installSh);

        if (uninstall)            psi.ArgumentList.Add("--uninstall");
        else if (check || update) psi.ArgumentList.Add("--check");
        if (from.HasValue) { psi.ArgumentList.Add("--from"); psi.ArgumentList.Add(from.Value.ToString()); }
        if (step.HasValue) { psi.ArgumentList.Add("--step"); psi.ArgumentList.Add(step.Value.ToString()); }
        if (yes) psi.ArgumentList.Add("--yes");
        if (yes) psi.Environment["HERMES_ASSUME_YES"] = "1";

        Console.Error.WriteLine($"[hermes-memory] launching: bash {installSh} {string.Join(' ', psi.ArgumentList.Skip(2))}");

        using var p = System.Diagnostics.Process.Start(psi)!;
        var stdoutDone = Task.Run(() => p.StandardOutput.BaseStream.CopyToAsync(Console.OpenStandardOutput()));
        var stderrDone = Task.Run(() => p.StandardError.BaseStream.CopyToAsync(Console.OpenStandardError()));
        p.WaitForExit();
        Task.WaitAll(stdoutDone, stderrDone);
        return p.ExitCode;
    }

    private static string? FindRepoRoot(string start)
    {
        var dir = new DirectoryInfo(start);
        while (dir != null)
        {
            if (File.Exists(Path.Combine(dir.FullName, "install.sh"))) return dir.FullName;
            dir = dir.Parent;
        }
        return null;
    }
}
