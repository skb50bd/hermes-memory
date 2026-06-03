using System.CommandLine.Invocation;
using Hermes.Memory.Core.Models;

namespace Hermes.Memory.Cli;

public static class VersionHandler
{
    public static ICommandHandler Create() => CommandHandler.Create(Run);

    public static int Run(InvocationContext ctx)
    {
        var info = new VersionInfo(
            Version: "0.1.0",
            BuildSha: Environment.GetEnvironmentVariable("HERMES_BUILD_SHA") ?? "dev",
            Runtime: "NativeAOT",
            AotCompiled: true,
            BinarySizeBytes: 0);  // populated at build time
        Console.WriteLine(System.Text.Json.JsonSerializer.Serialize(info, new System.Text.Json.JsonSerializerOptions { WriteIndented = true }));
        return 0;
    }
}
