using System.CommandLine.Invocation;
using System.Net.Http.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;

namespace Hermes.Memory.Cli;

public static class EmbedHandler
{
    public static ICommandHandler Create() =>
        CommandHandler.Create<string, int>(async (text, dim) =>
        {
            var apiKey = Environment.GetEnvironmentVariable("KIMI_API_KEY");
            if (apiKey is null)
            {
                Console.Error.WriteLine("KIMI_API_KEY not set; can't embed.");
                return 1;
            }
            using var http = new HttpClient();
            http.DefaultRequestHeaders.Authorization = new("Bearer", apiKey);
            using var req = new HttpRequestMessage(HttpMethod.Post, "https://api.kimi.com/coding/v1/embeddings");
            req.Content = JsonContent.Create(new { model = "bge_m3_embed", input = text });
            using var resp = await http.SendAsync(req);
            resp.EnsureSuccessStatusCode();
            var body = await resp.Content.ReadFromJsonAsync<EmbedResponse>();
            var v = body?.Data?[0].Embedding;
            if (v is null) { Console.Error.WriteLine("Empty response."); return 1; }
            if (v.Length != dim) { Console.Error.WriteLine($"Dim mismatch: provider returned {v.Length}, expected {dim}"); return 1; }
            // Print first 8 dims + length
            var preview = string.Join(", ", v.Take(8).Select(x => x.ToString("F4")));
            Console.WriteLine($"dim={v.Length} first8=[{preview}, ...] provider=kimi model=bge_m3_embed");
            return 0;
        });
}

public sealed class EmbedResponse
{
    [JsonPropertyName("data")] public EmbedDatum[]? Data { get; set; }
}
public sealed class EmbedDatum
{
    [JsonPropertyName("embedding")] public float[]? Embedding { get; set; }
}
