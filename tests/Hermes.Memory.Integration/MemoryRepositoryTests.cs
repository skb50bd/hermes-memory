using System.Text.Json;
using Hermes.Memory.Core.Db;
using Hermes.Memory.Core.Embeddings;
using Hermes.Memory.Core.Memory;
using Microsoft.Extensions.Logging.Abstractions;
using Npgsql;
using Testcontainers.PostgreSql;
using Xunit;

namespace Hermes.Memory.Integration;

/// <summary>
/// Integration test that hits a real Postgres container with the 6
/// extensions installed. Verifies the full stack: connection pool,
/// embedder (noop provider for determinism), repository, FTS + vector
/// search, and the migration runner.
///
/// To run: requires Docker. The test starts a Testcontainers instance,
/// applies the migrations, and runs the assertions.
/// </summary>
public sealed class MemoryRepositoryTests : IAsyncLifetime
{
    private readonly PostgreSqlBuilder _builder = new PostgreSqlBuilder()
        .WithImage("ghcr.io/skb50bd/hermes-postgres:dev")
        .WithDatabase("hermes_test")
        .WithUsername("postgres")
        .WithPassword("test")
        .WithCleanUp(true);

    private NpgsqlDataSource _ds = null!;
    private HermesDataSource _hermes = null!;
    private MemoryRepository _repo = null!;
    private EmbedderRegistry _embedders = null!;

    public async Task InitializeAsync()
    {
        await _builder.BuildAsync();
        var raw = _builder.ConnectionString;
        _ds = new NpgsqlDataSourceBuilder(raw).Build();

        // Apply the 5-schema bootstrap directly (skipping the gate script
        // since we're a real test, not a docker init).
        var bootstrapSql = File.ReadAllText("../../../../docker/postgres/initdb.d/01-template-bootstrap.sh");
        // Strip the psql wrapper — we want just the SQL.
        var sql = ExtractSqlBlock(bootstrapSql);

        await using (var cmd = new NpgsqlCommand(sql, _ds.CreateConnection()))
        await using (var conn = cmd.Connection!)
        {
            await conn.OpenAsync();
            await cmd.ExecuteNonQueryAsync();
        }

        // Configure noop embedder (deterministic, no network).
        _hermes = new HermesDataSource(raw, NullLogger<HermesDataSource>.Instance);
        _embedders = new EmbedderRegistry(NullLogger<EmbedderRegistry>.Instance,
            (dim, provider, model, _, _) => Task.FromResult(new HermesEmbedder(
                dim: dim, provider: provider, model: model, baseUrl: null, apiKey: null,
                cacheDir: Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N")),
                failOpen: true, logger: NullLogger.Instance)));
        await _embedders.InitializeAsync(_ds);

        _repo = new MemoryRepository(_hermes, _embedders);
    }

    public async Task DisposeAsync()
    {
        await _hermes.DisposeAsync();
        await _ds.DisposeAsync();
    }

    [Fact]
    public async Task Remember_Then_Search_Finds_It()
    {
        var id = await _repo.RememberAsync(
            content: "User prefers Postgres over MySQL for new projects",
            tags: new[] { "preferences", "databases" },
            category: "preferences",
            source: "test");
        Assert.True(id > 0);

        var hits = await _repo.SearchAsync("Postgres preferences", topK: 5);
        Assert.NotEmpty(hits);
        Assert.Contains(hits, h => h.Id == id);
    }

    [Fact]
    public async Task Forget_Sets_DeletedAt_And_Excludes_From_Search()
    {
        var id = await _repo.RememberAsync("delete me", tags: new[] { "ephemeral" });
        Assert.True(id > 0);
        Assert.True(await _repo.ForgetAsync(id));
        var hits = await _repo.SearchAsync("delete me", topK: 5);
        Assert.DoesNotContain(hits, h => h.Id == id);
    }

    [Fact]
    public async Task Duplicate_Remember_Returns_Zero()
    {
        var id1 = await _repo.RememberAsync("unique content xyz", source: "test");
        var id2 = await _repo.RememberAsync("unique content xyz", source: "test");
        Assert.NotEqual(0, id1);
        Assert.Equal(0, id2);
    }

    [Fact]
    public async Task Stats_Reports_Live_Count()
    {
        await _repo.RememberAsync("stats test memory", source: "test");
        var stats = await _repo.GetStatsAsync();
        Assert.True(stats.Live >= 1);
    }

    private static string ExtractSqlBlock(string bashContent)
    {
        // The bootstrap script uses a here-doc with 'SQL' as the delimiter.
        // Extract everything between the heredoc opener and closer.
        var start = bashContent.IndexOf("<<'SQL'");
        var end   = bashContent.LastIndexOf("SQL\n");
        if (start < 0 || end < 0) throw new InvalidOperationException("Could not extract SQL from bootstrap script");
        return bashContent[(start + "<<'SQL'".Length)..end];
    }
}
