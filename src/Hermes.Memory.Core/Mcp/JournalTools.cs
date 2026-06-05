using System.ComponentModel;
using System.Text.Json;
using Hermes.Memory.Core.Journal;
using ModelContextProtocol.Server;

namespace Hermes.Memory.Core.Mcp;

[McpServerToolType]
public sealed class JournalTools(JournalRepository repo)
{
    private readonly JournalRepository _repo = repo;

    [McpServerTool(Name = "journal_log_session"), Description("Open a new conversation session. Returns the session id.")]
    public async Task<string> LogSession(
        [Description("Profile name this session belongs to")] string profile,
        CancellationToken ct = default)
    {
        var id = await _repo.OpenSessionAsync(profile, ct: ct);
        return $"Session {id} opened for profile '{profile}'";
    }

    [McpServerTool(Name = "journal_log_message"), Description("Log one message to an open session. role ∈ {user, assistant, tool, system}.")]
    public async Task<string> LogMessage(
        [Description("Session id")] long session_id,
        [Description("Message role: user / assistant / tool / system")] string role,
        [Description("Message content")] string content,
        [Description("Optional JSON string of tool calls (for role=assistant)")] string? tool_calls = null,
        CancellationToken ct = default)
    {
        await _repo.LogMessageAsync(session_id, role, content, tool_calls, ct);
        return $"Logged {role} message to session {session_id}";
    }

    [McpServerTool(Name = "journal_search"), Description("FTS search across all conversation messages.")]
    public async Task<string> Search(
        [Description("The search query")] string query,
        [Description("Max results (default 50)")] int top_k = 50,
        CancellationToken ct = default)
    {
        var hits = await _repo.SearchAsync(query, top_k, ct);
        return JsonSerializer.Serialize(hits, JsonOpts);
    }

    private static readonly JsonSerializerOptions JsonOpts = new() { WriteIndented = true };
}
