using Hermes.Memory.Core.Db;
using Npgsql;
using NpgsqlTypes;

namespace Hermes.Memory.Core.Metrics;

/// <summary>
/// Writer for hermes_metrics.events (timescaledb hypertable). One row
/// per metric sample. Compression at 30d, retention at 90d (set in
/// initdb.d/01-template-bootstrap.sh).
///
/// Intended callers: gateway latency histograms, embedder cache hit
/// rates, MCP tool call counts. Not for conversation logs (those go
/// in hermes_journal, regular table, different access pattern).
/// </summary>
public sealed class MetricsRepository
{
    private readonly HermesDataSource _ds;
    public MetricsRepository(HermesDataSource ds) => _ds = ds;

    public async Task RecordAsync(string profile, string metricName, double value, string? tagsJson = null, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            INSERT INTO hermes_metrics.events (ts, profile, metric_name, value, tags)
            VALUES (now(), @p, @m, @v, @t::jsonb)
            """, conn);
        cmd.Parameters.AddWithValue("p", profile);
        cmd.Parameters.AddWithValue("m", metricName);
        cmd.Parameters.AddWithValue("v", value);
        cmd.Parameters.AddWithValue("t", tagsJson ?? "{}");
        await cmd.ExecuteNonQueryAsync(ct);
    }

    public async Task RecordBatchAsync(IReadOnlyList<MetricSample> samples, CancellationToken ct = default)
    {
        if (samples.Count == 0) return;
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var w = new NpgsqlBinaryImporter(conn,
            "COPY hermes_metrics.events (ts, profile, metric_name, value, tags) FROM STDIN (FORMAT BINARY)");
        foreach (var s in samples)
        {
            w.StartRow();
            w.Write(s.Ts, NpgsqlDbType.TimestampTz);
            w.Write(s.Profile, NpgsqlDbType.Text);
            w.Write(s.MetricName, NpgsqlDbType.Text);
            w.Write(s.Value, NpgsqlDbType.Double);
            if (s.TagsJson is null) w.WriteNull(); else w.Write(s.TagsJson, NpgsqlDbType.Jsonb);
        }
        await w.CompleteAsync(ct);
    }

    /// <summary>
    /// Aggregate over a time range. Returns one row per (metric_name,
    /// profile) with count/avg/min/max/p50/p95/p99. Uses time_bucket
    /// from timescaledb. For a 1-minute bucket and 1-hour range, the
    /// result is at most 60 rows; cheap to render in any UI.
    /// </summary>
    public async Task<IReadOnlyList<MetricAggregate>> QueryAsync(
        string? profile, string? metricName,
        DateTime from, DateTime to,
        string bucket = "1 minute", int topN = 100, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            SELECT
                time_bucket(@bucket::interval, ts) AS bucket,
                profile,
                metric_name,
                count(*) AS n,
                avg(value) AS avg_v,
                min(value) AS min_v,
                max(value) AS max_v,
                percentile_cont(0.50) WITHIN GROUP (ORDER BY value) AS p50,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY value) AS p95,
                percentile_cont(0.99) WITHIN GROUP (ORDER BY value) AS p99
            FROM hermes_metrics.events
            WHERE ts >= @from AND ts < @to
              AND (@profile IS NULL OR profile = @profile)
              AND (@metric IS NULL OR metric_name = @metric)
            GROUP BY bucket, profile, metric_name
            ORDER BY bucket DESC
            LIMIT @n
            """, conn);
        cmd.Parameters.AddWithValue("bucket", TimeSpan.TryParse(bucket, out var bs) ? bs : TimeSpan.FromMinutes(1));
        cmd.Parameters.AddWithValue("from", from);
        cmd.Parameters.AddWithValue("to", to);
        cmd.Parameters.AddWithValue("profile", (object?)profile ?? DBNull.Value);
        cmd.Parameters.AddWithValue("metric", (object?)metricName ?? DBNull.Value);
        cmd.Parameters.AddWithValue("n", topN);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var results = new List<MetricAggregate>();
        while (await reader.ReadAsync(ct))
        {
            results.Add(new MetricAggregate(
                Bucket: reader.GetFieldValue<DateTime>(0),
                Profile: reader.GetString(1),
                MetricName: reader.GetString(2),
                N: reader.GetInt64(3),
                Avg: reader.GetDouble(4),
                Min: reader.GetDouble(5),
                Max: reader.GetDouble(6),
                P50: reader.GetDouble(7),
                P95: reader.GetDouble(8),
                P99: reader.GetDouble(9)));
        }
        return results;
    }
}

public sealed record MetricSample(DateTime Ts, string Profile, string MetricName, double Value, string? TagsJson);
public sealed record MetricAggregate(DateTime Bucket, string Profile, string MetricName, long N, double Avg, double Min, double Max, double P50, double P95, double P99);
