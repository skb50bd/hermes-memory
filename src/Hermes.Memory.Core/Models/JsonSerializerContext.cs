using System.Text.Json.Serialization;

namespace Hermes.Memory.Core.Models;

/// <summary>
/// The single source of truth for AOT-friendly JSON serialization. Every
/// DTO record that crosses a JSON boundary (MCP tool args, MCP tool results,
/// embedding HTTP requests/responses) MUST be registered here. Adding a
/// tool? Register its args/result types here or trim will drop them.
/// </summary>
[JsonSourceGenerationOptions(
    WriteIndented = false,
    PropertyNamingPolicy = JsonKnownNamingPolicy.SnakeCaseLower,
    DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull)]
[JsonSerializable(typeof(McpError))]
[JsonSerializable(typeof(VersionInfo))]
[JsonSerializable(typeof(PreflightResult))]
[JsonSerializable(typeof(PreflightCheck))]
public partial class JsonSerializerContext : System.Text.Json.Serialization.JsonSerializerContext
{
}
