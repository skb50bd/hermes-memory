using System.CommandLine;
using Hermes.Memory.Core.Mcp;

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

    // Note: System.CommandLine 2.0.0-beta1.21072.2 does not support the
    // collection-initializer-with-Handler pattern that the older 1.x
    // `new Command(...) { new Option(...), Handler = ... }` used. We build
    // each command imperatively with .Add() and .Handler = ... instead.
    // Re-pin to a 2.0 version that supports initializer syntax when one ships.

    private static Command BuildMcpCommand()
    {
        var cmd = new Command("--mcp", "Run the stdio MCP server (used by the agent)");
        cmd.Handler = System.CommandLine.Invocation.CommandHandler.Create<string[]>(McpHost.RunAsync);
        return cmd;
    }

    private static Command BuildPreflightCommand()
    {
        var cmd = new Command("preflight", "Run 16-check preflight diagnostic");
        cmd.Handler = PreflightHandler.Create();
        return cmd;
    }

    private static Command BuildMigrateCommand()
    {
        var cmd = new Command("migrate", "Run migrations against a connection string");
        var conn = new Option<string>("--conn", "Postgres connection string");
        conn.IsRequired = true;
        var to = new Option<string>("--to", "Target migration version");
        // 2.0.0-beta1.21072.2: SetDefaultValue lives on the underlying Argument,
        // not on the Option itself. The Option<>.SetDefaultValue extension
        // only exists in later 2.0 builds.
        to.Argument.SetDefaultValue("head");
        cmd.Add(conn);
        cmd.Add(to);
        cmd.Handler = MigrateHandler.Create();
        return cmd;
    }

    private static Command BuildProfileCommand()
    {
        var cmd = new Command("profile", "Manage per-agent databases");

        var create = new Command("create", "Create a new profile DB cloned from hermes_template");
        create.Add(new Argument<string>("name"));
        create.Add(new Option<string>("--conn", "Postgres connection string (superuser)"));
        create.Handler = ProfileHandler.CreateHandler();

        var list = new Command("list", "List all hermes_* databases");
        list.Add(new Option<string>("--conn", "Postgres connection string"));
        list.Handler = ProfileHandler.ListHandler();

        var drop = new Command("drop", "Drop a profile DB");
        drop.Add(new Argument<string>("name"));
        drop.Add(new Option<string>("--conn", "Postgres connection string"));
        drop.Add(new Option<bool>("--confirm", "Skip the confirmation prompt"));
        drop.Handler = ProfileHandler.DropHandler();

        cmd.AddCommand(create);
        cmd.AddCommand(list);
        cmd.AddCommand(drop);
        return cmd;
    }

    private static Command BuildEmbedCommand()
    {
        var cmd = new Command("embed", "Standalone embedder test");
        var text = new Option<string>("--text", "Text to embed");
        text.IsRequired = true;
        var dim = new Option<int>("--dim", "Embedding dimension (768/1024/1536)");
        // See note in BuildMigrateCommand: defaults live on Argument in this
        // System.CommandLine build.
        dim.Argument.SetDefaultValue(1024);
        cmd.Add(text);
        cmd.Add(dim);
        cmd.Handler = EmbedHandler.Create();
        return cmd;
    }

    private static Command BuildVersionCommand()
    {
        var cmd = new Command("version", "Print version + build SHA");
        cmd.Handler = VersionHandler.Create();
        return cmd;
    }
}
