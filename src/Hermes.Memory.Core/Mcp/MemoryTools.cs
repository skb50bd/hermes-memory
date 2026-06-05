using System.ComponentModel;
using System.Text.Json;
using Hermes.Memory.Core.Embeddings;
using Hermes.Memory.Core.Memory;
using ModelContextProtocol.Server;

namespace Hermes.Memory.Core.Mcp;

/// <summary>
/// MCP tools for the memory surface. Each method becomes a tool
/// available to the agent over stdio. Method names are snake_cased
/// via the [Description] attribute; the MCP SDK derives the wire
/// name from the method name.
/// </summary>
[McpServerToolType]
public sealed class MemoryTools(MemoryRepository repo, EmbedderRegistry embedders)
{
    private readonly MemoryRepository _repo = repo;
    private readonly EmbedderRegistry _embedders = embedders;

    [McpServerTool(Name = "memory_remember"), Description("Store a memory. Idempotent on (content, source). Returns the memory id (0 = duplicate).")]
    public async Task<string> Remember(
        [Description("The content of the memory")] string content,
        [Description("Optional tags to attach")] string[]? tags = null,
        [Description("Optional ltree category (e.g. 'projects.sportsverse')")] string? category = null,
        [Description("Optional source identifier (e.g. 'mcp:wiki:platform-overview')")] string? source = null,
        CancellationToken ct = default)
    {
        var id = await _repo.RememberAsync(content, tags, category, source, ct: ct);
        return id > 0 ? $"Stored memory {id}" : "Memory already exists (deduped)";
    }

    [McpServerTool(Name = "memory_search"), Description("Hybrid FTS + vector search over memories. Empty result is valid — try a rephrasing.")]
    public async Task<string> Search(
        [Description("The search query")] string query,
        [Description("Max results to return (default 10)")] int top_k = 10,
        [Description("Weight of FTS rank vs cosine (0..1, default 0.5)")] float hybrid_text_weight = 0.5f,
        CancellationToken ct = default)
    {
        var hits = await _repo.SearchAsync(query, top_k, hybrid_text_weight, ct);
        return JsonSerializer.Serialize(hits, JsonOpts);
    }

    [McpServerTool(Name = "memory_forget"), Description("Soft-delete a memory by id. Sets deleted_at; row is excluded from search.")]
    public async Task<string> Forget(
        [Description("Memory id to delete")] long id,
        CancellationToken ct = default)
    {
        var ok = await _repo.ForgetAsync(id, ct);
        return ok ? $"Forgot memory {id}" : $"Memory {id} not found or already deleted";
    }

    [McpServerTool(Name = "memory_status"), Description("Memory table stats + embedder cache stats. Use this to verify embeddings are working.")]
    public async Task<string> Status(CancellationToken ct = default)
    {
        var stats = await _repo.GetStatsAsync(ct);
        var embedderStats = _embedders.GetDefault().Stats;
        return JsonSerializer.Serialize(new { memory = stats, embedder = embedderStats, defaultDim = _embedders.DefaultDim }, JsonOpts);
    }

    private static readonly JsonSerializerOptions JsonOpts = new() { WriteIndented = true };
}
