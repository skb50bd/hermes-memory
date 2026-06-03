using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using Microsoft.Extensions.Logging;
using Npgsql;

namespace Hermes.Memory.Core.Db;

/// <summary>
/// Migration runner. Reads .sql files from the embedded resource stream
/// (migrations/*.sql) in lexicographic order, applies each in a single
/// transaction, and records the result in public.schema_migrations.
///
/// The migration files are the source of truth for schema. The 5 schemas
/// are ALSO created in docker/postgres/initdb.d/01-template-bootstrap.sh
/// (so a fresh container boots with everything in place), but this runner
/// exists to:
///   1. Apply new migrations to existing profile DBs after a deploy.
///   2. Bring up an empty `postgres` DB to the same state as the template.
///   3. Apply schema changes that aren't safe to bake into the template
///      (e.g. column drops, data backfills).
/// </summary>
public sealed class MigrationRunner
{
    private readonly HermesDataSource _dataSource;
    private readonly ILogger<MigrationRunner> _logger;
    private readonly IReadOnlyList<Migration> _migrations;

    public MigrationRunner(HermesDataSource dataSource, ILogger<MigrationRunner> logger)
    {
        _dataSource = dataSource;
        _logger = logger;
        _migrations = LoadMigrations();
    }

    public IReadOnlyList<Migration> All => _migrations;

    /// <summary>
    /// Apply all migrations with version &lt;= target. Runs in a single
    /// per-migration transaction. Re-running is a no-op (already-applied
    /// versions are skipped).
    /// </summary>
    public async Task<int> RunAsync(string target, CancellationToken ct = default)
    {
        await EnsureMigrationsTable(ct);

        var alreadyApplied = await GetAppliedVersions(ct);
        var pending = _migrations
            .Where(m => !alreadyApplied.Contains(m.Version))
            .OrderBy(m => m.Version, StringComparer.Ordinal)
            .TakeWhile(m => target == "head" || string.Compare(m.Version, target, StringComparison.Ordinal) <= 0)
            .ToList();

        if (pending.Count == 0)
        {
            _logger.LogInformation("No pending migrations. Database is at version {Version}.", alreadyApplied.LastOrDefault() ?? "empty");
            return 0;
        }

        foreach (var m in pending)
        {
            _logger.LogInformation("Applying migration {Version} ({Name})...", m.Version, m.Name);
            await ApplyOne(m, ct);
        }

        _logger.LogInformation("Applied {Count} migration(s). Final version: {Version}", pending.Count, pending[^1].Version);
        return pending.Count;
    }

    private async Task EnsureMigrationsTable(CancellationToken ct)
    {
        await using var conn = await _dataSource.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            CREATE TABLE IF NOT EXISTS public.schema_migrations (
                version    text PRIMARY KEY,
                applied_at timestamptz DEFAULT now(),
                checksum   text
            );
            """, conn);
        await cmd.ExecuteNonQueryAsync(ct);
    }

    private async Task<HashSet<string>> GetAppliedVersions(CancellationToken ct)
    {
        await using var conn = await _dataSource.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand("SELECT version FROM public.schema_migrations", conn);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var set = new HashSet<string>(StringComparer.Ordinal);
        while (await reader.ReadAsync(ct))
        {
            set.Add(reader.GetString(0));
        }
        return set;
    }

    private async Task ApplyOne(Migration m, CancellationToken ct)
    {
        await using var conn = await _dataSource.OpenConnectionAsync(ct);
        await using var tx = await conn.BeginTransactionAsync(ct);
        try
        {
            await using (var apply = new NpgsqlCommand(m.Sql, conn, tx))
            {
                await apply.ExecuteNonQueryAsync(ct);
            }
            await using (var record = new NpgsqlCommand(
                "INSERT INTO public.schema_migrations (version, checksum) VALUES (@v, @c) ON CONFLICT (version) DO NOTHING",
                conn, tx))
            {
                record.Parameters.AddWithValue("v", m.Version);
                record.Parameters.AddWithValue("c", m.Checksum);
                await record.ExecuteNonQueryAsync(ct);
            }
            await tx.CommitAsync(ct);
        }
        catch
        {
            await tx.RollbackAsync(ct);
            throw;
        }
    }

    private static IReadOnlyList<Migration> LoadMigrations()
    {
        var asm = typeof(MigrationRunner).Assembly;
        var resources = asm.GetManifestResourceNames()
            .Where(n => n.Contains(".migrations.") && n.EndsWith(".sql", StringComparison.OrdinalIgnoreCase))
            .OrderBy(n => n, StringComparer.Ordinal)
            .ToList();

        var list = new List<Migration>();
        foreach (var res in resources)
        {
            // Resource names look like: Hermes.Memory.Core.migrations.0001_agent_memory.sql
            var fileName = res[(res.LastIndexOf(".migrations.", StringComparison.Ordinal) + ".migrations.".Length)..];
            var version = fileName[..fileName.IndexOf('_')];
            var name = fileName[(fileName.IndexOf('_') + 1)..^4];
            using var stream = asm.GetManifestResourceStream(res)
                ?? throw new InvalidOperationException($"Could not open migration resource: {res}");
            using var reader = new StreamReader(stream, Encoding.UTF8);
            var sql = reader.ReadToEnd();
            list.Add(new Migration(version, name, sql, Checksum(sql)));
        }
        return list;
    }

    private static string Checksum(string sql)
    {
        var bytes = SHA256.HashData(Encoding.UTF8.GetBytes(sql));
        return Convert.ToHexString(bytes)[..16];
    }
}

public sealed record Migration(string Version, string Name, string Sql, string Checksum);
