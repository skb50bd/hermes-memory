using System.Text.Json;
using Hermes.Memory.Core.Db;
using Hermes.Memory.Core.Embeddings;
using Hermes.Memory.Core.Memory;
using Microsoft.Extensions.Logging.Abstractions;
using Npgsql;
using Testcontainers.PostgreSql;
using Xunit;

namespace Hermes.Memory.Integration;

public sealed class MemoryRepositoryTests : IAsyncLifetime
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
    private MemoryRepository _repo = null!;
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

            var schemaFile = FindSchemaFile();
            // \ir in 01-schemas.sql is a psql meta-command — Npgsql can't execute
            // those. Inline the migration files instead. The schema file lives
            // at <repo>/docker/postgres/bin/01-schemas.sql and \ir references
            // ../../../migrations/*.sql, so migrations is 3 dirs up.
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
            // 01-schemas.sql adds a unique index that the per-file migrations
            // don't include (it's defined separately as a post-step in the
            // bootstrap). The Duplicate_Remember test depends on it.
            inlineSql.AppendLine("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_content_source
                    ON agent_memory.memories (md5(content), COALESCE(source, ''))
                    WHERE deleted_at IS NULL;
                """);
            await using var conn = new NpgsqlConnection(raw);
            await conn.OpenAsync();
            foreach (var ext in new[] { "vector", "pg_trgm", "ltree" })
            {
                await using var extCmd = new NpgsqlCommand($"CREATE EXTENSION IF NOT EXISTS \"{ext}\"", conn);
                await extCmd.ExecuteNonQueryAsync();
            }
            await using var cmd = new NpgsqlCommand(inlineSql.ToString(), conn);
            await cmd.ExecuteNonQueryAsync();
        }

        _ds = new NpgsqlDataSourceBuilder(raw).Build();
        _hermes = new HermesDataSource(raw, NullLogger<HermesDataSource>.Instance);
        _embedders = new EmbedderRegistry(NullLogger<EmbedderRegistry>.Instance,
            (dim, provider, model, _, _) => Task.FromResult(new HermesEmbedder(
                dim: dim, provider: provider, model: model, baseUrl: null, apiKey: null,
                cacheDir: Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N")),
                failOpen: true, logger: NullLogger.Instance)));
        await _embedders.InitializeAsync(_ds, default);

        _repo = new MemoryRepository(_hermes, _embedders);
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

    [Fact]
    public async Task Remember_Then_Search_Finds_It()
    {
        var unique = Guid.NewGuid().ToString("N")[..8];
        var id = await _repo.RememberAsync(
            content: $"User prefers Postgres over MySQL for new projects ({unique})",
            tags: ["preferences", "databases"],
            category: "preferences",
            source: "test");
        Assert.True(id > 0);

        var hits = await _repo.SearchAsync($"Postgres MySQL projects {unique}", topK: 5);
        Assert.NotEmpty(hits);
        Assert.Contains(hits, h => h.Id == id);
    }

    [Fact]
    public async Task Forget_Sets_DeletedAt_And_Excludes_From_Search()
    {
        var unique = Guid.NewGuid().ToString("N")[..8];
        var id = await _repo.RememberAsync($"delete me ephemeral test content ({unique})", tags: ["ephemeral"]);
        Assert.True(id > 0);
        Assert.True(await _repo.ForgetAsync(id));
        var hits = await _repo.SearchAsync($"delete me ephemeral {unique}", topK: 5);
        Assert.DoesNotContain(hits, h => h.Id == id);
    }

    [Fact]
    public async Task Duplicate_Remember_Returns_Zero()
    {
        var unique = $"unique content {Guid.NewGuid()}";
        var id1 = await _repo.RememberAsync(unique, source: "test");
        var id2 = await _repo.RememberAsync(unique, source: "test");
        Assert.NotEqual(0, id1);
        Assert.Equal(0, id2);
    }

    [Fact]
    public async Task Stats_Reports_Live_Count()
    {
        var unique = Guid.NewGuid().ToString("N")[..8];
        await _repo.RememberAsync($"stats test memory ({unique})", source: "test");
        var stats = await _repo.GetStatsAsync();
        Assert.True(stats.Live >= 1);
    }
}
