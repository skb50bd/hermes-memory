using System.CommandLine;
using Hermes.Memory.Core.Db;
using Hermes.Memory.Core.Mcp;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Hermes.Memory.Cli;

/// <summary>
/// Single entry point for the hermes-memory binary. Subcommands:
///   --mcp          Run as stdio MCP server (default for agent use)
///   preflight      16-check diagnostic
///   migrate        Run migrations against a connection string
///   profile        Manage per-agent databases
///   embed          Standalone embedder test
///   version        Print version + build SHA
/// </summary>
public static class Program
{
    public static async Task<int> Main(string[] args)
    {
        // --mcp: no args means run the MCP server. This is the agent-facing default.
        if (args.Length == 0 || (args.Length == 1 && args[0] == "--mcp"))
        {
            return await McpHost.RunAsync(args);
        }

        var root = new RootCommand("hermes-memory — Postgres-backed memory/wiki/journal/skills/metrics platform");

        root.AddCommand(BuildMcpCommand());
        root.AddCommand(BuildPreflightCommand());
        root.AddCommand(BuildMigrateCommand());
        root.AddCommand(BuildProfileCommand());
        root.AddCommand(BuildEmbedCommand());
        root.AddCommand(BuildVersionCommand());

        return await root.InvokeAsync(args);
    }

    private static Command BuildMcpCommand() =>
        new Command("--mcp", "Run the stdio MCP server (used by the agent)")
        {
            Handler = McpHost.RunHandler()
        };

    private static Command BuildPreflightCommand() =>
        new Command("preflight", "Run 16-check preflight diagnostic")
        {
            Handler = PreflightHandler.Create()
        };

    private static Command BuildMigrateCommand() =>
        new Command("migrate", "Run migrations against a connection string")
        {
            new Option<string>("--conn", "Postgres connection string") { IsRequired = true },
            new Option<string>("--to", "Target migration version", getDefaultValue: () => "head"),
            Handler = MigrateHandler.Create()
        };

    private static Command BuildProfileCommand()
    {
        var cmd = new Command("profile", "Manage per-agent databases");
        cmd.AddCommand(new Command("create", "Create a new profile DB cloned from hermes_template")
        {
            new Argument<string>("name"),
            new Option<string>("--conn", "Postgres connection string (superuser)"),
            Handler = ProfileHandler.CreateHandler()
        });
        cmd.AddCommand(new Command("list", "List all hermes_* databases")
        {
            new Option<string>("--conn", "Postgres connection string"),
            Handler = ProfileHandler.ListHandler()
        });
        cmd.AddCommand(new Command("drop", "Drop a profile DB")
        {
            new Argument<string>("name"),
            new Option<string>("--conn", "Postgres connection string"),
            new Option<bool>("--confirm", "Skip the confirmation prompt"),
            Handler = ProfileHandler.DropHandler()
        });
        return cmd;
    }

    private static Command BuildEmbedCommand() =>
        new Command("embed", "Standalone embedder test")
        {
            new Option<string>("--text", "Text to embed") { IsRequired = true },
            new Option<int>("--dim", "Embedding dimension (768/1024/1536)", getDefaultValue: () => 1024),
            Handler = EmbedHandler.Create()
        };

    private static Command BuildVersionCommand() =>
        new Command("version", "Print version + build SHA")
        {
            Handler = VersionHandler.Create()
        };
}
