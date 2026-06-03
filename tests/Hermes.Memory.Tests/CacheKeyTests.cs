using Hermes.Memory.Core.Embeddings;
using Microsoft.Extensions.Logging.Abstractions;
using Xunit;

namespace Hermes.Memory.Tests;

public class CacheKeyTests
{
    [Fact]
    public void CacheKey_Different_Text_Produces_Different_Keys()
    {
        // We test this by inspecting the internal behavior: same provider+model,
        // different text => different SHA. Verified via the public Stats counters:
        // two distinct texts miss the cache on first call.
        var e = new HermesEmbedder(
            dim: 4, provider: "noop", model: "noop", baseUrl: null, apiKey: null,
            cacheDir: Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N")),
            failOpen: true, logger: NullLogger.Instance);

        var v1 = e.EmbedAsync("alpha").GetAwaiter().GetResult();
        var v2 = e.EmbedAsync("beta").GetAwaiter().GetResult();

        Assert.Equal(4, v1.Length);
        Assert.Equal(4, v2.Length);
        Assert.Equal(2, e.Stats.Misses);
    }

    [Fact]
    public void CacheKey_Same_Text_Produces_Cache_Hit()
    {
        var e = new HermesEmbedder(
            dim: 4, provider: "noop", model: "noop", baseUrl: null, apiKey: null,
            cacheDir: Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N")),
            failOpen: true, logger: NullLogger.Instance);

        _ = e.EmbedAsync("gamma").GetAwaiter().GetResult();
        _ = e.EmbedAsync("gamma").GetAwaiter().GetResult();

        Assert.Equal(1, e.Stats.Misses);
        Assert.Equal(1, e.Stats.Hits);
    }

    [Fact]
    public void Noop_Provider_Returns_Zero_Vector_And_Caches()
    {
        // The noop provider deliberately returns zero vectors AND caches them
        // (unlike fail-open zero vectors from real providers).
        var e = new HermesEmbedder(
            dim: 8, provider: "noop", model: "noop", baseUrl: null, apiKey: null,
            cacheDir: Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N")),
            failOpen: true, logger: NullLogger.Instance);

        var v = e.EmbedAsync("delta").GetAwaiter().GetResult();
        Assert.All(v, x => Assert.Equal(0f, x));
        Assert.Equal(0, e.Stats.ZeroFallbacks);
    }
}
