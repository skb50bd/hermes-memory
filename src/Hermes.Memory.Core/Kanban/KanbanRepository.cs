using Hermes.Memory.Core.Db;
using Npgsql;
using NpgsqlTypes;

namespace Hermes.Memory.Core.Kanban;

/// <summary>
/// CRUD + dispatcher for the hermes_kanban schema. Eight tables:
/// tenants, tasks, task_runs, task_events, task_links, task_comments,
/// task_attachments, notify_subs.
///
/// The killer method is <see cref="ClaimNextAsync"/> — atomic claim of
/// the next ready task via SELECT ... FOR UPDATE SKIP LOCKED. No
/// claim_lock/claim_expires math, no race conditions. The OLD claim
/// model (used by the SQLite plugin) still works in Postgres but is
/// slower and racy; the dispatcher rewrite in v0.3.0 will use this.
///
/// A "board" is a tenant. Cross-tenant list goes through v_board_tasks
/// or the tenants table. Per-tenant list filters on tenant_id.
/// </summary>
public sealed class KanbanRepository(HermesDataSource ds)
{
    private readonly HermesDataSource _ds = ds;

    // ====================================================================
    // Tenants
    // ====================================================================

    public async Task<long> UpsertTenantAsync(
        string slug, string name, string? description = null,
        string? icon = null, string? color = null, string? defaultWorkdir = null,
        CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            INSERT INTO hermes_kanban.tenants (slug, name, description, icon, color, default_workdir)
            VALUES (@slug, @name, @description, @icon, @color, @workdir)
            ON CONFLICT (slug) DO UPDATE SET
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                icon = EXCLUDED.icon,
                color = EXCLUDED.color,
                default_workdir = EXCLUDED.default_workdir
            RETURNING id
            """, conn);
        cmd.Parameters.AddWithValue("slug", slug);
        cmd.Parameters.AddWithValue("name", name);
        cmd.Parameters.AddWithValue("description", (object?)description ?? DBNull.Value);
        cmd.Parameters.AddWithValue("icon", (object?)icon ?? DBNull.Value);
        cmd.Parameters.AddWithValue("color", (object?)color ?? DBNull.Value);
        cmd.Parameters.AddWithValue("workdir", (object?)defaultWorkdir ?? DBNull.Value);
        return (long)(await cmd.ExecuteScalarAsync(ct))!;
    }

    public async Task<IReadOnlyList<Tenant>> ListTenantsAsync(bool includeArchived = false, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            "SELECT id, slug, name, description, icon, color, default_workdir, archived " +
            "FROM hermes_kanban.tenants " +
            (includeArchived ? "" : "WHERE archived = false ") +
            "ORDER BY name", conn);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var results = new List<Tenant>();
        while (await reader.ReadAsync(ct))
        {
            results.Add(new Tenant(
                Id: reader.GetInt64(0),
                Slug: reader.GetString(1),
                Name: reader.GetString(2),
                Description: reader.IsDBNull(3) ? null : reader.GetString(3),
                Icon: reader.IsDBNull(4) ? null : reader.GetString(4),
                Color: reader.IsDBNull(5) ? null : reader.GetString(5),
                DefaultWorkdir: reader.IsDBNull(6) ? null : reader.GetString(6),
                Archived: reader.GetBoolean(7)));
        }
        return results;
    }

    // ====================================================================
    // Tasks
    // ====================================================================

    /// <summary>
    /// Create a task. Returns the task id (caller supplies it for
    /// idempotency — e.g. "t_&lt;12hex&gt;" like the SQLite version).
    /// </summary>
    public async Task<string> CreateTaskAsync(KanbanTaskCreate input, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            INSERT INTO hermes_kanban.tasks
                (id, tenant_id, title, body, assignee, status, priority,
                 created_by, workspace_kind, workspace_path, branch_name,
                 idempotency_key, skills, model_override, max_retries,
                 session_id, goal_mode, goal_max_turns)
            VALUES
                (@id, (SELECT id FROM hermes_kanban.tenants WHERE slug = @tenant_slug),
                 @title, @body, @assignee, @status, @priority,
                 @created_by, @workspace_kind, @workspace_path, @branch_name,
                 @idem, @skills::jsonb, @model, @max_retries,
                 @session_id, @goal_mode, @goal_max_turns)
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                body = EXCLUDED.body,
                assignee = EXCLUDED.assignee,
                priority = EXCLUDED.priority,
                updated_at = now()
            RETURNING id
            """, conn);
        cmd.Parameters.AddWithValue("id", input.Id);
        cmd.Parameters.AddWithValue("tenant_slug", input.TenantSlug);
        cmd.Parameters.AddWithValue("title", input.Title);
        cmd.Parameters.AddWithValue("body", (object?)input.Body ?? DBNull.Value);
        cmd.Parameters.AddWithValue("assignee", (object?)input.Assignee ?? DBNull.Value);
        cmd.Parameters.AddWithValue("status", input.Status ?? "ready");
        cmd.Parameters.AddWithValue("priority", input.Priority);
        cmd.Parameters.AddWithValue("created_by", (object?)input.CreatedBy ?? DBNull.Value);
        cmd.Parameters.AddWithValue("workspace_kind", input.WorkspaceKind ?? "scratch");
        cmd.Parameters.AddWithValue("workspace_path", (object?)input.WorkspacePath ?? DBNull.Value);
        cmd.Parameters.AddWithValue("branch_name", (object?)input.BranchName ?? DBNull.Value);
        cmd.Parameters.AddWithValue("idem", (object?)input.IdempotencyKey ?? DBNull.Value);
        cmd.Parameters.AddWithValue("skills", input.SkillsJson ?? "[]");
        cmd.Parameters.AddWithValue("model", (object?)input.ModelOverride ?? DBNull.Value);
        cmd.Parameters.AddWithValue("max_retries", (object?)input.MaxRetries ?? DBNull.Value);
        cmd.Parameters.AddWithValue("session_id", (object?)input.SessionId ?? DBNull.Value);
        cmd.Parameters.AddWithValue("goal_mode", input.GoalMode);
        cmd.Parameters.AddWithValue("goal_max_turns", (object?)input.GoalMaxTurns ?? DBNull.Value);
        return (string)(await cmd.ExecuteScalarAsync(ct))!;
    }

    /// <summary>
    /// List tasks, optionally filtered by tenant and/or status. Newest
    /// first; if priority is non-zero, sort by priority desc then created_at.
    /// </summary>
    public async Task<IReadOnlyList<KanbanTask>> ListTasksAsync(
        string? tenantSlug = null, string? status = null, string? assignee = null,
        int limit = 100, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            SELECT t.id, t.tenant_id, ten.slug, t.title, t.body, t.assignee, t.status,
                   t.priority, t.created_by, t.created_at, t.started_at, t.completed_at,
                   t.workspace_kind, t.workspace_path, t.branch_name, t.result,
                   t.consecutive_failures, t.worker_pid, t.last_failure_error,
                   t.max_runtime_seconds, t.last_heartbeat_at, t.current_run_id,
                   t.workflow_template_id, t.current_step_key, t.skills,
                   t.model_override, t.max_retries, t.session_id, t.goal_mode, t.goal_max_turns
            FROM hermes_kanban.tasks t
            JOIN hermes_kanban.tenants ten ON ten.id = t.tenant_id
            WHERE (@tenant IS NULL OR ten.slug = @tenant)
              AND (@status IS NULL OR t.status = @status)
              AND (@assignee IS NULL OR t.assignee = @assignee)
            ORDER BY t.priority DESC, t.created_at DESC
            LIMIT @lim
            """, conn);
        cmd.Parameters.Add(new NpgsqlParameter("tenant", NpgsqlDbType.Text) { Value = (object?)tenantSlug ?? DBNull.Value });
        cmd.Parameters.Add(new NpgsqlParameter("status", NpgsqlDbType.Text) { Value = (object?)status ?? DBNull.Value });
        cmd.Parameters.Add(new NpgsqlParameter("assignee", NpgsqlDbType.Text) { Value = (object?)assignee ?? DBNull.Value });
        cmd.Parameters.Add(new NpgsqlParameter("lim", NpgsqlDbType.Integer) { Value = limit });
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var results = new List<KanbanTask>();
        while (await reader.ReadAsync(ct))
        {
            results.Add(ReadTask(reader));
        }
        return results;
    }

    public async Task<KanbanTask?> GetTaskAsync(string id, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            SELECT t.id, t.tenant_id, ten.slug, t.title, t.body, t.assignee, t.status,
                   t.priority, t.created_by, t.created_at, t.started_at, t.completed_at,
                   t.workspace_kind, t.workspace_path, t.branch_name, t.result,
                   t.consecutive_failures, t.worker_pid, t.last_failure_error,
                   t.max_runtime_seconds, t.last_heartbeat_at, t.current_run_id,
                   t.workflow_template_id, t.current_step_key, t.skills,
                   t.model_override, t.max_retries, t.session_id, t.goal_mode, t.goal_max_turns
            FROM hermes_kanban.tasks t
            JOIN hermes_kanban.tenants ten ON ten.id = t.tenant_id
            WHERE t.id = @id
            """, conn);
        cmd.Parameters.AddWithValue("id", id);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct) ? ReadTask(reader) : null;
    }

    // ====================================================================
    // Dispatcher (the killer pattern)
    // ====================================================================

    /// <summary>
    /// Atomically claim the next ready task for the given assignee.
    /// Returns the claimed task, or null if another worker grabbed it
    /// first. The SKIP LOCKED CTE means we don't block on busy rows.
    /// </summary>
    public async Task<KanbanTask?> ClaimNextAsync(
        string assignee, int workerPid, int? maxRuntimeSeconds = null,
        CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            WITH next AS (
                SELECT id FROM hermes_kanban.tasks
                WHERE status = 'ready'
                  AND (assignee IS NULL OR assignee = @assignee)
                ORDER BY priority DESC, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE hermes_kanban.tasks t
            SET status = 'running',
                started_at = now(),
                worker_pid = @worker_pid,
                assignee = @assignee,
                max_runtime_seconds = COALESCE(@max_runtime, max_runtime_seconds),
                last_heartbeat_at = now()
            FROM next
            WHERE t.id = next.id
            RETURNING t.id, t.tenant_id, (SELECT slug FROM hermes_kanban.tenants WHERE id = t.tenant_id),
                      t.title, t.body, t.assignee, t.status, t.priority, t.created_by,
                      t.created_at, t.started_at, t.completed_at, t.workspace_kind,
                      t.workspace_path, t.branch_name, t.result, t.consecutive_failures,
                      t.worker_pid, t.last_failure_error, t.max_runtime_seconds,
                      t.last_heartbeat_at, t.current_run_id, t.workflow_template_id,
                      t.current_step_key, t.skills, t.model_override, t.max_retries,
                      t.session_id, t.goal_mode, t.goal_max_turns
            """, conn);
        cmd.Parameters.AddWithValue("assignee", assignee);
        cmd.Parameters.AddWithValue("worker_pid", workerPid);
        cmd.Parameters.AddWithValue("max_runtime", (object?)maxRuntimeSeconds ?? DBNull.Value);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        return await reader.ReadAsync(ct) ? ReadTask(reader) : null;
    }

    /// <summary>
    /// Worker heartbeat — updates last_heartbeat_at. The dispatcher
    /// checks this to detect dead workers.
    /// </summary>
    public async Task<bool> HeartbeatAsync(string taskId, int workerPid, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            "UPDATE hermes_kanban.tasks SET last_heartbeat_at = now() " +
            "WHERE id = @id AND worker_pid = @pid AND status = 'running'",
            conn);
        cmd.Parameters.AddWithValue("id", taskId);
        cmd.Parameters.AddWithValue("pid", workerPid);
        return await cmd.ExecuteNonQueryAsync(ct) == 1;
    }

    /// <summary>
    /// Mark a task done. Records a task_event and a task_runs row.
    /// </summary>
    public async Task<bool> CompleteAsync(
        string taskId, int workerPid, string summary, string? result = null,
        string? outcome = "completed", CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var tx = await conn.BeginTransactionAsync(ct);
        try
        {
            await using (var upd = new NpgsqlCommand(
                """
                UPDATE hermes_kanban.tasks
                SET status = 'done', completed_at = now(), result = @result,
                    consecutive_failures = 0, last_failure_error = NULL,
                    current_run_id = NULL
                WHERE id = @id AND worker_pid = @pid AND status = 'running'
                """, conn, tx))
            {
                upd.Parameters.AddWithValue("id", taskId);
                upd.Parameters.AddWithValue("pid", workerPid);
                upd.Parameters.AddWithValue("result", (object?)result ?? DBNull.Value);
                if (await upd.ExecuteNonQueryAsync(ct) != 1)
                {
                    await tx.RollbackAsync(ct);
                    return false;
                }
            }
            await InsertRunAndEventAsync(conn, tx, taskId, workerPid, "done", outcome, summary, null, ct);
            await tx.CommitAsync(ct);
            return true;
        }
        catch
        {
            await tx.RollbackAsync(ct);
            throw;
        }
    }

    /// <summary>
    /// Mark a task failed. Increments consecutive_failures and trips
    /// the circuit breaker if it exceeds max_retries.
    /// </summary>
    public async Task<bool> FailAsync(
        string taskId, int workerPid, string error, string? statusOverride = null,
        CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var tx = await conn.BeginTransactionAsync(ct);
        try
        {
            string? nextStatus = null;
            await using (var upd = new NpgsqlCommand(
                """
                UPDATE hermes_kanban.tasks
                SET consecutive_failures = consecutive_failures + 1,
                    last_failure_error = @err,
                    current_run_id = NULL
                WHERE id = @id AND worker_pid = @pid AND status = 'running'
                RETURNING consecutive_failures, max_retries
                """, conn, tx))
            {
                upd.Parameters.AddWithValue("id", taskId);
                upd.Parameters.AddWithValue("pid", workerPid);
                upd.Parameters.AddWithValue("err", error);
                await using var reader = await upd.ExecuteReaderAsync(ct);
                if (!await reader.ReadAsync(ct))
                {
                    await tx.RollbackAsync(ct);
                    return false;
                }
                int failures = reader.GetInt32(0);
                int? maxRetries = reader.IsDBNull(1) ? null : reader.GetInt32(1);
                int limit = maxRetries ?? 3;   // matches SQLite default
                nextStatus = statusOverride ?? (failures >= limit ? "blocked" : "ready");
            }
            // Update status in a separate statement (we already have the row data).
            await using (var setStatus = new NpgsqlCommand(
                "UPDATE hermes_kanban.tasks SET status = @s, worker_pid = NULL, " +
                "started_at = NULL, last_heartbeat_at = NULL WHERE id = @id",
                conn, tx))
            {
                setStatus.Parameters.AddWithValue("s", nextStatus);
                setStatus.Parameters.AddWithValue("id", taskId);
                await setStatus.ExecuteNonQueryAsync(ct);
            }
            await InsertRunAndEventAsync(conn, tx, taskId, workerPid, "failed", null, null, error, ct);
            await tx.CommitAsync(ct);
            return true;
        }
        catch
        {
            await tx.RollbackAsync(ct);
            throw;
        }
    }

    // ====================================================================
    // Comments, events, links
    // ====================================================================

    public async Task<long> AddCommentAsync(string taskId, string author, string body, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            "INSERT INTO hermes_kanban.task_comments (task_id, author, body) " +
            "VALUES (@t, @a, @b) RETURNING id", conn);
        cmd.Parameters.AddWithValue("t", taskId);
        cmd.Parameters.AddWithValue("a", author);
        cmd.Parameters.AddWithValue("b", body);
        return (long)(await cmd.ExecuteScalarAsync(ct))!;
    }

    public async Task<IReadOnlyList<TaskComment>> GetCommentsAsync(string taskId, int limit = 100, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            "SELECT id, task_id, author, body, created_at FROM hermes_kanban.task_comments " +
            "WHERE task_id = @t ORDER BY created_at LIMIT @l", conn);
        cmd.Parameters.AddWithValue("t", taskId);
        cmd.Parameters.AddWithValue("l", limit);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var results = new List<TaskComment>();
        while (await reader.ReadAsync(ct))
        {
            results.Add(new TaskComment(
                Id: reader.GetInt64(0),
                TaskId: reader.GetString(1),
                Author: reader.GetString(2),
                Body: reader.GetString(3),
                CreatedAt: reader.GetFieldValue<DateTime>(4)));
        }
        return results;
    }

    public async Task<IReadOnlyList<TaskEvent>> GetHistoryAsync(string taskId, int limit = 100, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            "SELECT id, task_id, run_id, kind, payload, created_at " +
            "FROM hermes_kanban.task_events WHERE task_id = @t " +
            "ORDER BY created_at DESC LIMIT @l", conn);
        cmd.Parameters.AddWithValue("t", taskId);
        cmd.Parameters.AddWithValue("l", limit);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var results = new List<TaskEvent>();
        while (await reader.ReadAsync(ct))
        {
            results.Add(new TaskEvent(
                Id: reader.GetInt64(0),
                TaskId: reader.GetString(1),
                RunId: reader.IsDBNull(2) ? null : reader.GetInt64(2),
                Kind: reader.GetString(3),
                PayloadJson: reader.IsDBNull(4) ? null : reader.GetString(4),
                CreatedAt: reader.GetFieldValue<DateTime>(5)));
        }
        return results;
    }

    public async Task<bool> LinkAsync(string parentId, string childId, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            "INSERT INTO hermes_kanban.task_links (parent_id, child_id) VALUES (@p, @c) " +
            "ON CONFLICT DO NOTHING", conn);
        cmd.Parameters.AddWithValue("p", parentId);
        cmd.Parameters.AddWithValue("c", childId);
        return await cmd.ExecuteNonQueryAsync(ct) == 1;
    }

    public async Task<bool> UnlinkAsync(string parentId, string childId, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            "DELETE FROM hermes_kanban.task_links WHERE parent_id = @p AND child_id = @c", conn);
        cmd.Parameters.AddWithValue("p", parentId);
        cmd.Parameters.AddWithValue("c", childId);
        return await cmd.ExecuteNonQueryAsync(ct) == 1;
    }

    public async Task<IReadOnlyList<string>> GetChildrenAsync(string parentId, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            "SELECT child_id FROM hermes_kanban.task_links WHERE parent_id = @p ORDER BY child_id", conn);
        cmd.Parameters.AddWithValue("p", parentId);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var results = new List<string>();
        while (await reader.ReadAsync(ct)) results.Add(reader.GetString(0));
        return results;
    }

    public async Task<IReadOnlyList<string>> GetParentsAsync(string childId, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            "SELECT parent_id FROM hermes_kanban.task_links WHERE child_id = @c ORDER BY parent_id", conn);
        cmd.Parameters.AddWithValue("c", childId);
        await using var reader = await cmd.ExecuteReaderAsync(ct);
        var results = new List<string>();
        while (await reader.ReadAsync(ct)) results.Add(reader.GetString(0));
        return results;
    }

    // ====================================================================
    // Notify subscriptions
    // ====================================================================

    public async Task<bool> SubscribeAsync(string taskId, string platform, string chatId, string? threadId = null, string? userId = null, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            """
            INSERT INTO hermes_kanban.notify_subs (task_id, platform, chat_id, thread_id, user_id)
            VALUES (@t, @p, @c, COALESCE(NULLIF(@th, ''), ''), @u)
            ON CONFLICT DO NOTHING
            """, conn);
        cmd.Parameters.AddWithValue("t", taskId);
        cmd.Parameters.AddWithValue("p", platform);
        cmd.Parameters.AddWithValue("c", chatId);
        cmd.Parameters.AddWithValue("th", (object?)threadId ?? DBNull.Value);
        cmd.Parameters.AddWithValue("u", (object?)userId ?? DBNull.Value);
        return await cmd.ExecuteNonQueryAsync(ct) == 1;
    }

    public async Task<bool> UnsubscribeAsync(string taskId, string platform, string chatId, string? threadId = null, CancellationToken ct = default)
    {
        await using var conn = await _ds.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(
            "DELETE FROM hermes_kanban.notify_subs WHERE task_id=@t AND platform=@p AND chat_id=@c AND thread_id=COALESCE(NULLIF(@th,''), '')", conn);
        cmd.Parameters.AddWithValue("t", taskId);
        cmd.Parameters.AddWithValue("p", platform);
        cmd.Parameters.AddWithValue("c", chatId);
        cmd.Parameters.AddWithValue("th", (object?)threadId ?? DBNull.Value);
        return await cmd.ExecuteNonQueryAsync(ct) == 1;
    }

    // ====================================================================
    // Helpers
    // ====================================================================

    private static KanbanTask ReadTask(NpgsqlDataReader r) => new(
        Id: r.GetString(0),
        TenantId: r.GetInt64(1),
        TenantSlug: r.GetString(2),
        Title: r.GetString(3),
        Body: r.IsDBNull(4) ? null : r.GetString(4),
        Assignee: r.IsDBNull(5) ? null : r.GetString(5),
        Status: r.GetString(6),
        Priority: r.GetInt32(7),
        CreatedBy: r.IsDBNull(8) ? null : r.GetString(8),
        CreatedAt: r.GetFieldValue<DateTime>(9),
        StartedAt: r.IsDBNull(10) ? null : r.GetFieldValue<DateTime>(10),
        CompletedAt: r.IsDBNull(11) ? null : r.GetFieldValue<DateTime>(11),
        WorkspaceKind: r.GetString(12),
        WorkspacePath: r.IsDBNull(13) ? null : r.GetString(13),
        BranchName: r.IsDBNull(14) ? null : r.GetString(14),
        Result: r.IsDBNull(15) ? null : r.GetString(15),
        ConsecutiveFailures: r.GetInt32(16),
        WorkerPid: r.IsDBNull(17) ? null : r.GetInt32(17),
        LastFailureError: r.IsDBNull(18) ? null : r.GetString(18),
        MaxRuntimeSeconds: r.IsDBNull(19) ? null : r.GetInt32(19),
        LastHeartbeatAt: r.IsDBNull(20) ? null : r.GetFieldValue<DateTime>(20),
        CurrentRunId: r.IsDBNull(21) ? null : r.GetInt64(21),
        WorkflowTemplateId: r.IsDBNull(22) ? null : r.GetString(22),
        CurrentStepKey: r.IsDBNull(23) ? null : r.GetString(23),
        SkillsJson: r.IsDBNull(24) ? "[]" : r.GetString(24),
        ModelOverride: r.IsDBNull(25) ? null : r.GetString(25),
        MaxRetries: r.IsDBNull(26) ? null : r.GetInt32(26),
        SessionId: r.IsDBNull(27) ? null : r.GetString(27),
        GoalMode: r.GetInt32(28),
        GoalMaxTurns: r.IsDBNull(29) ? null : r.GetInt32(29));

    private static async Task InsertRunAndEventAsync(
        NpgsqlConnection conn, NpgsqlTransaction tx,
        string taskId, int workerPid, string status, string? outcome,
        string? summary, string? error, CancellationToken ct)
    {
        long runId;
        await using (var ins = new NpgsqlCommand(
            """
            INSERT INTO hermes_kanban.task_runs (task_id, status, worker_pid, started_at, ended_at, outcome, summary, error)
            VALUES (@t, @s, @p, now(), now(), @o, @sum, @err)
            RETURNING id
            """, conn, tx))
        {
            ins.Parameters.AddWithValue("t", taskId);
            ins.Parameters.AddWithValue("s", status);
            ins.Parameters.AddWithValue("p", workerPid);
            ins.Parameters.AddWithValue("o", (object?)outcome ?? DBNull.Value);
            ins.Parameters.AddWithValue("sum", (object?)summary ?? DBNull.Value);
            ins.Parameters.AddWithValue("err", (object?)error ?? DBNull.Value);
            runId = (long)(await ins.ExecuteScalarAsync(ct))!;
        }
        await using var ev = new NpgsqlCommand(
            "INSERT INTO hermes_kanban.task_events (task_id, run_id, kind, payload) " +
            "VALUES (@t, @r, @k, @p::jsonb)", conn, tx);
        ev.Parameters.AddWithValue("t", taskId);
        ev.Parameters.AddWithValue("r", runId);
        ev.Parameters.AddWithValue("k", status);
        ev.Parameters.AddWithValue("p", "{}");
        await ev.ExecuteNonQueryAsync(ct);
    }
}

// =====================================================================
// Records
// =====================================================================

public sealed record Tenant(
    long Id, string Slug, string Name, string? Description, string? Icon,
    string? Color, string? DefaultWorkdir, bool Archived);

public sealed record KanbanTaskCreate(
    string Id, string TenantSlug, string Title, string? Body = null,
    string? Assignee = null, string? Status = "ready", int Priority = 0,
    string? CreatedBy = null, string? WorkspaceKind = "scratch",
    string? WorkspacePath = null, string? BranchName = null,
    string? IdempotencyKey = null, string? SkillsJson = null,
    string? ModelOverride = null, int? MaxRetries = null,
    string? SessionId = null, int GoalMode = 0, int? GoalMaxTurns = null);

public sealed record KanbanTask(
    string Id, long TenantId, string TenantSlug, string Title, string? Body,
    string? Assignee, string Status, int Priority, string? CreatedBy,
    DateTime CreatedAt, DateTime? StartedAt, DateTime? CompletedAt,
    string WorkspaceKind, string? WorkspacePath, string? BranchName, string? Result,
    int ConsecutiveFailures, int? WorkerPid, string? LastFailureError,
    int? MaxRuntimeSeconds, DateTime? LastHeartbeatAt, long? CurrentRunId,
    string? WorkflowTemplateId, string? CurrentStepKey, string SkillsJson,
    string? ModelOverride, int? MaxRetries, string? SessionId,
    int GoalMode, int? GoalMaxTurns);

public sealed record TaskComment(long Id, string TaskId, string Author, string Body, DateTime CreatedAt);
public sealed record TaskEvent(long Id, string TaskId, long? RunId, string Kind, string? PayloadJson, DateTime CreatedAt);
