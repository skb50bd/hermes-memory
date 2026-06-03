namespace Hermes.Memory.Core.Models;

public sealed record VersionInfo(
    string Version,
    string BuildSha,
    string Runtime,
    bool AotCompiled,
    long BinarySizeBytes);
