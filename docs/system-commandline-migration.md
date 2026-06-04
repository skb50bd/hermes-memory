# System.CommandLine Migration Guide

## Current State

The CLI uses `System.CommandLine` **2.0.0-beta1.21072.2** (pinned in `Directory.Packages.props`).
This beta version has a fragile API surface that differs from both 1.0 and 2.0 GA.

## The Problem

The beta API lacks:
- `Option<T>(string name, string description, T defaultValue)` — the `defaultValue` parameter doesn't exist
- `Option.SetDefaultValue()` — method doesn't exist on `Option<T>`
- Mixed collection + property initializers on `Command` — fails with CS0747

## The Workaround (Current)

We use these patterns that work in beta1.21072.2:

```csharp
// ✅ Works: construct Option, add to Command, set Handler
var opt = new Option<string>("--name", "Description");
opt.Argument.SetDefaultValue("default");  // Set default via Argument base class
var cmd = new Command("subcommand");
cmd.Add(opt);
cmd.Handler = CommandHandler.Create<string>((name) => { ... });

// ❌ Fails: collection initializer + property assignment
var cmd = new Command("sub") { opt, Handler = ... };  // CS0747

// ❌ Fails: SetDefaultValue on Option<T>
opt.SetDefaultValue("default");  // Method not found
```

## Migration Path to 2.0 GA

When upgrading to `System.CommandLine` 2.0.0-beta4.22272.1 or later:

1. **Update `Directory.Packages.props`**:
   ```xml
   <PackageVersion Include="System.CommandLine" Version="2.0.0-beta4.22272.1" />
   ```

2. **Replace `Argument.SetDefaultValue()`** with `Option.SetDefaultValue()`:
   ```csharp
   // Before (beta1)
   opt.Argument.SetDefaultValue("default");
   
   // After (beta4+)
   opt.SetDefaultValue("default");
   ```

3. **Replace `CommandHandler.Create` with `SetAction`**:
   ```csharp
   // Before (beta1)
   cmd.Handler = CommandHandler.Create<string>((name) => { ... });
   
   // After (beta4+)
   cmd.SetAction((ctx) => {
       var name = ctx.ParseResult.GetValueForOption(opt);
       // ...
   });
   ```

4. **Use `ParseResult.GetValueForOption<T>()`** instead of direct parameter binding:
   ```csharp
   // Before (beta1)
   cmd.Handler = CommandHandler.Create<string, int>((name, count) => { ... });
   
   // After (beta4+)
   cmd.SetAction((ctx) => {
       var name = ctx.ParseResult.GetValueForOption(nameOpt);
       var count = ctx.ParseResult.GetValueForOption(countOpt);
       // ...
   });
   ```

5. **Add `ArgumentArity` for required options**:
   ```csharp
   // Before (beta1)
   var opt = new Option<string>("--name", "Description") { IsRequired = true };
   
   // After (beta4+)
   var opt = new Option<string>("--name", "Description");
   opt.Arity = ArgumentArity.ExactlyOne;  // Required
   ```

## Test After Migration

```bash
cd ~/repos/hermes-memory
dotnet build -c Release --nologo
dotnet test -c Release --no-build --nologo
hermes-memory --help
hermes-memory version
hermes-memory migrate --help
hermes-memory embed --help
```

## Notes

- The `System.CommandLine` 2.0 GA is still in preview. Monitor for stable release.
- The `System.CommandLine.NamingConventionBinder` package may be needed for `CommandHandler.Create` compatibility.
- Consider migrating to `System.CommandLine` 2.0 GA when it ships stable, or switch to a simpler CLI framework like `Cocona` or `Spectre.Console` if the API continues to churn.
