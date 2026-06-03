using System.ComponentModel;
using System.Text.Json;
using Hermes.Memory.Core.Skills;
using ModelContextProtocol.Server;

namespace Hermes.Memory.Core.Mcp;

[McpServerToolType]
public sealed class SkillsTools
{
    private readonly SkillsRepository _repo;
    public SkillsTools(SkillsRepository repo) => _repo = repo;

    [McpServerTool(Name = "skill_index_search"), Description("Search the installed-skills index. Returns matching skill names + descriptions.")]
    public async Task<string> IndexSearch(
        [Description("The search query (use '' to list all)")] string query = "",
        [Description("Max results (default 20)")] int top_k = 20,
        CancellationToken ct = default)
    {
        var hits = await _repo.IndexSearchAsync(query, top_k, ct);
        return JsonSerializer.Serialize(hits, JsonOpts);
    }

    [McpServerTool(Name = "skill_register"), Description("Register or update a skill in the catalog index. Idempotent on (name, version).")]
    public async Task<string> Register(
        [Description("Skill name (unique)")] string name,
        [Description("Skill version (semver string)")] string version,
        [Description("Optional owner (user or org)")] string? owner = null,
        [Description("Optional short description")] string? description = null,
        [Description("Optional tags")] string[]? tags = null,
        CancellationToken ct = default)
    {
        var id = await _repo.RegisterAsync(name, version, owner, description, tags, ct);
        return $"Skill '{name}' v{version} registered with id {id}";
    }

    [McpServerTool(Name = "skill_link"), Description("Add a relationship between two skills. kind ∈ {depends_on, supersedes, related, see_also}.")]
    public async Task<string> Link(
        [Description("Source skill name")] string source_skill,
        [Description("Target skill name")] string target_skill,
        [Description("Relationship kind")] string kind,
        CancellationToken ct = default)
    {
        var ok = await _repo.LinkAsync(source_skill, target_skill, kind, ct);
        return ok ? $"Linked {source_skill} --{kind}--> {target_skill}" : $"Could not link — check both skills exist and kind is valid";
    }

    [McpServerTool(Name = "skill_graph"), Description("Walk the skill-link graph N hops from a starting skill.")]
    public async Task<string> Graph(
        [Description("Root skill name")] string root_skill,
        [Description("Max hop depth (default 2)")] int max_hops = 2,
        CancellationToken ct = default)
    {
        var edges = await _repo.GraphAsync(root_skill, max_hops, ct);
        return JsonSerializer.Serialize(edges, JsonOpts);
    }

    private static readonly JsonSerializerOptions JsonOpts = new() { WriteIndented = true };
}
