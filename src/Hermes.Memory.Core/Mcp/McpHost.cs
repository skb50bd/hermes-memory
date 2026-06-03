using System.CommandLine.Invocation;
using Hermes.Memory.Core.Db;
using Hermes.Memory.Core.Embeddings;
using Hermes.Memory.Core.Journal;
using Hermes.Memory.Core.Kanban;
using Hermes.Memory.Core.Memory;
using Hermes.Memory.Core.Metrics;
using Hermes.Memory.Core.Models;
using Hermes.Memory.Core.Skills;
using Hermes.Memory.Core.Wiki;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using ModelContextProtocol.Server;

namespace Hermes.Memory.Core.Mcp;

/// <summary>
/// Host for the stdio MCP server. Wires up all dependencies, registers
/// the tool types, and starts the SDK's stdio transport. The agent
/// spawns this process; tool calls are JSON-RPC over stdin/stdout.
/// </summary>
public static class McpHost
{
    public static IHostBuilder ConfigureMcp(IHostBuilder builder) =>
        builder
            .ConfigureServices((ctx, services) =>
            {
                var connStr = Environment.GetEnvironmentVariable("HERMES_PG_CONN_STR")
                    ?? throw new InvalidOperationException("HERMES_PG_CONN_STR is not set");

                // Logging: stderr only (stdout is the MCP transport).
                services.AddLogging(b => b
                    .AddConsole(o => o.LogToStandardErrorThreshold = LogLevel.Trace)
                    .SetMinimumLevel(LogLevel.Warning));

                services.AddSingleton<HermesDataSource>(_ => new HermesDataSource(connStr,
                    LoggerFactory.Create(b => b.AddConsole(o => o.LogToStandardErrorThreshold = LogLevel.Warning))
                        .CreateLogger<HermesDataSource>()));
                services.AddSingleton<MigrationRunner>();
                services.AddSingleton<EmbedderRegistry>(sp => new EmbedderRegistry(
                    sp.GetRequiredService<ILogger<EmbedderRegistry>>(),
                    async (dim, provider, model, baseUrl, apiKey) =>
                    {
                        var cacheDir = Environment.GetEnvironmentVariable($"HERMES_EMBED_CACHE_DIR_{dim}")
                            ?? Environment.GetEnvironmentVariable("HERMES_EMBED_CACHE_DIR")
                            ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".cache", "hermes", "embeddings");
                        return new HermesEmbedder(
                            dim: dim, provider: provider, model: model, baseUrl: baseUrl, apiKey: apiKey,
                            cacheDir: cacheDir,
                            failOpen: Environment.GetEnvironmentVariable("HERMES_EMBED_FAIL_OPEN") != "0",
                            logger: sp.GetRequiredService<ILogger<HermesEmbedder>>());
                    }));

                services.AddSingleton<MemoryRepository>();
                services.AddSingleton<WikiRepository>();
                services.AddSingleton<JournalRepository>();
                services.AddSingleton<SkillsRepository>();
                services.AddSingleton<MetricsRepository>();
                services.AddSingleton<KanbanRepository>();

                // MCP tool types — each [McpServerToolType] is registered.
                services.AddMcpServer()
                    .WithStdioServerTransport()
                    .WithTools<MemoryTools>()
                    .WithTools<WikiTools>()
                    .WithTools<JournalTools>()
                    .WithTools<SkillsTools>()
                    .WithTools<MetricsTools>()
                    .WithTools<KanbanTools>();
            });

    public static async Task<int> RunAsync(string[] args)
    {
        // Run the host; the MCP SDK owns the stdio lifecycle.
        var host = Host.CreateDefaultBuilder(args)
            .ConfigureMcp(_ => { })
            .Build();

        // Initialize the embedder registry once at startup.
        var registry = host.Services.GetRequiredService<EmbedderRegistry>();
        var ds = host.Services.GetRequiredService<HermesDataSource>();
        await registry.InitializeAsync(ds.Inner);

        await host.RunAsync();
        return 0;
    }

    public static ICommandHandler RunHandler() =>
        CommandHandler.Create<string[]>(RunAsync);
}
