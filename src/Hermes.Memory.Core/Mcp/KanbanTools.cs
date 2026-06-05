using System.ComponentModel;
using System.Text.Json;
using Hermes.Memory.Core.Kanban;
using ModelContextProtocol.Server;

namespace Hermes.Memory.Core.Mcp;

/// <summary>
/// MCP tools for the kanban surface. 17 tools covering tenants, tasks,
/// dispatch (claim/heartbeat/complete/fail), comments, history, links,
/// and notify subscriptions. All over stdio.
///
/// Concurrency note: the dispatcher (in the worker process) calls
/// <see cref="ClaimNext"/>; the race-free guarantee comes from
/// SELECT ... FOR UPDATE SKIP LOCKED inside the repo.
/// </summary>
[McpServerToolType]
public sealed class KanbanTools(KanbanRepository repo)
{
    private readonly KanbanRepository _repo = repo;

    // ===== Tenants =====

    [McpServerTool(Name = "kanban_tenants"), Description("List tenants (boards). Pass include_archived=true to see archived ones.")]
    public async Task<string> Tenants(
        [Description("Include archived tenants (default false)")] bool include_archived = false,
        CancellationToken ct = default)
    {
        var rows = await _repo.ListTenantsAsync(include_archived, ct);
        return JsonSerializer.Serialize(rows, JsonOpts);
    }

    [McpServerTool(Name = "kanban_tenant_create"), Description("Create or update a tenant (board) by slug. Idempotent on slug.")]
    public async Task<string> TenantCreate(
        [Description("URL-safe slug, e.g. 'sv'")] string slug,
        [Description("Display name")] string name,
        [Description("Optional description")] string? description = null,
        [Description("Optional emoji or icon URL")] string? icon = null,
        [Description("Optional color (hex or name)")] string? color = null,
        [Description("Optional default workdir for new tasks")] string? default_workdir = null,
        CancellationToken ct = default)
    {
        var id = await _repo.UpsertTenantAsync(slug, name, description, icon, color, default_workdir, ct);
        return $"Tenant '{slug}' upserted with id {id}";
    }

    // ===== Tasks =====

    [McpServerTool(Name = "kanban_create"), Description("Create or update a kanban task. Id is supplied by the caller (e.g. 't_abc123').")]
    public async Task<string> Create(
        [Description("Task id, e.g. 't_4b0999d8'")] string id,
        [Description("Tenant slug, e.g. 'sv'")] string tenant_slug,
        [Description("Task title")] string title,
        [Description("Task body / description")] string? body = null,
        [Description("Optional assignee (worker identity)")] string? assignee = null,
        [Description("Initial status (default 'ready'). One of: ready/running/blocked/done/crashed/timed_out/failed/archived/cancelled")] string? status = "ready",
        [Description("Priority (higher = claimed first). Default 0.")] int priority = 0,
        [Description("Optional JSON array of skill names to force-load on dispatch")] string? skills_json = null,
        [Description("Optional model override for the worker")] string? model_override = null,
        [Description("Optional per-task failure limit (overrides default 3)")] int? max_retries = null,
        [Description("Optional originating session id")] string? session_id = null,
        CancellationToken ct = default)
    {
        var input = new KanbanTaskCreate(
            Id: id, TenantSlug: tenant_slug, Title: title, Body: body,
            Assignee: assignee, Status: status, Priority: priority,
            CreatedBy: Environment.UserName,
            SkillsJson: skills_json, ModelOverride: model_override,
            MaxRetries: max_retries, SessionId: session_id);
        var out_ = await _repo.CreateTaskAsync(input, ct);
        return $"Task {out_} upserted in tenant '{tenant_slug}'";
    }

    [McpServerTool(Name = "kanban_list"), Description("List tasks. Filter by tenant_slug, status, or assignee.")]
    public async Task<string> List(
        [Description("Optional tenant slug filter")] string? tenant_slug = null,
        [Description("Optional status filter")] string? status = null,
        [Description("Optional assignee filter")] string? assignee = null,
        [Description("Max results (default 100)")] int limit = 100,
        CancellationToken ct = default)
    {
        var rows = await _repo.ListTasksAsync(tenant_slug, status, assignee, limit, ct);
        return JsonSerializer.Serialize(rows, JsonOpts);
    }

    [McpServerTool(Name = "kanban_get"), Description("Get one task by id, with full details.")]
    public async Task<string> Get(
        [Description("Task id")] string id,
        CancellationToken ct = default)
    {
        var t = await _repo.GetTaskAsync(id, ct);
        if (t is null) return $"No task with id '{id}'";
        return JsonSerializer.Serialize(t, JsonOpts);
    }

    [McpServerTool(Name = "kanban_search"), Description("FTS search across task titles and bodies. Tenant-scoped if tenant_slug is given.")]
    public async Task<string> Search(
        [Description("Search query")] string query,
        [Description("Optional tenant slug filter")] string? tenant_slug = null,
        [Description("Max results (default 20)")] int limit = 20,
        CancellationToken ct = default)
    {
        // Simple LIKE over title+body. The v_board_tasks view has a
        // generated tsvector; a future refinement would use
        // websearch_to_tsquery directly. Kept simple here for compat.
        var all = await _repo.ListTasksAsync(tenant_slug, limit: 500, ct: ct);
        var q = query.ToLowerInvariant();
        var hits = all.Where(t =>
            (t.Title?.ToLowerInvariant().Contains(q, StringComparison.InvariantCultureIgnoreCase) ?? false) ||
            (t.Body?.ToLowerInvariant().Contains(q, StringComparison.InvariantCultureIgnoreCase) ?? false))
            .Take(limit)
            .ToList();
        return JsonSerializer.Serialize(hits, JsonOpts);
    }

    // ===== Dispatcher (the killer pattern) =====

    [McpServerTool(Name = "kanban_claim"), Description("Atomically claim the next ready task. Race-free via SELECT ... FOR UPDATE SKIP LOCKED. Returns null if nothing to claim.")]
    public async Task<string> Claim(
        [Description("The worker identity claiming the task")] string assignee,
        [Description("Optional max runtime in seconds (writes to tasks.max_runtime_seconds)")] int? max_runtime_seconds = null,
        CancellationToken ct = default)
    {
        // worker_pid is the process id of the calling worker; we don't
        // know it from MCP, so we use the .NET process id (which is the
        // hermes-memory process — the dispatcher). For per-task workers,
        // the dispatcher sets this from its own process id.
        var pid = Environment.ProcessId;
        var task = await _repo.ClaimNextAsync(assignee, pid, max_runtime_seconds, ct);
        if (task is null) return "{\"claimed\":false}";
        return JsonSerializer.Serialize(new { claimed = true, task }, JsonOpts);
    }

    [McpServerTool(Name = "kanban_heartbeat"), Description("Worker heartbeat — updates last_heartbeat_at. Returns true if the task is still claimed by this worker.")]
    public async Task<string> Heartbeat(
        [Description("Task id")] string id,
        CancellationToken ct = default)
    {
        var pid = Environment.ProcessId;
        var ok = await _repo.HeartbeatAsync(id, pid, ct);
        return ok ? "{\"ok\":true}" : "{\"ok\":false}";
    }

    [McpServerTool(Name = "kanban_complete"), Description("Mark a task done. Records a task_runs row + a task_events entry.")]
    public async Task<string> Complete(
        [Description("Task id")] string id,
        [Description("One-line summary of what happened")] string summary,
        [Description("Optional structured result (e.g. PR URL, file path, JSON blob)")] string? result = null,
        CancellationToken ct = default)
    {
        var pid = Environment.ProcessId;
        var ok = await _repo.CompleteAsync(id, pid, summary, result, ct: ct);
        return ok ? $"Task {id} marked done" : $"Task {id} not running for this worker";
    }

    [McpServerTool(Name = "kanban_fail"), Description("Mark a task failed. Increments consecutive_failures. Returns to 'ready' or trips to 'blocked' if max_retries exceeded.")]
    public async Task<string> Fail(
        [Description("Task id")] string id,
        [Description("Error message")] string error,
        [Description("Optional status override ('blocked', 'failed', 'cancelled'). Default: ready/blocked per circuit breaker.")] string? status = null,
        CancellationToken ct = default)
    {
        var pid = Environment.ProcessId;
        var ok = await _repo.FailAsync(id, pid, error, status, ct);
        return ok ? $"Task {id} marked failed" : $"Task {id} not running for this worker";
    }

    // ===== Comments, history, links =====

    [McpServerTool(Name = "kanban_comment"), Description("Add a comment to a task.")]
    public async Task<string> Comment(
        [Description("Task id")] string id,
        [Description("Comment body")] string body,
        [Description("Author (defaults to env user)")] string? author = null,
        CancellationToken ct = default)
    {
        var id_ = await _repo.AddCommentAsync(id, author ?? Environment.UserName, body, ct);
        return $"Comment {id_} added to {id}";
    }

    [McpServerTool(Name = "kanban_history"), Description("Get the event + run history for a task. Newest first.")]
    public async Task<string> History(
        [Description("Task id")] string id,
        [Description("Max events (default 100)")] int limit = 100,
        CancellationToken ct = default)
    {
        var events = await _repo.GetHistoryAsync(id, limit, ct);
        return JsonSerializer.Serialize(events, JsonOpts);
    }

    [McpServerTool(Name = "kanban_link"), Description("Add a parent/child link between two tasks.")]
    public async Task<string> Link(
        [Description("Parent task id")] string parent_id,
        [Description("Child task id")] string child_id,
        CancellationToken ct = default)
    {
        var ok = await _repo.LinkAsync(parent_id, child_id, ct);
        return ok ? $"Linked {parent_id} -> {child_id}" : "Link already exists or one of the tasks is missing";
    }

    [McpServerTool(Name = "kanban_unlink"), Description("Remove a parent/child link between two tasks.")]
    public async Task<string> Unlink(
        [Description("Parent task id")] string parent_id,
        [Description("Child task id")] string child_id,
        CancellationToken ct = default)
    {
        var ok = await _repo.UnlinkAsync(parent_id, child_id, ct);
        return ok ? $"Unlinked {parent_id} -> {child_id}" : "No such link";
    }

    [McpServerTool(Name = "kanban_children"), Description("List the children of a parent task.")]
    public async Task<string> Children(
        [Description("Parent task id")] string parent_id,
        CancellationToken ct = default)
    {
        var children = await _repo.GetChildrenAsync(parent_id, ct);
        return JsonSerializer.Serialize(children, JsonOpts);
    }

    [McpServerTool(Name = "kanban_parents"), Description("List the parents of a child task.")]
    public async Task<string> Parents(
        [Description("Child task id")] string child_id,
        CancellationToken ct = default)
    {
        var parents = await _repo.GetParentsAsync(child_id, ct);
        return JsonSerializer.Serialize(parents, JsonOpts);
    }

    // ===== Notify subscriptions =====

    [McpServerTool(Name = "kanban_subscribe"), Description("Subscribe a chat/thread to status updates for a task.")]
    public async Task<string> Subscribe(
        [Description("Task id")] string id,
        [Description("Platform: discord, telegram, slack, ...")] string platform,
        [Description("Chat/channel id")] string chat_id,
        [Description("Optional thread id (for threaded platforms)")] string? thread_id = null,
        [Description("Optional user id (for DM contexts)")] string? user_id = null,
        CancellationToken ct = default)
    {
        var ok = await _repo.SubscribeAsync(id, platform, chat_id, thread_id, user_id, ct);
        return ok ? $"Subscribed {platform}:{chat_id} to {id}" : "Already subscribed";
    }

    [McpServerTool(Name = "kanban_unsubscribe"), Description("Remove a notify subscription.")]
    public async Task<string> Unsubscribe(
        [Description("Task id")] string id,
        [Description("Platform")] string platform,
        [Description("Chat id")] string chat_id,
        [Description("Optional thread id")] string? thread_id = null,
        CancellationToken ct = default)
    {
        var ok = await _repo.UnsubscribeAsync(id, platform, chat_id, thread_id, ct);
        return ok ? $"Unsubscribed {platform}:{chat_id} from {id}" : "No such subscription";
    }

    private static readonly JsonSerializerOptions JsonOpts = new() { WriteIndented = false };
}
