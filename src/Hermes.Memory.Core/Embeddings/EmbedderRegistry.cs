using System.Collections.Concurrent;
using Microsoft.Extensions.Logging;
using Npgsql;

namespace Hermes.Memory.Core.Embeddings;

/// <summary>
/// Per-dim singleton registry. The plugin's runtime reads the
/// agent_memory.models and agent_memory.settings tables on init and
/// constructs one HermesEmbedder per supported dim. Subsequent calls
/// to <see cref="GetAsync"/> return the cached instance.
///
/// AOT-safe: the embedder constructors don't use reflection, and the
/// model/provider strings come from the SQL registry (not from a
/// hardcoded switch).
/// </summary>
public sealed class EmbedderRegistry
{
    private readonly ConcurrentDictionary<int, HermesEmbedder> _byDim = new();
    private readonly Func<int, string, string, string?, string?, Task<HermesEmbedder>> _factory;
    private readonly ILogger _logger;
    private int _defaultDim;

    public EmbedderRegistry(ILogger logger, Func<int, string, string, string?, string?, Task<HermesEmbedder>> factory)
    {
        _logger = logger;
        _factory = factory;
    }

    public int DefaultDim => _defaultDim;

    public async Task InitializeAsync(NpgsqlDataSource dataSource, CancellationToken ct)
    {
        await using var conn = await dataSource.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            SELECT s.default_dim, m.dim, m.provider, m.model, m.base_url, m.api_key_env
            FROM agent_memory.models m
            CROSS JOIN (SELECT (value #>> '{}')::int AS default_dim FROM agent_memory.settings WHERE key = 'default_dim') s
            """, conn);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        while (await reader.ReadAsync(ct))
        {
            _defaultDim = reader.GetInt32(0);
            var dim      = reader.GetInt32(1);
            var provider = reader.GetString(2);
            var model    = reader.GetString(3);
            var baseUrl  = reader.IsDBNull(4) ? null : reader.GetString(4);
            var apiKeyEnv = reader.IsDBNull(5) ? null : reader.GetString(5);
            var apiKey   = apiKeyEnv is null ? null : Environment.GetEnvironmentVariable(apiKeyEnv);
            if (apiKey is null && provider != "noop" && provider != "ollama_local")
            {
                _logger.LogWarning("No API key in env var {Env} for embedder {Provider}/{Model} (dim={Dim}); embeddings will fail-open to zero",
                    apiKeyEnv, provider, model, dim);
            }
            var embedder = await _factory(dim, provider, model, baseUrl, apiKey);
            _byDim[dim] = embedder;
            _logger.LogInformation("Embedder ready: dim={Dim} provider={Provider} model={Model}", dim, provider, model);
        }
    }

    public HermesEmbedder? Get(int dim) => _byDim.TryGetValue(dim, out var e) ? e : null;
    public HermesEmbedder GetDefault() => _byDim[_defaultDim];
}
