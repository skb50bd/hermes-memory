using System.Collections.Concurrent;
using System.Net.Http.Json;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;

namespace Hermes.Memory.Core.Embeddings;

/// <summary>
/// Per-dim embedder. One HermesEmbedder instance per (provider, model)
/// combination, configured at startup from the agent_memory.models table.
/// Cache-first, fail-open, no reflection.
///
/// Fail-open zero-vector policy: if the provider errors, we substitute
/// a zero vector and return success. We do NOT cache the zero vector —
/// a transient 401 / 429 / DNS hiccup must not poison the disk cache
/// for the rest of the process's lifetime.
/// </summary>
public sealed class HermesEmbedder
{
    public int Dim { get; }
    public string Provider { get; }
    public string Model { get; }

    private readonly HttpClient _http;
    private readonly string _baseUrl;
    private readonly string? _apiKey;
    private readonly string _cacheDir;
    private readonly bool _failOpen;
    private readonly ILogger _logger;

    // Per-process in-memory cache. Skips the disk round-trip when
    // the same text is embedded twice in a session.
    private readonly ConcurrentDictionary<string, float[]> _memCache = new();

    // Stats — surfaced via memory_status tool and preflight
    private long _hits, _misses, _errors, _zeroFallbacks;
    public EmbedderStats Stats => new(_hits, _misses, _errors, _zeroFallbacks);

    public HermesEmbedder(int dim, string provider, string model, string? baseUrl, string? apiKey, string cacheDir, bool failOpen, ILogger logger, HttpClient? http = null)
    {
        Dim = dim;
        Provider = provider;
        Model = model;
        _baseUrl = baseUrl ?? DefaultBaseUrl(provider);
        _apiKey = apiKey;
        _cacheDir = Path.Combine(cacheDir, dim.ToString());
        _failOpen = failOpen;
        _logger = logger;
        _http = http ?? new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
        Directory.CreateDirectory(_cacheDir);
    }

    /// <summary>
    /// Embed one text. Returns a real vector from the provider, or a
    /// zero vector on failure if fail-open is enabled. Never throws on
    /// provider errors; the caller decides what to do with zero vectors.
    /// </summary>
    public async Task<float[]> EmbedAsync(string text, CancellationToken ct = default)
    {
        var key = CacheKey(text);

        // 1. In-memory cache
        if (_memCache.TryGetValue(key, out var cached))
        {
            Interlocked.Increment(ref _hits);
            return cached;
        }

        // 2. Disk cache
        var diskPath = DiskPath(key);
        if (File.Exists(diskPath))
        {
            try
            {
                var fromDisk = await ReadDiskAsync(diskPath, ct);
                _memCache[key] = fromDisk;
                Interlocked.Increment(ref _hits);
                return fromDisk;
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "Disk cache read failed for {Key}, will re-embed", key);
            }
        }

        // 3. Network
        Interlocked.Increment(ref _misses);
        float[] vec;
        bool usedFallback = false;
        try
        {
            vec = await _EmbedLiveAsync(text, ct);
        }
        catch (Exception ex) when (_failOpen)
        {
            Interlocked.Increment(ref _errors);
            Interlocked.Increment(ref _zeroFallbacks);
            _logger.LogWarning(ex, "Embedder provider error, falling back to zero vector");
            vec = new float[Dim];
            usedFallback = true;
        }

        // Guard against caching the fail-open zero vector. A real zero
        // from the noop provider is intentional and IS cached.
        if (!usedFallback || Provider == "noop")
        {
            _memCache[key] = vec;
            try
            {
                await WriteDiskAsync(diskPath, vec, ct);
            }
            catch (Exception ex)
            {
                _logger.LogDebug(ex, "Disk cache write failed for {Key}", key);
            }
        }

        return vec;
    }

    public async Task<IReadOnlyList<float[]>> EmbedBatchAsync(IReadOnlyList<string> texts, CancellationToken ct = default)
    {
        var results = new float[texts.Count][];
        for (int i = 0; i < texts.Count; i++)
        {
            results[i] = await EmbedAsync(texts[i], ct);
        }
        return results;
    }

    private async Task<float[]> _EmbedLiveAsync(string text, CancellationToken ct)
    {
        return Provider switch
        {
            "kimi" => await EmbedOpenAiCompatAsync(text, ct),
            "ollama_local" => await EmbedOllamaAsync(text, ct),
            "openai" => await EmbedOpenAiCompatAsync(text, ct),
            "noop" => new float[Dim],
            _ => throw new NotSupportedException($"Embedder provider '{Provider}' is not registered.")
        };
    }

    private async Task<float[]> EmbedOpenAiCompatAsync(string text, CancellationToken ct)
    {
        using var req = new HttpRequestMessage(HttpMethod.Post, $"{_baseUrl.TrimEnd('/')}/embeddings");
        if (_apiKey is not null) req.Headers.Authorization = new("Bearer", _apiKey);
        req.Content = JsonContent.Create(new { model = Model, input = text });
        using var resp = await _http.SendAsync(req, ct);
        resp.EnsureSuccessStatusCode();
        var body = await resp.Content.ReadFromJsonAsync<OpenAiEmbeddingResponse>(cancellationToken: ct);
        var vec = body?.Data?.FirstOrDefault()?.Embedding
            ?? throw new InvalidOperationException("Embedding response had no data");
        if (vec.Length != Dim) throw new InvalidOperationException($"Provider returned dim={vec.Length}, expected {Dim}");
        return vec;
    }

    private async Task<float[]> EmbedOllamaAsync(string text, CancellationToken ct)
    {
        // Ollama /api/embed (newer) vs /api/embeddings (older). We try the
        // new endpoint first; the old endpoint returns {embedding: [...]} (singular).
        using var req = new HttpRequestMessage(HttpMethod.Post, $"{_baseUrl.TrimEnd('/')}/api/embed");
        req.Content = JsonContent.Create(new { model = Model, input = text });
        using var resp = await _http.SendAsync(req, ct);
        resp.EnsureSuccessStatusCode();
        var body = await resp.Content.ReadFromJsonAsync<OllamaEmbedResponse>(cancellationToken: ct);
        var vec = body?.Embeddings?.FirstOrDefault()
            ?? throw new InvalidOperationException("Ollama response had no embeddings");
        if (vec.Length != Dim) throw new InvalidOperationException($"Ollama returned dim={vec.Length}, expected {Dim}");
        return vec;
    }

    private static string DefaultBaseUrl(string provider) => provider switch
    {
        "kimi" => "https://api.kimi.com/coding/v1",
        // Ollama local port: regular + 5000 = 16434. Override with
        // HERMES_OLLAMA_HOST_PORT. Falls back to 11434 (legacy).
        "ollama_local" => $"http://localhost:{ResolveOllamaPort()}",
        "openai" => "https://api.openai.com/v1",
        // "noop" is a test/local provider that returns zero vectors without
        // hitting the network. It needs a non-null baseUrl only because the
        // embedder HTTP code path dereferences it; an empty string is the
        // cheapest sentinel that satisfies the type without a real endpoint.
        "noop" => string.Empty,
        _ => throw new NotSupportedException($"No default base URL for provider '{provider}'")
    };

    private static int ResolveOllamaPort()
    {
        // Convention: regular port + 5000 = 16434. HERMES_OLLAMA_HOST_PORT
        // wins. Falls back to 11434 (legacy) for back-compat.
        var raw = Environment.GetEnvironmentVariable("HERMES_OLLAMA_HOST_PORT");
        if (!string.IsNullOrEmpty(raw) && int.TryParse(raw, out var p)) return p;
        return 11434;
    }

    private string CacheKey(string text)
    {
        // Cache key includes provider+model so a model switch invalidates
        // the right entries automatically.
        var raw = $"{Provider}|{Model}|{text}";
        var hash = SHA256.HashData(Encoding.UTF8.GetBytes(raw));
        return Convert.ToHexString(hash);
    }

    private string DiskPath(string key) =>
        Path.Combine(_cacheDir, key[..2], key + ".json");

    private static async Task<float[]> ReadDiskAsync(string path, CancellationToken ct)
    {
        await using var fs = File.OpenRead(path);
        var raw = await JsonSerializer.DeserializeAsync<float[]>(fs, cancellationToken: ct);
        return raw ?? throw new InvalidDataException("Empty cache file");
    }

    private static async Task WriteDiskAsync(string path, float[] vec, CancellationToken ct)
    {
        var dir = Path.GetDirectoryName(path)!;
        Directory.CreateDirectory(dir);
        await using var fs = File.Create(path);
        await JsonSerializer.SerializeAsync(fs, vec, cancellationToken: ct);
    }
}

public sealed record EmbedderStats(long Hits, long Misses, long Errors, long ZeroFallbacks);

// --- AOT-friendly HTTP response DTOs ---
// These are deserialized via System.Text.Json reflection-free source-gen.
// Add new ones to Models/JsonSerializerContext.cs if they grow.
public sealed class OpenAiEmbeddingResponse
{
    [JsonPropertyName("data")] public OpenAiEmbeddingDatum[]? Data { get; set; }
}
public sealed class OpenAiEmbeddingDatum
{
    [JsonPropertyName("embedding")] public float[]? Embedding { get; set; }
}
public sealed class OllamaEmbedResponse
{
    [JsonPropertyName("embeddings")] public float[][]? Embeddings { get; set; }
}
