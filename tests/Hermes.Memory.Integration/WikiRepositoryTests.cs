using Hermes.Memory.Core.Db;
using Hermes.Memory.Core.Embeddings;
using Hermes.Memory.Core.Wiki;
using Microsoft.Extensions.Logging.Abstractions;
using Npgsql;
using Testcontainers.PostgreSql;
using Xunit;

namespace Hermes.Memory.Integration;

/// <summary>
/// Regression tests for issue #1: C# MCP `wiki_search` fails with 42703
/// (column "vector_1024" does not exist) on the CTE.
///
/// Root cause: the `fts_candidates` CTE only projected `id, slug, title,
/// body_md, text_rank` — but the outer SELECT referenced `vector_1024`
/// from the CTE. The vector lived in the base table, never in the CTE.
///
/// Fix: add `vector_1024` to the CTE projection.
/// </summary>
public sealed class WikiRepositoryTests : IAsyncLifetime
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
    private WikiRepository _repo = null!;
    private EmbedderRegistry _embedders = null!;

    private static string FindSchemaFile()
    {
        // In the test output (set up by Hermes.Memory.Integration.csproj
        // <None Include ... CopyToOutputDirectory="PreserveNewest"/>):
        var output = AppContext.BaseDirectory;
        var paths = new[]
        {
            Path.Combine(output, "schema", "01-schemas.sql"),
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

            // The schema file and migrations are both copied to the test
            // output by Hermes.Memory.Integration.csproj (CopyToOutputDirectory).
            // schema/01-schemas.sql sits next to migrations/*.sql in
            // AppContext.BaseDirectory.
            var schemaFile = FindSchemaFile();
            var migrationsDir = Path.Combine(AppContext.BaseDirectory, "migrations");
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

        _repo = new WikiRepository(_hermes, _embedders);
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
    /// Regression for issue #1: search must not fail with
    /// "column vector_1024 does not exist" when the CTE omits it.
    /// </summary>
    [Fact]
    public async Task Search_On_Empty_Database_Returns_Empty_Without_Raising()
    {
        // Before the fix, this raised:
        //   Npgsql.PostgresException: 42703: column "vector_1024" does not exist
        // because the fts_candidates CTE never projected vector_1024.
        var hits = await _repo.SearchAsync("anything", topK: 5);
        Assert.NotNull(hits);
        Assert.Empty(hits);
    }

    /// <summary>
    /// End-to-end: create a doc, search for a term in its body, expect to find it.
    /// Exercises the full hybrid FTS + vector path.
    /// </summary>
    [Fact]
    public async Task Create_Then_Search_Finds_The_Document()
    {
        var unique = Guid.NewGuid().ToString("N")[..8];
        var slug = $"test-doc-{unique}";
        var body = $"This document mentions martian-{unique} in its body for FTS purposes.";

        var id = await _repo.UpsertAsync(slug, "Test Doc", body);
        Assert.True(id > 0);

        // Search for the unique token
        var hits = await _repo.SearchAsync($"martian-{unique}", topK: 5);
        Assert.Contains(hits, h => h.Slug == slug);
    }
}
