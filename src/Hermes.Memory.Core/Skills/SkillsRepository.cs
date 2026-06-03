using Hermes.Memory.Core.Db;
using Npgsql;

namespace Hermes.Memory.Core.Skills;

/// <summary>
/// Catalog index for installed skills. Stores skill metadata + a
/// generated FTS column over name+description. The actual SKILL.md
/// file lives in the repo (skills-as-code) — this table is just the
/// searchable index (skills-as-data).
/// </summary>
public sealed class SkillsRepository
{
    private readonly HermesDataSource _ds;
    public SkillsRepository(HermesDataSource ds) => _ds = ds;

    /// <summary>
    /// Register or update a skill's catalog entry. Idempotent on
    /// (name, version): same name + same version = no-op. Different
    /// version = update.
    /// </summary>
    public async Task<long> RegisterAsync(string name, string version, string? owner = null, string? description = null, string[]? tags = null, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            INSERT INTO hermes_skills.skills (name, version, owner, description, tags)
            VALUES (@n, @v, @o, @d, @t)
            ON CONFLICT (name) DO UPDATE SET
                version = EXCLUDED.version,
                owner = EXCLUDED.owner,
                description = EXCLUDED.description,
                tags = EXCLUDED.tags,
                updated_at = now()
            RETURNING id
            """, conn);
        cmd.Parameters.AddWithValue("n", name);
        cmd.Parameters.AddWithValue("v", version);
        cmd.Parameters.AddWithValue("o", (object?)owner ?? DBNull.Value);
        cmd.Parameters.AddWithValue("d", (object?)description ?? DBNull.Value);
        cmd.Parameters.Add(new NpgsqlParameter("t", NpgsqlDbType.Array | NpgsqlDbType.Text)
            { Value = (object?)tags ?? Array.Empty<string>() });
        return (long)(await cmd.ExecuteScalarAsync(ct))!;
    }

    /// <summary>
    /// Add a relationship between two skills. Kind ∈ {depends_on, supersedes, related, see_also}.
    /// </summary>
    public async Task<bool> LinkAsync(string sourceSkill, string targetSkill, string kind, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            INSERT INTO hermes_skills.skill_links (source_id, target_id, kind)
            SELECT s.id, t.id, @k
            FROM hermes_skills.skills s, hermes_skills.skills t
            WHERE s.name = @src AND t.name = @tgt
            ON CONFLICT (source_id, target_id, kind) DO NOTHING
            """, conn);
        cmd.Parameters.AddWithValue("src", sourceSkill);
        cmd.Parameters.AddWithValue("tgt", targetSkill);
        cmd.Parameters.AddWithValue("k", kind);
        return await cmd.ExecuteNonQueryAsync(ct) == 1;
    }

    public async Task<IReadOnlyList<SkillEntry>> IndexSearchAsync(string query, int topK = 20, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            SELECT name, version, owner, description, tags,
                   ts_rank_cd(body_tsv, websearch_to_tsquery('english', @q)) AS rank
            FROM hermes_skills.skills
            WHERE body_tsv @@ websearch_to_tsquery('english', @q) OR @q = ''
            ORDER BY rank DESC NULLS LAST, name
            LIMIT @k
            """, conn);
        cmd.Parameters.AddWithValue("q", query);
        cmd.Parameters.AddWithValue("k", topK);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var results = new List<SkillEntry>();
        while (await reader.ReadAsync(ct))
        {
            results.Add(new SkillEntry(
                Name: reader.GetString(0),
                Version: reader.GetString(1),
                Owner: reader.IsDBNull(2) ? null : reader.GetString(2),
                Description: reader.IsDBNull(3) ? null : reader.GetString(3),
                Tags: reader.IsDBNull(4) ? Array.Empty<string>() : reader.GetFieldValue<string[]>(4),
                Rank: reader.IsDBNull(5) ? 0 : reader.GetDouble(5)));
        }
        return results;
    }

    /// <summary>
    /// Walk the skill-link graph 2 hops from a starting skill, returning
    /// related skills grouped by relationship kind. Useful for "what
    /// does this skill depend on / supersede / relate to".
    /// </summary>
    public async Task<IReadOnlyList<SkillLinkEdge>> GraphAsync(string rootSkill, int maxHops = 2, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            WITH RECURSIVE walk AS (
                SELECT s.id, s.name, NULL::text AS kind, 0 AS depth, ARRAY[s.id] AS path
                FROM hermes_skills.skills s
                WHERE s.name = @root
                UNION ALL
                SELECT t.id, t.name, l.kind, w.depth + 1, w.path || t.id
                FROM walk w
                JOIN hermes_skills.skill_links l ON l.source_id = w.id
                JOIN hermes_skills.skills t ON t.id = l.target_id
                WHERE w.depth < @max_hops AND NOT (t.id = ANY(w.path))
            )
            SELECT name, kind, MIN(depth) AS depth
            FROM walk
            WHERE depth > 0
            GROUP BY name, kind
            ORDER BY kind, depth, name
            """, conn);
        cmd.Parameters.AddWithValue("root", rootSkill);
        cmd.Parameters.AddWithValue("max_hops", maxHops);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var results = new List<SkillLinkEdge>();
        while (await reader.ReadAsync(ct))
        {
            results.Add(new SkillLinkEdge(
                Name: reader.GetString(0),
                Kind: reader.IsDBNull(1) ? null : reader.GetString(1),
                Depth: reader.GetInt32(2)));
        }
        return results;
    }
}

public sealed record SkillEntry(string Name, string Version, string? Owner, string? Description, string[] Tags, double Rank);
public sealed record SkillLinkEdge(string Name, string? Kind, int Depth);
