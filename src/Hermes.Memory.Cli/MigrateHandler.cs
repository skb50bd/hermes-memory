using System.CommandLine.Invocation;
using Hermes.Memory.Core.Db;
using Microsoft.Extensions.Logging;

namespace Hermes.Memory.Cli;

public static class MigrateHandler
{
    public static ICommandHandler Create() =>
        CommandHandler.Create<string, string>(async (conn, to) =>
        {
            var ds = new HermesDataSource(conn, LoggerFactory.Create(b => b.AddConsole()).CreateLogger<HermesDataSource>());
            await using var _ = ds;
            var runner = new MigrationRunner(ds, LoggerFactory.Create(b => b.AddConsole()).CreateLogger<MigrationRunner>());
            var n = await runner.RunAsync(to);
            return n >= 0 ? 0 : 1;
        });
}
