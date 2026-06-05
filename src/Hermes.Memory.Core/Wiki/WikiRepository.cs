using System.Text.Json;
using Hermes.Memory.Core.Db;
using Hermes.Memory.Core.Embeddings;
using Npgsql;
using NpgsqlTypes;

namespace Hermes.Memory.Core.Wiki;

/// <summary>
/// CRUD + link graph for hermes_wiki.documents. Slugs are unique URLs
/// (e.g. "platform-overview"). Wikilinks between documents are stored
/// in hermes_wiki.document_links and queryable via recursive CTE for
/// related-document traversal.
/// </summary>
public sealed class WikiRepository
{
    private readonly HermesDataSource _ds;
    private readonly EmbedderRegistry _embedders;

    public WikiRepository(HermesDataSource ds, EmbedderRegistry embedders)
    {
        _ds = ds;
        _embedders = embedders;
    }

    /// <summary>
    /// Create or update a document by slug. Idempotent: re-creating an
    /// existing slug updates the body, embedding, and updated_at.
    /// </summary>
    public async Task<long> UpsertAsync(string slug, string title, string bodyMd, string? category = null, string[]? tags = null, CancellationToken ct = default)
    {
        var embedder = _embedders.GetDefault();
        var vec = await embedder.EmbedAsync(bodyMd, ct);
        var dim = embedder.Dim;

        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            INSERT INTO hermes_wiki.documents (slug, title, body_md, vector_1024, category)
            VALUES (@slug, @title, @body, @vec, @category::ltree)
            ON CONFLICT (slug) DO UPDATE SET
                title = EXCLUDED.title,
                body_md = EXCLUDED.body_md,
                vector_1024 = EXCLUDED.vector_1024,
                category = EXCLUDED.category,
                updated_at = now()
            RETURNING id
            """, conn);
        cmd.Parameters.AddWithValue("slug", slug);
        cmd.Parameters.AddWithValue("title", title);
        cmd.Parameters.AddWithValue("body", bodyMd);
        cmd.Parameters.AddWithValue("vec", vec);
        cmd.Parameters.AddWithValue("category", (object?)category ?? DBNull.Value);
        var id = (long)(await cmd.ExecuteScalarAsync(ct))!;

        if (tags is { Length: > 0 })
        {
            await SyncTagsAsync(conn, id, tags, ct);
        }
        return id;
    }

    public async Task<WikiDocument?> ReadAsync(string slug, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            "SELECT id, slug, title, body_md, category, created_at, updated_at FROM hermes_wiki.documents WHERE slug = @slug",
            conn);
        cmd.Parameters.AddWithValue("slug", slug);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        if (!await reader.ReadAsync(ct)) return null;
        return new WikiDocument(
            Id: reader.GetInt64(0),
            Slug: reader.GetString(1),
            Title: reader.GetString(2),
            BodyMd: reader.GetString(3),
            Category: reader.IsDBNull(4) ? null : reader.GetString(4),
            CreatedAt: reader.GetFieldValue<DateTime>(5),
            UpdatedAt: reader.GetFieldValue<DateTime>(6));
    }

    /// <summary>
    /// Add a wikilink from source to target slug. Resolves slugs to ids
    /// inside a single transaction. If target doesn't exist, returns
    /// 0 (the caller can decide: create it, or just record an orphan link).
    /// </summary>
    public async Task<bool> LinkAsync(string sourceSlug, string targetSlug, string? context = null, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            INSERT INTO hermes_wiki.document_links (source_id, target_id, context)
            SELECT s.id, t.id, @context
            FROM hermes_wiki.documents s, hermes_wiki.documents t
            WHERE s.slug = @src AND t.slug = @tgt
            ON CONFLICT (source_id, target_id) DO UPDATE SET context = EXCLUDED.context
            """, conn);
        cmd.Parameters.AddWithValue("src", sourceSlug);
        cmd.Parameters.AddWithValue("tgt", targetSlug);
        cmd.Parameters.AddWithValue("context", (object?)context ?? DBNull.Value);
        return await cmd.ExecuteNonQueryAsync(ct) == 1;
    }

    /// <summary>
    /// Find every document that links TO the given slug. Backlinks are
    /// the single most useful query in a wiki — it's how Obsidian feels
    /// like a graph.
    /// </summary>
    public async Task<IReadOnlyList<WikiLinkRef>> BacklinksAsync(string targetSlug, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            SELECT s.id, s.slug, s.title, l.context
            FROM hermes_wiki.document_links l
            JOIN hermes_wiki.documents s ON s.id = l.source_id
            JOIN hermes_wiki.documents t ON t.id = l.target_id
            WHERE t.slug = @slug
            ORDER BY s.title
            """, conn);
        cmd.Parameters.AddWithValue("slug", targetSlug);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var results = new List<WikiLinkRef>();
        while (await reader.ReadAsync(ct))
        {
            results.Add(new WikiLinkRef(
                Id: reader.GetInt64(0),
                Slug: reader.GetString(1),
                Title: reader.GetString(2),
                Context: reader.IsDBNull(3) ? null : reader.GetString(3)));
        }
        return results;
    }

    /// <summary>
    /// Find documents related to the given slug via 2-hop link traversal.
    /// Recursive CTE walks source → link → target, deduplicates, and
    /// ranks by hop count. Returns the top N.
    /// </summary>
    public async Task<IReadOnlyList<WikiRelated>> RelatedAsync(string slug, int maxHops = 2, int topK = 10, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            WITH RECURSIVE walk AS (
                SELECT d.id, d.slug, d.title, 0 AS depth, ARRAY[d.id] AS path
                FROM hermes_wiki.documents d
                WHERE d.slug = @slug
                UNION ALL
                SELECT d.id, d.slug, d.title, w.depth + 1, w.path || d.id
                FROM walk w
                JOIN hermes_wiki.document_links l ON l.source_id = w.id
                JOIN hermes_wiki.documents d ON d.id = l.target_id
                WHERE w.depth < @max_hops AND NOT (d.id = ANY(w.path))
            )
            SELECT slug, title, MIN(depth) AS depth
            FROM walk
            WHERE depth > 0
            GROUP BY slug, title
            ORDER BY depth, title
            LIMIT @k
            """, conn);
        cmd.Parameters.AddWithValue("slug", slug);
        cmd.Parameters.AddWithValue("max_hops", maxHops);
        cmd.Parameters.AddWithValue("k", topK);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var results = new List<WikiRelated>();
        while (await reader.ReadAsync(ct))
        {
            results.Add(new WikiRelated(
                Slug: reader.GetString(0),
                Title: reader.GetString(1),
                Depth: reader.GetInt32(2)));
        }
        return results;
    }

    /// <summary>
    /// Hybrid search over document bodies. Same shape as memory search.
    /// </summary>
    public async Task<IReadOnlyList<WikiSearchHit>> SearchAsync(string query, int topK = 10, CancellationToken ct = default)
    {
        var embedder = _embedders.GetDefault();
        var qVec = await embedder.EmbedAsync(query, ct);

        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            WITH fts_candidates AS (
                SELECT id, slug, title, body_md, vector_1024,
                       ts_rank_cd(body_tsv, websearch_to_tsquery('english', @q)) AS text_rank
                FROM hermes_wiki.documents
                WHERE body_tsv @@ websearch_to_tsquery('english', @q)
                ORDER BY text_rank DESC
                LIMIT 200
            )
            SELECT slug, title, body_md, text_rank,
                   1 - (vector_1024 <=> @qvec::vector) AS vector_sim
            FROM fts_candidates
            ORDER BY (text_rank + (1 - (vector_1024 <=> @qvec::vector))) DESC
            LIMIT @k
            """, conn);
        cmd.Parameters.AddWithValue("q", query);
        cmd.Parameters.Add(new NpgsqlParameter("qvec", NpgsqlDbType.Array | NpgsqlDbType.Real) { Value = qVec });
        cmd.Parameters.AddWithValue("k", topK);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var results = new List<WikiSearchHit>();
        while (await reader.ReadAsync(ct))
        {
            results.Add(new WikiSearchHit(
                Slug: reader.GetString(0),
                Title: reader.GetString(1),
                BodyMd: reader.GetString(2),
                TextRank: reader.GetDouble(3),
                VectorSim: reader.GetDouble(4)));
        }
        return results;
    }

    private static async Task SyncTagsAsync(NpgsqlConnection conn, long docId, string[] tags, CancellationToken ct)
    {
        // 1. upsert tag rows
        foreach (var t in tags.Distinct())
        {
            await using var tagCmd = new NpgsqlCommand(
                "INSERT INTO hermes_wiki.tags (name) VALUES (@n) ON CONFLICT (name) DO NOTHING", conn);
            tagCmd.Parameters.AddWithValue("n", t);
            await tagCmd.ExecuteNonQueryAsync(ct);
        }
        // 2. replace document_tags rows
        await using (var del = new NpgsqlCommand(
            "DELETE FROM hermes_wiki.document_tags WHERE document_id = @id", conn))
        {
            del.Parameters.AddWithValue("id", docId);
            await del.ExecuteNonQueryAsync(ct);
        }
        foreach (var t in tags.Distinct())
        {
            await using var ins = new NpgsqlCommand(
                "INSERT INTO hermes_wiki.document_tags (document_id, tag_id) " +
                "SELECT @id, id FROM hermes_wiki.tags WHERE name = @n", conn);
            ins.Parameters.AddWithValue("id", docId);
            ins.Parameters.AddWithValue("n", t);
            await ins.ExecuteNonQueryAsync(ct);
        }
    }
}

public sealed record WikiDocument(long Id, string Slug, string Title, string BodyMd, string? Category, DateTime CreatedAt, DateTime UpdatedAt);
public sealed record WikiLinkRef(long Id, string Slug, string Title, string? Context);
public sealed record WikiRelated(string Slug, string Title, int Depth);
public sealed record WikiSearchHit(string Slug, string Title, string BodyMd, double TextRank, double VectorSim);
