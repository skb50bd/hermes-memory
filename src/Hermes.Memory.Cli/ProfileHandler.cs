using System.CommandLine.Invocation;
using Microsoft.Extensions.Logging;
using Npgsql;

namespace Hermes.Memory.Cli;

public static partial class ProfileHandler
{
    private static NpgsqlConnection OpenSuperuserConn(string? conn)
    {
        var s = conn ?? Environment.GetEnvironmentVariable("HERMES_PG_CONN_STR")
            ?? throw new InvalidOperationException("No connection string (--conn or HERMES_PG_CONN_STR)");
        var c = new NpgsqlConnection(s);
        c.Open();
        return c;
    }

    public static ICommandHandler CreateHandler() =>
        CommandHandler.Create<string, string?>(async (name, conn) =>
        {
            await using var c = OpenSuperuserConn(conn);
            // Sanity: name must match hermes_<x>; reject obvious mistakes.
            if (!MyRegex().IsMatch(name))
            {
                Console.Error.WriteLine($"Invalid profile name '{name}'. Use lowercase, digits, underscores; max 31 chars.");
                return 1;
            }
            var dbName = $"hermes_{name}";
            await using var cmd = new NpgsqlCommand(
                $"CREATE DATABASE {dbName} TEMPLATE hermes_template CONNECTION LIMIT 20", c);
            try
            {
                await cmd.ExecuteNonQueryAsync();
                Console.WriteLine($"Created database '{dbName}' from template (CONNECTION LIMIT 20).");
                return 0;
            }
            catch (PostgresException ex) when (ex.SqlState == "42P04")   // duplicate_database
            {
                Console.Error.WriteLine($"Database '{dbName}' already exists.");
                return 1;
            }
        });

    public static ICommandHandler ListHandler() =>
        CommandHandler.Create<string?>(async (conn) =>
        {
            await using var c = OpenSuperuserConn(conn);
            await using var cmd = new NpgsqlCommand(
                """
                SELECT datname, pg_size_pretty(pg_database_size(datname)) AS size
                FROM pg_database
                WHERE datname LIKE 'hermes_%' OR datname = 'hermes_template'
                ORDER BY datname
                """, c);
            await using var r = await cmd.ExecuteReaderAsync();
            Console.WriteLine($"{"name",-30}  {"size",-12}");
            while (await r.ReadAsync())
            {
                Console.WriteLine($"{r.GetString(0),-30}  {r.GetString(1),-12}");
            }
            return 0;
        });

    public static ICommandHandler DropHandler() =>
        CommandHandler.Create<string, string?, bool>(async (name, conn, confirm) =>
        {
            var dbName = $"hermes_{name}";
            if (!confirm)
            {
                Console.Write($"Type the database name to confirm drop of '{dbName}': ");
                var line = Console.ReadLine();
                if (line != dbName) { Console.Error.WriteLine("Aborted."); return 1; }
            }
            await using var c = OpenSuperuserConn(conn);
            await using var cmd = new NpgsqlCommand($"DROP DATABASE {dbName}", c);
            try
            {
                await cmd.ExecuteNonQueryAsync();
                Console.WriteLine($"Dropped database '{dbName}'.");
                return 0;
            }
            catch (PostgresException ex)
            {
                Console.Error.WriteLine($"Drop failed: {ex.Message}");
                return 1;
            }
        });
    [System.Text.RegularExpressions.GeneratedRegex("^[a-z][a-z0-9_]{0,30}$")]
    private static partial System.Text.RegularExpressions.Regex MyRegex();
}
