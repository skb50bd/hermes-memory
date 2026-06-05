using System.CommandLine.Invocation;
using Hermes.Memory.Core.Db;
using Hermes.Memory.Core.Models;
using Microsoft.Extensions.Logging;
using Npgsql;

namespace Hermes.Memory.Cli;

/// <summary>
/// 16-check preflight. Reuses the same diagnostic shape as the
/// hermes-memory plugin so existing operational knowledge
/// transfers. Order matters: catalog → grants → data.
/// </summary>
public static class PreflightHandler
{
    public static ICommandHandler Create() => CommandHandler.Create(RunAsync);

    public static async Task<int> RunAsync(InvocationContext ctx)
    {
        var connStr = Environment.GetEnvironmentVariable("HERMES_PG_CONN_STR")
            ?? throw new InvalidOperationException("HERMES_PG_CONN_STR is not set");

        var checks = new List<PreflightCheck>();

        // 1. Can connect
        try
        {
            await using var conn = new NpgsqlConnection(connStr);
            await conn.OpenAsync();
            checks.Add(new("connect", true));
        }
        catch (Exception ex)
        {
            checks.Add(new("connect", false, ex.Message));
            return OutputAndExit(checks);
        }

        // Open one connection for the rest
        await using var conn2 = new NpgsqlConnection(connStr);
        await conn2.OpenAsync();

        // 2-7. Required extensions
        foreach (var ext in new[] { "vector", "pg_trgm", "pg_cron", "timescaledb", "age" })
        {
            try
            {
                await using var cmd = new NpgsqlCommand(
                    "SELECT extname, extversion FROM pg_extension WHERE extname = @n", conn2);
                cmd.Parameters.AddWithValue("n", ext);
                await using var r = await cmd.ExecuteReaderAsync();
                if (await r.ReadAsync())
                    checks.Add(new($"ext.{ext}", true, r.GetString(1)));
                else
                    checks.Add(new($"ext.{ext}", false, "not installed"));
            }
            catch (Exception ex) { checks.Add(new($"ext.{ext}", false, ex.Message)); }
        }

        // 8-12. Required schemas
        foreach (var schema in new[] { "agent_memory", "hermes_wiki", "hermes_journal", "hermes_skills", "hermes_metrics" })
        {
            try
            {
                await using var cmd = new NpgsqlCommand(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name = @n", conn2);
                cmd.Parameters.AddWithValue("n", schema);
                var v = await cmd.ExecuteScalarAsync();
                checks.Add(new($"schema.{schema}", v is not null));
            }
            catch (Exception ex) { checks.Add(new($"schema.{schema}", false, ex.Message)); }
        }

        // 13. agent_memory.memories exists + readable
        try
        {
            await using var cmd = new NpgsqlCommand("SELECT count(*) FROM agent_memory.memories", conn2);
            var n = (long)(await cmd.ExecuteScalarAsync())!;
            checks.Add(new("read.agent_memory.memories", true, $"{n} rows"));
        }
        catch (Exception ex) { checks.Add(new("read.agent_memory.memories", false, ex.Message)); }

        // 14. Embedder registry has a default dim
        try
        {
            await using var cmd = new NpgsqlCommand(
                "SELECT (value #>> '{}')::int FROM agent_memory.settings WHERE key = 'default_dim'", conn2);
            var dim = (int)(await cmd.ExecuteScalarAsync() ?? 0);
            checks.Add(new("embedder.default_dim", dim is 768 or 1024 or 1536, $"dim={dim}"));
        }
        catch (Exception ex) { checks.Add(new("embedder.default_dim", false, ex.Message)); }

        // 15. HNSW index on the default-dim column
        try
        {
            await using var cmd = new NpgsqlCommand(
                """
                SELECT 1 FROM pg_indexes
                WHERE schemaname = 'agent_memory' AND tablename = 'memories' AND indexname LIKE '%hnsw%'
                LIMIT 1
                """, conn2);
            var ok = await cmd.ExecuteScalarAsync();
            checks.Add(new("hnsw.index.agent_memory", ok is not null));
        }
        catch (Exception ex) { checks.Add(new("hnsw.index.agent_memory", false, ex.Message)); }

        // 16. role connection count sane
        try
        {
            await using var cmd = new NpgsqlCommand(
                "SELECT count(*) FROM pg_stat_activity WHERE usename = current_user", conn2);
            var n = (int)(long)(await cmd.ExecuteScalarAsync())!;
            checks.Add(new("conn.count", n < 20, $"{n} active for current_user"));
        }
        catch (Exception ex) { checks.Add(new("conn.count", false, ex.Message)); }

        return OutputAndExit(checks);
    }

    private static int OutputAndExit(List<PreflightCheck> checks)
    {
        var result = PreflightResult.From(checks);
        foreach (var c in result.Checks)
        {
            var mark = c.Pass ? "✓" : "✗";
            var detail = c.Detail is null ? "" : $" — {c.Detail}";
            Console.WriteLine($"  {mark} {c.Name}{detail}");
        }
        Console.WriteLine();
        Console.WriteLine($"{result.PassedCount}/{result.Checks.Count} passed");
        return result.AllPass ? 0 : 1;
    }
}
