# Unity Upgrade Checklist

bash templates (macOS/Linux, or Git Bash on Windows). Run from the Unity project root. Point `UNITY_EDITOR` at the TARGET editor executable for each stage (macOS: `/Applications/Unity/Hub/Editor/<version>/Unity.app/Contents/MacOS/Unity`). The Unity CLI installs targets with `unity install <version> --yes --accept-eula` and lists them with `unity editors --installed`.

## Snapshot before anything

```bash
test -z "$(git status --porcelain)" || { echo "working tree must be clean" >&2; exit 1; }
git switch -c upgrade/unity-<target>
mkdir -p Logs
cp ProjectSettings/ProjectVersion.txt Logs/ProjectVersion.before.txt
cp Packages/packages-lock.json Logs/packages-lock.before.json
git rev-parse HEAD > Logs/upgrade-base-commit.txt
```

## Staged upgrade order

| Stage | Move                                       | Risk   | Gate before next stage                 |
| ----- | ------------------------------------------ | ------ | -------------------------------------- |
| 1     | current -> latest patch of same minor      | low    | compile + EditMode                      |
| 2     | -> next minor                              | medium | compile + EditMode + PlayMode           |
| 3     | -> next major (one at a time, prefer LTS)  | high   | full ladder + key scenes + build check  |

Repeat stage 3 once per major. Commit after every green gate. Never fold feature work into a stage commit.

## Editor stage: first open and compile check

```bash
"$UNITY_EDITOR" -batchmode -quit -accept-apiupdate -projectPath "$PWD" -logFile "Logs/Upgrade-editor.log"
grep -niE "error CS|Compilation failed|API Updater|obsolete" Logs/Upgrade-editor.log
cat ProjectSettings/ProjectVersion.txt        # confirm the version actually moved
```

Prefer an interactive first open when a desktop session is available so API Updater prompts and import progress are visible (`unity open . --editor-version <target>`); `-accept-apiupdate` is the headless equivalent. The first import can take a long time; do not kill the process on a timeout without checking the log tail.

## Package stage: one bump at a time

Edit exactly one entry in `Packages/manifest.json`, then:

```bash
"$UNITY_EDITOR" -batchmode -quit -projectPath "$PWD" -logFile "Logs/Upgrade-pkg-<name>.log"
git diff -- Packages/packages-lock.json               # only the requested package and its declared deps should move
```

With a connected editor, `unity command package add --identifier com.unity.<name>@<version> --confirm true --wait true` performs the same single bump live.

Changelog of the currently cached version (the target version's changelog lives on docs.unity3d.com):

```bash
head -n 80 "$(ls -d Library/PackageCache/com.unity.<name>@*/ | head -1)CHANGELOG.md"
```

## Churn diff triage

```bash
git status --porcelain | wc -l                                         # churn size
git diff --stat -- '*.meta' | tail -n 3                                # .meta churn volume
git diff -- '*.meta' | grep -E '^[+-]guid:'                            # ANY hit is a red flag
git diff -- '*.unity' '*.prefab' '*.asset' | grep -E '^[+-].*serializedVersion' | head -n 5
git diff --stat -- '*.unity' '*.prefab'                                # concentrated churn = read it by hand
python3 .agents/scripts/check_unity_meta.py                            # orphan/missing .meta in the churn
```

Safe churn is uniform: the same one-or-two-line change repeated across hundreds of files. Concentrated churn (few files, many lines) or any deleted component/reference block requires a manual read before commit.

## Validation ladder per stage

Minimum gate is a clean compile:

```bash
"$UNITY_EDITOR" -batchmode -quit -projectPath "$PWD" -logFile "Logs/Stage-compile.log"
grep -niE "error CS|Compilation failed" Logs/Stage-compile.log
```

Then run EditMode/PlayMode using the unity-validate command templates, open key scenes through unity-mcp, and delegate a player build to unity-build when the stage gate requires it.

## Reporting wording

- "Stage 1 (2022.3.10f1 -> 2022.3.62f1): compile clean, EditMode passed, churn committed (uniform importer bumps)."
- "Stage blocked: API Updater left 12 errors in Assets/ThirdParty/<vendor>; stopping per stop conditions."
- "packages-lock diff shows a transitive bump of com.unity.burst that was not requested; investigating before commit."
- "Compile not verified: UNITY_EDITOR not set and target editor not installed."
