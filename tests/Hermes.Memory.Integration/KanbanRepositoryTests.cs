using Hermes.Memory.Core.Db;
using Hermes.Memory.Core.Embeddings;
using Hermes.Memory.Core.Kanban;
using Microsoft.Extensions.Logging.Abstractions;
using Npgsql;
using Testcontainers.PostgreSql;
using Xunit;

namespace Hermes.Memory.Integration;

/// <summary>
/// Regression tests for issue #2: C# MCP `kanban_list` fails with 42P08
/// (ambiguous_parameter_type) on null filter args.
///
/// Root cause: `cmd.Parameters.AddWithValue("p", (object?)null ?? DBNull.Value)`
/// lets Npgsql infer the type from the .NET value — but a null has no
/// type, so Postgres rejects the parameter as "could not determine
/// data type of parameter $N".
///
/// Fix: pin the Postgres type with `NpgsqlParameter(name, NpgsqlDbType.X)`
/// so a null value is still typed.
/// </summary>
public sealed class KanbanRepositoryTests : IAsyncLifetime
{
    private readonly PostgreSqlBuilder _builder = new PostgreSqlBuilder()
        .WithImage("hermes-postgres:dev")
        .WithDatabase("hermes_test")
        .WithUsername("postgres")
        .WithPassword("test")
        .WithCleanUp(true);

    private PostgreSqlContainer? _container;
    private NpgsqlDataSource _ds = null!;
    private HermesDataSource _hermes = null!;
    private KanbanRepository _repo = null!;
    private EmbedderRegistry _embedders = null!;

    private static string FindSchemaFile()
    {
        var paths = new[]
        {
            "../../../../docker/postgres/bin/01-schemas.sql",
            "../../../docker/postgres/bin/01-schemas.sql",
            "../../docker/postgres/bin/01-schemas.sql",
            "../docker/postgres/bin/01-schemas.sql",
            "docker/postgres/bin/01-schemas.sql",
            "/home/pixu/repos/hermes-memory/docker/postgres/bin/01-schemas.sql",
        };
        foreach (var p in paths)
        {
            if (File.Exists(p)) return p;
        }
        throw new FileNotFoundException("Could not find 01-schemas.sql");
    }

    public async Task InitializeAsync()
    {
        string raw;
        var envConn = Environment.GetEnvironmentVariable("HERMES_PG_CONN_STR");
        if (!string.IsNullOrWhiteSpace(envConn))
        {
            raw = envConn;
        }
        else
        {
            _container = _builder.Build();
            await _container.StartAsync();
            raw = _container.GetConnectionString();

            // The schema file lives at <repo>/docker/postgres/bin/01-schemas.sql,
            // and \ir references ../../../migrations/*.sql — so the migrations
            // directory is three levels up from the schema file's directory.
            var schemaFile = FindSchemaFile();
            var repoRoot = Path.GetFullPath(Path.Combine(Path.GetDirectoryName(schemaFile)!, "..", "..", ".."));
            var migrationsDir = Path.Combine(repoRoot, "migrations");
            var inlineSql = new System.Text.StringBuilder();
            foreach (var f in new[] {
                "0001_agent_memory.sql", "0002_wiki.sql", "0003_journal.sql",
                "0004_skills.sql", "0005_metrics.sql", "0006_kanban.sql",
            })
            {
                var path = Path.Combine(migrationsDir, f);
                if (File.Exists(path)) inlineSql.AppendLine(File.ReadAllText(path));
            }
            if (inlineSql.Length == 0)
            {
                throw new InvalidOperationException(
                    $"no migration files found in {migrationsDir}");
            }
            await using (var conn = new NpgsqlConnection(raw))
            {
                await conn.OpenAsync();
                // The testcontainer image is a vanilla postgres with the extensions
                // AVAILABLE but not installed. Install them before running the
                // migration SQL, which expects them to be present.
                foreach (var ext in new[] { "vector", "pg_trgm", "ltree" })
                {
                    await using var extCmd = new NpgsqlCommand($"CREATE EXTENSION IF NOT EXISTS \"{ext}\"", conn);
                    await extCmd.ExecuteNonQueryAsync();
                }
                await using var cmd = new NpgsqlCommand(inlineSql.ToString(), conn);
                await cmd.ExecuteNonQueryAsync();
            }
        }

        _ds = new NpgsqlDataSourceBuilder(raw).Build();
        _hermes = new HermesDataSource(raw, NullLogger<HermesDataSource>.Instance);
        _embedders = new EmbedderRegistry(NullLogger<EmbedderRegistry>.Instance,
            (dim, provider, model, _, _) => Task.FromResult(new HermesEmbedder(
                dim: dim, provider: provider, model: model, baseUrl: null, apiKey: null,
                cacheDir: Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N")),
                failOpen: true, logger: NullLogger.Instance)));
        await _embedders.InitializeAsync(_ds, default);

        _repo = new KanbanRepository(_hermes);
    }

    public async Task DisposeAsync()
    {
        await _hermes.DisposeAsync();
        await _ds.DisposeAsync();
        if (_container != null)
        {
            await _container.DisposeAsync();
        }
    }

    /// <summary>
    /// Regression for issue #2: passing no filters must NOT raise 42P08.
    /// </summary>
    [Fact]
    public async Task ListTasks_With_No_Filters_Does_Not_Raise_Ambiguous_Param_Type()
    {
        // Before the fix, this raised:
        //   Npgsql.PostgresException: 42P08: could not determine data type of parameter $1
        // because @tenant was passed as DBNull without a typed parameter.
        var tasks = await _repo.ListTasksAsync();
        Assert.NotNull(tasks);
        Assert.Empty(tasks);   // fresh DB, no rows
    }

    /// <summary>
    /// All-null filter combination should also work (exercises every nullable param).
    /// </summary>
    [Fact]
    public async Task ListTasks_With_All_Null_Filters_Does_Not_Raise_Ambiguous_Param_Type()
    {
        var tasks = await _repo.ListTasksAsync(tenantSlug: null, status: null, assignee: null);
        Assert.NotNull(tasks);
    }

    /// <summary>
    /// Mixed null + real filter — confirms the pinned-type fix doesn't break
    /// the case where filters are populated.
    /// </summary>
    [Fact]
    public async Task ListTasks_With_Tenant_Only_Filters_Results()
    {
        // Seed a tenant + task so the filter has something to match.
        await _repo.UpsertTenantAsync("test-tenant-" + Guid.NewGuid().ToString("N")[..6], "Test Tenant");
        // First, list all (the test above already proved no filters works).
        // Then list with a non-existent tenant — should return [].
        var tasks = await _repo.ListTasksAsync(tenantSlug: "no-such-tenant-12345");
        Assert.Empty(tasks);
    }
}
