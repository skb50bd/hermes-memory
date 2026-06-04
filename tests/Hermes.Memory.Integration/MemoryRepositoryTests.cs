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
        .WithImage("ghcr.io/skb50bd/hermes-postgres:dev")
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

            var schemaPath = FindSchemaFile();
            var sql = File.ReadAllText(schemaPath);
            await using (var conn = new NpgsqlConnection(raw))
            {
                await conn.OpenAsync();
                await using (var cmd = new NpgsqlCommand(sql, conn))
                {
                    await cmd.ExecuteNonQueryAsync();
                }
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
            tags: new[] { "preferences", "databases" },
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
        var id = await _repo.RememberAsync($"delete me ephemeral test content ({unique})", tags: new[] { "ephemeral" });
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
