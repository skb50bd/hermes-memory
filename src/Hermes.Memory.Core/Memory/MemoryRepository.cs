using System.Text.Json;
using Hermes.Memory.Core.Db;
using Hermes.Memory.Core.Embeddings;
using Npgsql;
using NpgsqlTypes;

namespace Hermes.Memory.Core.Memory;

/// <summary>
/// CRUD over agent_memory.memories. Hybrid search (FTS + cosine) using
/// the per-dim vector column matching the embedder's dim. Soft-delete
/// via deleted_at. Tags and category (ltree) for faceting.
/// </summary>
public sealed class MemoryRepository(HermesDataSource ds, EmbedderRegistry embedders)
{
    private readonly HermesDataSource _ds = ds;
    private readonly EmbedderRegistry _embedders = embedders;

    /// <summary>
    /// Store one memory. Embeds the content via the default-dim embedder
    /// and writes into the matching vector_<dim> column. Idempotent on
    /// (content, source) — re-storing identical content is a no-op.
    /// </summary>
    public async Task<long> RememberAsync(
        string content,
        string[]? tags = null,
        string? category = null,
        string? source = null,
        JsonElement? metadata = default,
        CancellationToken ct = default)
    {
        var embedder = _embedders.GetDefault();
        var vec = await embedder.EmbedAsync(content, ct);
        var dim = embedder.Dim;

        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            INSERT INTO agent_memory.memories
                (content, vector_768, vector_1024, vector_1536, tags, category, source, metadata)
            VALUES
                (@content, @v768, @v1024, @v1536, @tags, @category, @source, @metadata::jsonb)
            ON CONFLICT DO NOTHING
            RETURNING id
            """, conn);
        cmd.Parameters.AddWithValue("content", content);
        cmd.Parameters.AddWithValue("v768", (object?)(dim == 768 ? vec : null) ?? DBNull.Value);
        cmd.Parameters.AddWithValue("v1024", (object?)(dim == 1024 ? vec : null) ?? DBNull.Value);
        cmd.Parameters.AddWithValue("v1536", (object?)(dim == 1536 ? vec : null) ?? DBNull.Value);
        cmd.Parameters.Add(new NpgsqlParameter("tags", NpgsqlDbType.Array | NpgsqlDbType.Text)
        { Value = (object?)tags ?? Array.Empty<string>() });
        cmd.Parameters.Add(new NpgsqlParameter("category", NpgsqlDbType.Unknown)
        { Value = (object?)category ?? DBNull.Value });
        cmd.Parameters.AddWithValue("source", (object?)source ?? DBNull.Value);
        cmd.Parameters.AddWithValue("metadata", metadata?.GetRawText() ?? "{}");
        var result = await cmd.ExecuteScalarAsync(ct);
        return result is long id ? id : 0L;   // 0 = duplicate, ON CONFLICT fired
    }

    /// <summary>
    /// Hybrid search: FTS pre-filter (token overlap), rerank with cosine.
    /// Returns up to <paramref name="topK"/> rows ordered by the hybrid
    /// score. Empty result is valid — it means no token overlap. The
    /// caller can fall back to a pure-vector search if that's not what
    /// they want.
    /// </summary>
    public async Task<IReadOnlyList<MemoryHit>> SearchAsync(
        string query,
        int topK = 10,
        float hybridTextWeight = 0.5f,
        CancellationToken ct = default)
    {
        var embedder = _embedders.GetDefault();
        var dim = embedder.Dim;
        var qVec = await embedder.EmbedAsync(query, ct);

        // Build the SQL with the right dim column at runtime.
        // Crucially, both the CTE and outer SELECT reference the same
        // column — the FTS candidate filter and the cosine rerank share
        // their source row.
        var dimColumn = dim switch
        {
            768 => "vector_768",
            1024 => "vector_1024",
            1536 => "vector_1536",
            _ => throw new NotSupportedException($"Dim {dim} not supported")
        };

        var sql = $"""
            WITH fts_candidates AS (
                SELECT id, content, tags, category, created_at, source, {dimColumn},
                       ts_rank_cd(content_tsv, websearch_to_tsquery('english', @q)) AS text_rank
                FROM agent_memory.memories
                WHERE deleted_at IS NULL
                  AND content_tsv @@ websearch_to_tsquery('english', @q)
                ORDER BY text_rank DESC
                LIMIT 200
            )
            SELECT id, content, tags, category, created_at, source, text_rank,
                   CASE WHEN 1 - ({dimColumn} <=> @qvec::vector) = 'NaN'::real
                        THEN 0
                        ELSE 1 - ({dimColumn} <=> @qvec::vector)
                   END AS vector_sim
            FROM fts_candidates
            ORDER BY (@w * text_rank + (1 - @w) * CASE WHEN 1 - ({dimColumn} <=> @qvec::vector) = 'NaN'::real
                                                        THEN 0
                                                        ELSE 1 - ({dimColumn} <=> @qvec::vector)
                                                   END) DESC
            LIMIT @k
            """;

        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(sql, conn);
        cmd.Parameters.AddWithValue("q", query);
        cmd.Parameters.Add(new NpgsqlParameter("qvec", NpgsqlDbType.Array | NpgsqlDbType.Real) { Value = qVec });
        cmd.Parameters.AddWithValue("w", hybridTextWeight);
        cmd.Parameters.AddWithValue("k", topK);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var results = new List<MemoryHit>();
        while (await reader.ReadAsync(ct))
        {
            results.Add(new MemoryHit(
                Id: reader.GetInt64(0),
                Content: reader.GetString(1),
                Tags: reader.IsDBNull(2) ? [] : reader.GetFieldValue<string[]>(2),
                Category: reader.IsDBNull(3) ? null : reader.GetString(3),
                CreatedAt: reader.GetFieldValue<DateTime>(4),
                Source: reader.IsDBNull(5) ? null : reader.GetString(5),
                TextRank: reader.GetDouble(6),
                VectorSim: reader.GetDouble(7)
            ));
        }
        return results;
    }

    /// <summary>
    /// Soft-delete a memory by id. Sets deleted_at = now(). The row
    /// stays in the table (so embeddings are reusable) but is excluded
    /// from all search queries.
    /// </summary>
    public async Task<bool> ForgetAsync(long id, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            "UPDATE agent_memory.memories SET deleted_at = now(), updated_at = now() WHERE id = @id AND deleted_at IS NULL",
            conn);
        cmd.Parameters.AddWithValue("id", id);
        return await cmd.ExecuteNonQueryAsync(ct) == 1;
    }

    public async Task<MemoryStats> GetStatsAsync(CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            SELECT
                count(*) FILTER (WHERE deleted_at IS NULL) AS live,
                count(*) FILTER (WHERE deleted_at IS NOT NULL) AS deleted,
                count(*) FILTER (WHERE vector_1024 IS NOT NULL) AS embedded_1024,
                count(*) FILTER (WHERE vector_1024 = array_fill(0, ARRAY[1024])::vector) AS zero_vec
            FROM agent_memory.memories
            """, conn);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        await reader.ReadAsync(ct);
        return new MemoryStats(
            Live: reader.GetInt64(0),
            Deleted: reader.GetInt64(1),
            EmbeddedDim1024: reader.GetInt64(2),
            ZeroVectorDim1024: reader.GetInt64(3));
    }
}

public sealed record MemoryHit(
    long Id, string Content, string[] Tags, string? Category,
    DateTime CreatedAt, string? Source, double TextRank, double VectorSim);

public sealed record MemoryStats(long Live, long Deleted, long EmbeddedDim1024, long ZeroVectorDim1024);
