using System.ComponentModel;
using System.Text.Json;
using Hermes.Memory.Core.Wiki;
using ModelContextProtocol.Server;

namespace Hermes.Memory.Core.Mcp;

[McpServerToolType]
public sealed class WikiTools(WikiRepository repo)
{
    private readonly WikiRepository _repo = repo;

    [McpServerTool(Name = "wiki_create"), Description("Create or update a wiki document by slug. Idempotent on slug.")]
    public async Task<string> Create(
        [Description("Unique URL slug, e.g. 'platform-overview'")] string slug,
        [Description("Document title")] string title,
        [Description("Markdown body")] string body_md,
        [Description("Optional ltree category")] string? category = null,
        [Description("Optional tags")] string[]? tags = null,
        CancellationToken ct = default)
    {
        var id = await _repo.UpsertAsync(slug, title, body_md, category, tags, ct);
        return $"Document {slug} stored with id {id}";
    }

    [McpServerTool(Name = "wiki_read"), Description("Read a wiki document by slug.")]
    public async Task<string> Read(
        [Description("Document slug")] string slug,
        CancellationToken ct = default)
    {
        var doc = await _repo.ReadAsync(slug, ct);
        if (doc is null) return $"No document with slug '{slug}'";
        return JsonSerializer.Serialize(doc, JsonOpts);
    }

    [McpServerTool(Name = "wiki_link"), Description("Add a wikilink from one document to another. Both slugs must exist.")]
    public async Task<string> Link(
        [Description("Source document slug")] string source_slug,
        [Description("Target document slug")] string target_slug,
        [Description("Optional context (the sentence that contained the link)")] string? context = null,
        CancellationToken ct = default)
    {
        var ok = await _repo.LinkAsync(source_slug, target_slug, context, ct);
        return ok ? $"Linked {source_slug} -> {target_slug}" : $"Could not link — check both slugs exist";
    }

    [McpServerTool(Name = "wiki_backlinks"), Description("Find every document that links TO the given slug.")]
    public async Task<string> Backlinks(
        [Description("Target document slug")] string slug,
        CancellationToken ct = default)
    {
        var links = await _repo.BacklinksAsync(slug, ct);
        return JsonSerializer.Serialize(links, JsonOpts);
    }

    [McpServerTool(Name = "wiki_related"), Description("Walk the wikilink graph N hops from a document. Returns related docs ordered by depth.")]
    public async Task<string> Related(
        [Description("Document slug to start from")] string slug,
        [Description("Max hop depth (default 2)")] int max_hops = 2,
        [Description("Max results (default 10)")] int top_k = 10,
        CancellationToken ct = default)
    {
        var related = await _repo.RelatedAsync(slug, max_hops, top_k, ct);
        return JsonSerializer.Serialize(related, JsonOpts);
    }

    [McpServerTool(Name = "wiki_search"), Description("Hybrid FTS + vector search over wiki documents.")]
    public async Task<string> Search(
        [Description("The search query")] string query,
        [Description("Max results (default 10)")] int top_k = 10,
        CancellationToken ct = default)
    {
        var hits = await _repo.SearchAsync(query, top_k, ct);
        return JsonSerializer.Serialize(hits, JsonOpts);
    }

    private static readonly JsonSerializerOptions JsonOpts = new() { WriteIndented = true };
}
