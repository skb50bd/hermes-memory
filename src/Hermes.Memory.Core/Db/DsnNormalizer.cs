using System;
using System.Collections.Generic;
using System.Web;

namespace Hermes.Memory.Core.Db;

/// <summary>
/// Bidirectional DSN (Postgres connection string) translator.
///
/// Three accepted input forms:
///   1. URI form:        postgresql://user:pass@host:port/dbname[?param=...]
///   2. Libpq key=value: host=... port=... dbname=... user=... password=...
///   3. ADO-style:       Host=...;Port=...;Database=...;Username=...;Password=...
///
/// - URI form: the human-friendly form used in `~/.hermes/.env` as
///   `PG_MEM_DB_CONN_STR=postgresql://hermes:***@127.0.0.1:5444/hermes_default`.
///   psycopg2 (the Python driver) accepts this natively. Npgsql 7+ does NOT.
/// - Libpq/ADO forms: what Npgsql wants.
///
/// This normalizer lets the C# binary accept ANY of the three forms —
/// it auto-detects the input format and produces the libpq key=value
/// form that Npgsql requires. Mirrors the Python plugin's
/// `_dsn_to_libpq` in `plugins/memory/postgres/__init__.py`.
///
/// URL-decodes percent-escapes in the URI's user/password components
/// (e.g. `p%40ss` → `p@ss`), which a naive split would mangle.
///
/// Query-string params on URI form are surfaced via the `QueryParams`
/// dict so callers can decide what to do (most are silently dropped
/// for Npgsql, which doesn't have a standard set — but `sslmode` and
/// `application_name` are kept when present).
/// </summary>
public static class DsnNormalizer
{
    public sealed class Parsed
    {
        public string Host { get; set; } = "localhost";
        public int Port { get; set; } = 5432;
        public string Database { get; set; } = "";
        public string Username { get; set; } = "";
        public string Password { get; set; } = "";
        public string SslMode { get; set; } = "";
        public string ApplicationName { get; set; } = "";

        /// <summary>Render as Npgsql-compatible key=value pairs (whitespace-separated).</summary>
        public string ToLibpq() => ToNpgsql();

        /// <summary>Render as Npgsql-compatible key=value pairs (whitespace-separated).</summary>
        public string ToNpgsql()
        {
            var parts = new List<string> { $"Host={Host}", $"Port={Port}" };
            if (!string.IsNullOrEmpty(Database)) parts.Add($"Database={Database}");
            if (!string.IsNullOrEmpty(Username)) parts.Add($"Username={Username}");
            if (!string.IsNullOrEmpty(Password)) parts.Add($"Password={Password}");
            if (!string.IsNullOrEmpty(SslMode)) parts.Add($"SslMode={SslMode}");
            if (!string.IsNullOrEmpty(ApplicationName)) parts.Add($"ApplicationName={ApplicationName}");
            return string.Join(";", parts);
        }

        /// <summary>Render as a libpq-style key=value string (whitespace-separated, no quoting).</summary>
        public string ToLibpqKeyValue()
        {
            var parts = new List<string> { $"host={Host}", $"port={Port}" };
            if (!string.IsNullOrEmpty(Database)) parts.Add($"dbname={Database}");
            if (!string.IsNullOrEmpty(Username)) parts.Add($"user={Username}");
            if (!string.IsNullOrEmpty(Password)) parts.Add($"password={Password}");
            return string.Join(" ", parts);
        }

        /// <summary>Render as a Postgres URI (postgresql://user:pass@host:port/db).</summary>
        public string ToUri()
        {
            var ub = new UriBuilder("postgresql", Host, Port, "/" + Database);
            if (!string.IsNullOrEmpty(Username))
            {
                ub.UserName = HttpUtility.UrlEncode(Username);
                ub.Password = HttpUtility.UrlEncode(Password);
            }
            return ub.Uri.ToString();
        }
    }

    /// <summary>
    /// Normalize any accepted DSN form to a libpq-style key=value string
    /// suitable for `psql` and similar CLI tools. This is the most
    /// portable form — most Postgres tools accept it.
    /// </summary>
    public static string ToLibpq(string dsn) => Parse(dsn).ToLibpqKeyValue();

    /// <summary>
    /// Normalize any accepted DSN form to an Npgsql-compatible connection
    /// string (semicolon-separated `Key=Value` pairs). This is what
    /// `NpgsqlDataSourceBuilder` wants.
    /// </summary>
    public static string ToNpgsql(string dsn) => Parse(dsn).ToNpgsql();

    /// <summary>
    /// Parse any accepted DSN form into a structured <see cref="Parsed"/>.
    /// Returns an empty <see cref="Parsed"/> (host=localhost, port=5432)
    /// if the input is null/whitespace.
    /// </summary>
    public static Parsed Parse(string dsn)
    {
        var p = new Parsed();
        if (string.IsNullOrWhiteSpace(dsn)) return p;

        var raw = dsn.Trim();

        // Form 1: URI
        if (raw.StartsWith("postgresql://", StringComparison.OrdinalIgnoreCase) ||
            raw.StartsWith("postgres://", StringComparison.OrdinalIgnoreCase))
        {
            if (Uri.TryCreate(raw, UriKind.Absolute, out var uri))
            {
                p.Host = string.IsNullOrEmpty(uri.Host) ? "localhost" : uri.Host;
                if (uri.Port > 0) p.Port = uri.Port;
                p.Database = uri.AbsolutePath?.TrimStart('/') ?? "";
                if (!string.IsNullOrEmpty(uri.UserInfo))
                {
                    var sep = uri.UserInfo.IndexOf(':');
                    if (sep < 0)
                    {
                        p.Username = HttpUtility.UrlDecode(uri.UserInfo);
                    }
                    else
                    {
                        p.Username = HttpUtility.UrlDecode(uri.UserInfo[..sep]);
                        p.Password = HttpUtility.UrlDecode(uri.UserInfo[(sep + 1)..]);
                    }
                }
                // Pull known query params
                if (!string.IsNullOrEmpty(uri.Query))
                {
                    var q = HttpUtility.ParseQueryString(uri.Query);
                    if (q["sslmode"] != null) p.SslMode = q["sslmode"] ?? "";
                    if (q["application_name"] != null) p.ApplicationName = q["application_name"] ?? "";
                }
            }
            return p;
        }

        // Form 3: ADO-style semicolon-separated (must be checked BEFORE
        // form 2, because libpq form has no semicolons but ADO does).
        if (raw.Contains(';'))
        {
            foreach (var part in raw.Split(';'))
            {
                var eq = part.IndexOf('=');
                if (eq < 0) continue;
                var k = part[..eq].Trim();
                var v = part[(eq + 1)..].Trim();
                ApplyKey(k, v, p);
            }
            return p;
        }

        // Form 2: libpq key=value (whitespace-separated)
        foreach (var part in raw.Split([' ', '\t'], StringSplitOptions.RemoveEmptyEntries))
        {
            var eq = part.IndexOf('=');
            if (eq < 0) continue;
            var k = part[..eq].Trim();
            var v = part[(eq + 1)..].Trim();
            ApplyKey(k, v, p);
        }
        return p;
    }

    private static void ApplyKey(string key, string value, Parsed p)
    {
        switch (key.ToLowerInvariant())
        {
            case "host":
            case "server":
            case "data source":
                p.Host = value;
                break;
            case "port":
                if (int.TryParse(value, out var port)) p.Port = port;
                break;
            case "database":
            case "dbname":
            case "initial catalog":
                p.Database = value;
                break;
            case "user":
            case "username":
            case "user id":
            case "uid":
                p.Username = value;
                break;
            case "password":
            case "pwd":
                p.Password = value;
                break;
            case "sslmode":
                p.SslMode = value;
                break;
            case "application_name":
            case "application name":
                p.ApplicationName = value;
                break;
        }
    }
}
