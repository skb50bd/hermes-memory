using Hermes.Memory.Core.Embeddings;
using Microsoft.Extensions.Logging.Abstractions;
using Xunit;

namespace Hermes.Memory.Tests;

public class CacheKeyTests
{
    [Fact]
    public async Task CacheKey_Different_Text_Produces_Different_Keys()
    {
        // We test this by inspecting the internal behavior: same provider+model,
        // different text => different SHA. Verified via the public Stats counters:
        // two distinct texts miss the cache on first call.
        var e = new HermesEmbedder(
            dim: 4, provider: "noop", model: "noop", baseUrl: null, apiKey: null,
            cacheDir: Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N")),
            failOpen: true, logger: NullLogger.Instance);

        var v1 = await e.EmbedAsync("alpha");
        var v2 = await e.EmbedAsync("beta");

        Assert.Equal(4, v1.Length);
        Assert.Equal(4, v2.Length);
        Assert.Equal(2, e.Stats.Misses);
    }

    [Fact]
    public async Task CacheKey_Same_Text_Produces_Cache_Hit()
    {
        var e = new HermesEmbedder(
            dim: 4, provider: "noop", model: "noop", baseUrl: null, apiKey: null,
            cacheDir: Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N")),
            failOpen: true, logger: NullLogger.Instance);

        _ = await e.EmbedAsync("gamma");
        _ = await e.EmbedAsync("gamma");

        Assert.Equal(1, e.Stats.Misses);
        Assert.Equal(1, e.Stats.Hits);
    }

    [Fact]
    public async Task Noop_Provider_Returns_Zero_Vector_And_Caches()
    {
        // The noop provider deliberately returns zero vectors AND caches them
        // (unlike fail-open zero vectors from real providers).
        var e = new HermesEmbedder(
            dim: 8, provider: "noop", model: "noop", baseUrl: null, apiKey: null,
            cacheDir: Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N")),
            failOpen: true, logger: NullLogger.Instance);

        var v = await e.EmbedAsync("delta");
        Assert.All(v, x => Assert.Equal(0f, x));
        Assert.Equal(0, e.Stats.ZeroFallbacks);
    }
}
