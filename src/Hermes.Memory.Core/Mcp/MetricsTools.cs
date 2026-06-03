using System.ComponentModel;
using System.Globalization;
using System.Text.Json;
using Hermes.Memory.Core.Metrics;
using ModelContextProtocol.Server;

namespace Hermes.Memory.Core.Mcp;

[McpServerToolType]
public sealed class MetricsTools
{
    private readonly MetricsRepository _repo;
    public MetricsTools(MetricsRepository repo) => _repo = repo;

    [McpServerTool(Name = "metrics_record"), Description("Record one metric sample. Use sparingly — prefer metrics_record_batch for >10 samples.")]
    public async Task<string> Record(
        [Description("Profile name")] string profile,
        [Description("Metric name, e.g. 'mcp.tool.duration_ms'")] string metric_name,
        [Description("Numeric value")] double value,
        [Description("Optional JSON tags string")] string? tags = null,
        CancellationToken ct = default)
    {
        await _repo.RecordAsync(profile, metric_name, value, tags, ct);
        return $"Recorded {metric_name}={value} for {profile}";
    }

    [McpServerTool(Name = "metrics_query"), Description("Aggregate metrics over a time range. Returns p50/p95/p99 per bucket.")]
    public async Task<string> Query(
        [Description("Optional profile filter")] string? profile = null,
        [Description("Optional metric name filter")] string? metric_name = null,
        [Description("From time (ISO8601, default 1h ago)")] string? from = null,
        [Description("To time (ISO8601, default now)")] string? to = null,
        [Description("Bucket size, e.g. '1 minute', '5 minutes' (default '1 minute')")] string bucket = "1 minute",
        [Description("Max result rows (default 100)")] int top_n = 100,
        CancellationToken ct = default)
    {
        var fromDt = from is null ? DateTime.UtcNow.AddHours(-1) : DateTime.Parse(from, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind);
        var toDt   = to   is null ? DateTime.UtcNow            : DateTime.Parse(to,   CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind);
        var rows = await _repo.QueryAsync(profile, metric_name, fromDt, toDt, bucket, top_n, ct);
        return JsonSerializer.Serialize(rows, JsonOpts);
    }

    private static readonly JsonSerializerOptions JsonOpts = new() { WriteIndented = true };
}
