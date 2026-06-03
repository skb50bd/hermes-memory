namespace Hermes.Memory.Core.Models;

public sealed record PreflightCheck(
    string Name,
    bool Pass,
    string? Detail = null,
    bool Blocking = true);

public sealed record PreflightResult(
    bool AllPass,
    IReadOnlyList<PreflightCheck> Checks,
    int PassedCount,
    int FailedCount)
{
    public static PreflightResult From(IReadOnlyList<PreflightCheck> checks)
    {
        var passed = checks.Count(c => c.Pass);
        return new PreflightResult(
            AllPass: passed == checks.Count,
            Checks: checks,
            PassedCount: passed,
            FailedCount: checks.Count - passed);
    }
}
