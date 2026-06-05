using System.CommandLine;
using System.CommandLine.Invocation;

namespace Hermes.Memory.Cli;

/// <summary>
/// `hermes-memory uninstall` — convenience entry that recurses through
/// `install --uninstall`. Kept as a separate command (rather than a flag)
/// so users can tab-complete it directly.
/// </summary>
public static class UninstallCommand
{
    public static Command Build()
    {
        var cmd = new Command("uninstall", "Reverse every install step (stops container, removes MCP registration, deletes state file)");
        cmd.Add(new Option<bool>("--yes"));
        cmd.Handler = CommandHandler.Create<bool>(Run);
        return cmd;
    }

    private static int Run(bool yes)
    {
        var args = new List<string> { "install", "--uninstall" };
        if (yes) args.Add("--yes");
        // Recurse through Program.Main so the binary stays the canonical
        // entry point (per install design). Program.Main is async.
        return Program.Main(args.ToArray()).GetAwaiter().GetResult();
    }
}
