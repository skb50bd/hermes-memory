using System.Collections.Concurrent;
using Microsoft.Extensions.Logging;
using Npgsql;
using NpgsqlTypes;

namespace Hermes.Memory.Core.Db;

/// <summary>
/// AOT-safe connection provider. Wraps an NpgsqlDataSource (the modern,
/// pool-aware way to get connections in Npgsql 7+). Registered once at
/// startup; the connection string is set by the binary's --conn flag
/// or the HERMES_PG_CONN_STR env var.
///
/// Why we don't use NpgsqlConnection directly: NpgsqlDataSource pools
/// connections across the process and is the only safe way to use
/// Npgsql in a long-running process (the MCP server). One connection
/// per tool call is wasteful and burns through the per-DB connection
/// limit.
///
/// AOT note: every Postgres type we read from any of the 5 schemas
/// must be handled in <see cref="ConfigureDataSource"/>. The JSON
/// serializer context only covers JSON; vector, ltree, tsvector, and
/// jsonb columns have their own Npgsql type handlers.
/// </summary>
public sealed class HermesDataSource : IAsyncDisposable
{
    private readonly NpgsqlDataSource _dataSource;
    private readonly ILogger<HermesDataSource> _logger;

    public HermesDataSource(string connectionString, ILogger<HermesDataSource> logger)
    {
        _logger = logger;
        var builder = new NpgsqlDataSourceBuilder(connectionString);
        ConfigureDataSource(builder);
        _dataSource = builder.Build();
    }

    public NpgsqlDataSource Inner => _dataSource;
    public string ConnectionString => _dataSource.ConnectionString;

    /// <summary>
    /// AOT type registration. Every custom type that crosses a query
    /// boundary must be mapped here. The 5 schemas use:
    ///   - vector(N)      (pgvector; Npgsql 7+ has built-in support)
    ///   - ltree          (built into Postgres; Npgsql has built-in support)
    ///   - tsvector       (built into Postgres; read-only in Npgsql)
    ///   - jsonb          (built into Npgsql)
    ///   - timestamptz    (built into Npgsql)
    ///   - int[] / text[] (built into Npgsql)
    ///   - uuid           (built into Npgsql)
    /// </summary>
    private static void ConfigureDataSource(NpgsqlDataSourceBuilder builder)
    {
        // vector is built-in to Npgsql 7+ via Npgsql.NodaTime-style plugin
        // discovery, but in AOT mode we have to opt in explicitly.
        builder.EnableDynamicJson();   // jsonb round-trip
        builder.MapEnum<HermesJournalRole>("hermes_journal_role");  // see below

        // Performance: trust the server on read replica vs primary
        builder.ConnectionStringBuilder.ApplicationName = "hermes-memory";
        builder.ConnectionStringBuilder.CommandTimeout = 10;
        builder.ConnectionStringBuilder.Timeout = 5;
        builder.ConnectionStringBuilder.Pooling = true;
        builder.ConnectionStringBuilder.MaxPoolSize = 2;   // see skill: per-process pool cap
        builder.ConnectionStringBuilder.MinPoolSize = 0;
    }

    public Task<NpgsqlConnection> OpenConnectionAsync(CancellationToken ct = default)
        => _dataSource.OpenConnectionAsync(ct);

    public async ValueTask DisposeAsync()
    {
        await _dataSource.DisposeAsync();
    }
}

/// <summary>
/// Postgres enum for the hermes_journal.messages.role column.
/// Enum support requires explicit registration in NpgsqlDataSourceBuilder
/// AND the enum to exist in the database before the first query.
/// See: 01-template-bootstrap.sh (the CHECK constraint should be replaced
/// with a real enum, but for v1 the CHECK is fine).
/// </summary>
public enum HermesJournalRole
{
    User,
    Assistant,
    Tool,
    System
}
