using Hermes.Memory.Core.Db;
using Npgsql;
using NpgsqlTypes;

namespace Hermes.Memory.Core.Journal;

/// <summary>
/// Conversation log writer/reader. Stores sessions and messages in
/// hermes_journal.* (regular tables, NOT a hypertable). Use cases:
///   - "show me what I said to the agent in the last 7 days"
///   - "find every tool call that errored in the last 30 days"
///   - audit trail for sensitive operations
///
/// NOT a timeseries use case: small, joined to agent context, queried
/// by session_id first and time second. The hermes_metrics schema is
/// for actual timeseries.
/// </summary>
public sealed class JournalRepository(HermesDataSource ds)
{
    private readonly HermesDataSource _ds = ds;

    public async Task<long> OpenSessionAsync(string profile, string? metadataJson = null, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            "INSERT INTO hermes_journal.sessions (profile, metadata) VALUES (@p, @m::jsonb) RETURNING id", conn);
        cmd.Parameters.AddWithValue("p", profile);
        cmd.Parameters.AddWithValue("m", metadataJson ?? "{}");
        return (long)(await cmd.ExecuteScalarAsync(ct))!;
    }

    public async Task CloseSessionAsync(long sessionId, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            "UPDATE hermes_journal.sessions SET ended_at = now() WHERE id = @id AND ended_at IS NULL", conn);
        cmd.Parameters.AddWithValue("id", sessionId);
        await cmd.ExecuteNonQueryAsync(ct);
    }

    public async Task LogMessageAsync(long sessionId, string role, string content, string? toolCallsJson = null, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            INSERT INTO hermes_journal.messages (session_id, role, content, tool_calls)
            VALUES (@s, @r, @c, @t::jsonb)
            """, conn);
        cmd.Parameters.AddWithValue("s", sessionId);
        cmd.Parameters.AddWithValue("r", role);
        cmd.Parameters.AddWithValue("c", content);
        cmd.Parameters.AddWithValue("t", (object?)toolCallsJson ?? DBNull.Value);
        await cmd.ExecuteNonQueryAsync(ct);
    }

    public async Task<IReadOnlyList<JournalMessage>> GetSessionMessagesAsync(long sessionId, int limit = 1000, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            SELECT id, ts, role, content, tool_calls
            FROM hermes_journal.messages
            WHERE session_id = @s
            ORDER BY ts
            LIMIT @lim
            """, conn);
        cmd.Parameters.AddWithValue("s", sessionId);
        cmd.Parameters.AddWithValue("lim", limit);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var results = new List<JournalMessage>();
        while (await reader.ReadAsync(ct))
        {
            results.Add(new JournalMessage(
                Id: reader.GetInt64(0),
                Ts: reader.GetFieldValue<DateTime>(1),
                Role: reader.GetString(2),
                Content: reader.GetString(3),
                ToolCallsJson: reader.IsDBNull(4) ? null : reader.GetString(4)));
        }
        return results;
    }

    /// <summary>
    /// FTS search across all messages. Returns the matching messages
    /// with the session id, ordered by rank.
    /// </summary>
    public async Task<IReadOnlyList<JournalSearchHit>> SearchAsync(string query, int topK = 50, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            SELECT m.id, m.session_id, m.ts, m.role, m.content,
                   ts_rank_cd(m.content_tsv, websearch_to_tsquery('english', @q)) AS rank
            FROM hermes_journal.messages m
            WHERE m.content_tsv @@ websearch_to_tsquery('english', @q)
            ORDER BY rank DESC
            LIMIT @k
            """, conn);
        cmd.Parameters.AddWithValue("q", query);
        cmd.Parameters.AddWithValue("k", topK);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var results = new List<JournalSearchHit>();
        while (await reader.ReadAsync(ct))
        {
            results.Add(new JournalSearchHit(
                MessageId: reader.GetInt64(0),
                SessionId: reader.GetInt64(1),
                Ts: reader.GetFieldValue<DateTime>(2),
                Role: reader.GetString(3),
                Content: reader.GetString(4),
                Rank: reader.GetDouble(5)));
        }
        return results;
    }
}

public sealed record JournalMessage(long Id, DateTime Ts, string Role, string Content, string? ToolCallsJson);
public sealed record JournalSearchHit(long MessageId, long SessionId, DateTime Ts, string Role, string Content, double Rank);
