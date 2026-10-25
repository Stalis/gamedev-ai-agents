# Unity Build Commands

Prefer project-specific commands when they exist. These templates are fallbacks.

For game-pipeline Prepare delivery, run these commands only in a separate clean detached worktree at the reviewed local commit. Every template goes through `.agents/scripts/unity_guard.py build` (Python 3, standard library only; see `$unity-validate`'s `references/validation-commands.md` for the shared guard). The guard performs its postflight in a `finally` block, so a failed Unity process cannot bypass source-mutation detection. A missing build entry point returns the task to Execute; do not create or edit one during Prepare delivery.

## Environment

Set `UNITY_EDITOR` to the full Unity executable path matching `ProjectSettings/ProjectVersion.txt` (macOS: `/Applications/Unity/Hub/Editor/<version>/Unity.app/Contents/MacOS/Unity`; `unity editors path <version>` prints the install folder):

```bash
set -euo pipefail
cd "<build-worktree>"
GUARD="<primary-checkout>/.agents/scripts/unity_guard.py"
export UNITY_EDITOR="<path-to-unity-editor-executable>"
export AGENT_META_CHECK="<primary-checkout>/.agents/scripts/check_unity_meta.py"
export AGENT_BASE_SHA="<full-recorded-base-sha>"
export AGENT_SOURCE_HEAD="<full-recorded-source-head>"
export AGENT_CANDIDATE_TREE="<reviewed-candidate-tree-sha>"
export AGENT_TASK_FINGERPRINT="<reviewed-task-content-sha256>"
export AGENT_TASK_PATHS_FILE="<frozen-task-paths-json>"
export AGENT_EVIDENCE_ATTEMPT="<next-positive-attempt-number>"
export AGENT_EVIDENCE_ROOT="<persistent-ignored-evidence-root>/$AGENT_CANDIDATE_TREE/attempt-$AGENT_EVIDENCE_ATTEMPT"
export AGENT_DELIVERY_COMMIT="<exact-reviewed-local-delivery-commit>"
mkdir -p Builds Logs

python3 "$GUARD" preflight
```

`preflight` confirms that the selected editor is the exact project version. Do not silently fall back to another installed Unity version.

`unity_guard.py build` adds the delivery checks to the guarded run: `HEAD` must equal `AGENT_DELIVERY_COMMIT`, be detached, have the reviewed candidate tree, and live in a linked worktree (not the primary checkout); the proof is written to `<label>-build-commit-preflight.json`. A fresh detached commit worktree normally has no ignored Addressables player data. When Addressables is installed, the guard proves the known generated paths (`Assets/StreamingAssets/aa`, `aa.meta`, `AddressableAssetsData/**/*.bin*`) are absent and ignored, records them, then removes only those newly generated paths before the postflight (`<label>-addressables-generated-paths.txt`). Any other source/settings mutation still blocks the build. Do not use this cleanup in a shared checkout.

## Win64 player

```bash
python3 "$GUARD" build --label win64 -- \
  "$UNITY_EDITOR" -batchmode -quit -projectPath "$PWD" -buildTarget Win64 -executeMethod BuildScript.BuildWin64 -logFile "Logs/BuildWin64.log"
```

## macOS player

Add a `BuildMacOS` entry point beside the others (`Build(BuildTarget.StandaloneOSX, "Builds/macOS/Game.app")`):

```bash
python3 "$GUARD" build --label macos -- \
  "$UNITY_EDITOR" -batchmode -quit -projectPath "$PWD" -buildTarget OSXUniversal -executeMethod BuildScript.BuildMacOS -logFile "Logs/BuildMacOS.log"
```

## Unity 6+ build profile

If the project has BuildProfile assets, prefer them over hand-rolled scripts. `-activeBuildProfile` only selects the profile; a build entry point is still required (`BuildActiveProfile` from the BuildScript below):

```bash
python3 "$GUARD" build --label profile -- \
  "$UNITY_EDITOR" -batchmode -quit -projectPath "$PWD" -activeBuildProfile "Assets/Settings/Build Profiles/Win64.asset" \
  -executeMethod BuildScript.BuildActiveProfile -logFile "Logs/BuildProfile.log"
```

## Android

Supply keystore data through env vars; the build script must copy them into `PlayerSettings` each run (passwords are never persisted). Read secrets from a secret store, never type them into shell history:

```bash
export ANDROID_KEYSTORE_PATH="<path-to-keystore.keystore>"
export ANDROID_KEYSTORE_PASS="<keystore-password>"
export ANDROID_KEYALIAS_NAME="<alias>"
export ANDROID_KEYALIAS_PASS="<alias-password>"
python3 "$GUARD" build --label android -- \
  "$UNITY_EDITOR" -batchmode -quit -projectPath "$PWD" -buildTarget Android -executeMethod BuildScript.BuildAndroid -logFile "Logs/BuildAndroid.log"
```

If any keystore variable is missing for a release build, stop and report it. Do not generate a keystore.

## iOS (export only)

```bash
python3 "$GUARD" build --label ios -- \
  "$UNITY_EDITOR" -batchmode -quit -projectPath "$PWD" -buildTarget iOS -executeMethod BuildScript.BuildIOS -logFile "Logs/BuildIOS.log"
```

Output is an Xcode project directory (`Builds/iOS/`). Archiving and signing happen in Xcode on macOS; report the export as the artifact.

The Unity CLI's `unity build <project> --target <target> --execute-method BuildScript.<Method>` is an alternative wrapped command when the project already uses it; keep it inside `unity_guard.py build` so the guard still applies.

## Minimal BuildScript

Place at `Assets/Editor/BuildScript.cs` (or inside an editor-only asmdef). Keystore assignments dirty in-memory PlayerSettings; do not commit any resulting `ProjectSettings` diff.

This is an Execute-stage authoring example, not permission to add build code during Prepare delivery. After adding it, rematerialize, validate, and review a new candidate before attempting the build.

```csharp
using System;
using System.Linq;
using UnityEditor;
using UnityEditor.Build.Reporting;

public static class BuildScript
{
    static string[] Scenes =>
        EditorBuildSettings.scenes.Where(s => s.enabled).Select(s => s.path).ToArray();

    public static void BuildWin64() =>
        Finish(Build(BuildTarget.StandaloneWindows64, "Builds/Win64/Game.exe"));

    public static void BuildMacOS() =>
        Finish(Build(BuildTarget.StandaloneOSX, "Builds/macOS/Game.app"));

    public static void BuildAndroid()
    {
        bool previousUseCustomKeystore = PlayerSettings.Android.useCustomKeystore;
        string previousKeystoreName = PlayerSettings.Android.keystoreName;
        string previousKeystorePass = PlayerSettings.Android.keystorePass;
        string previousKeyaliasName = PlayerSettings.Android.keyaliasName;
        string previousKeyaliasPass = PlayerSettings.Android.keyaliasPass;

        BuildReport report;
        try
        {
            PlayerSettings.Android.useCustomKeystore = true;
            PlayerSettings.Android.keystoreName = Environment.GetEnvironmentVariable("ANDROID_KEYSTORE_PATH");
            PlayerSettings.Android.keystorePass = Environment.GetEnvironmentVariable("ANDROID_KEYSTORE_PASS");
            PlayerSettings.Android.keyaliasName = Environment.GetEnvironmentVariable("ANDROID_KEYALIAS_NAME");
            PlayerSettings.Android.keyaliasPass = Environment.GetEnvironmentVariable("ANDROID_KEYALIAS_PASS");
            // EditorUserBuildSettings.buildAppBundle = true; // AAB for store release
            report = Build(BuildTarget.Android, "Builds/Android/Game.apk");
        }
        finally
        {
            PlayerSettings.Android.useCustomKeystore = previousUseCustomKeystore;
            PlayerSettings.Android.keystoreName = previousKeystoreName;
            PlayerSettings.Android.keystorePass = previousKeystorePass;
            PlayerSettings.Android.keyaliasName = previousKeyaliasName;
            PlayerSettings.Android.keyaliasPass = previousKeyaliasPass;
        }

        Finish(report);
    }

    public static void BuildIOS() =>
        Finish(Build(BuildTarget.iOS, "Builds/iOS")); // exports an Xcode project directory

#if UNITY_6000_0_OR_NEWER
    // Entry point for the -activeBuildProfile command: builds whatever profile
    // the CLI flag activated. Unity 6+ only (BuildProfile API).
    public static void BuildActiveProfile()
    {
        var profile = UnityEditor.Build.Profile.BuildProfile.GetActiveBuildProfile();
        if (profile == null)
        {
            Console.WriteLine("No active build profile; pass -activeBuildProfile <asset path>.");
            EditorApplication.Exit(1);
            return;
        }
        BuildReport report = BuildPipeline.BuildPlayer(new BuildPlayerWithProfileOptions
        {
            buildProfile = profile,
            locationPathName = "Builds/Profile/Game",
        });
        Console.WriteLine($"Build {report.summary.result}: {report.summary.totalSize} bytes, {report.summary.totalErrors} errors");
        if (report.summary.result != BuildResult.Succeeded)
            EditorApplication.Exit(1);
    }
#endif

    static BuildReport Build(BuildTarget target, string outputPath)
    {
        BuildReport report = BuildPipeline.BuildPlayer(Scenes, outputPath, target, BuildOptions.None);
        Console.WriteLine($"Build {report.summary.result}: {report.summary.totalSize} bytes, {report.summary.totalErrors} errors");
        return report;
    }

    static void Finish(BuildReport report)
    {
        if (report.summary.result != BuildResult.Succeeded)
            EditorApplication.Exit(1);
    }
}
```

## Addressables before player

Requires an asmdef reference to `Unity.Addressables.Editor` (or place next to existing Addressables editor code). Run this instead of the bare player method when the project uses Addressables:

```csharp
using UnityEditor.AddressableAssets.Build;
using UnityEditor.AddressableAssets.Settings;

public static void BuildContentThenWin64()
{
    AddressableAssetSettings.BuildPlayerContent(out AddressablesPlayerBuildResult result);
    if (!string.IsNullOrEmpty(result.Error))
    {
        UnityEngine.Debug.LogError($"Addressables build failed: {result.Error}");
        UnityEditor.EditorApplication.Exit(1);
        return;
    }
    BuildWin64();
}
```

## Log Review

After each command, inspect the exit code and the `-logFile` output:

```bash
# First actionable error (search from the top, not the tail)
python3 "$GUARD" scan-logs --dir Logs --pattern "error CS|Error building Player|UnityLinker|il2cpp|Exception" --first 10

# Success confirmation and size report
grep -E "Build succeeded|Build Report|Total user assemblies" Logs/Build*.log
```

Wording discipline:

- "Build ran: exit 0, artifact at Builds/Win64/Game.exe (183 MB)."
- "Build failed at IL2CPP step: see Logs/BuildWin64.log line N."
- "Not run: ANDROID_KEYSTORE_PATH is not set; next command is the Android template above."
