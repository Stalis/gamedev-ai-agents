# Guarded Unity Validation Commands

Prefer project-specific commands. These bash templates are conservative fallbacks for an isolated candidate worktree created by `$crossworking`. Do not use a shared dirty checkout for delivery-grade evidence. The guard logic lives in `.agents/scripts/unity_guard.py` (Python 3, standard library only); call it from the primary checkout because the candidate may not contain portable Agent Kit files.

## Preflight And Guard

Set `AGENT_EVIDENCE_ROOT` to a new persistent ignored directory in the primary checkout named `<candidate-tree>/attempt-<n>`, and set `AGENT_EVIDENCE_ATTEMPT` to that positive attempt number. The directory must not already exist, so retries cannot overwrite earlier evidence. Set `AGENT_META_CHECK` to the primary checkout's `.agents/scripts/check_unity_meta.py`. Store the frozen UTF-8 JSON array of repository-relative file paths beside the attempt directories (for example `<candidate-tree>/task-paths.json`) and point `AGENT_TASK_PATHS_FILE` to it; expand the path set across the base/current union so deletions are explicit. Also set `AGENT_BASE_SHA`, `AGENT_SOURCE_HEAD`, `AGENT_CANDIDATE_TREE`, and `AGENT_TASK_FINGERPRINT` to the full recorded values, and `UNITY_EDITOR` to the editor executable (macOS: `/Applications/Unity/Hub/Editor/<version>/Unity.app/Contents/MacOS/Unity`; `unity editors path <version>` prints the install folder). The guard refuses placeholders, a candidate index tree that does not exactly match the expected tree, or a fingerprint that cannot be reproduced from the frozen path set and candidate-tree blobs.

```bash
set -euo pipefail
cd "<candidate-worktree>"
GUARD="<primary-checkout>/.agents/scripts/unity_guard.py"
export AGENT_META_CHECK="<primary-checkout>/.agents/scripts/check_unity_meta.py"
export AGENT_EVIDENCE_ROOT="<primary-checkout>/<evidence-dir>/<candidate-tree>/attempt-<n>"
export AGENT_EVIDENCE_ATTEMPT="<n>"
export AGENT_TASK_PATHS_FILE="<primary-checkout>/<evidence-dir>/<candidate-tree>/task-paths.json"
export AGENT_BASE_SHA="<full base sha>" AGENT_SOURCE_HEAD="<full source head>"
export AGENT_CANDIDATE_TREE="<full candidate tree>" AGENT_TASK_FINGERPRINT="<sha-256>"
export UNITY_EDITOR="<exact editor executable>"

python3 "$GUARD" preflight
```

`preflight` creates the attempt directory, verifies the base and source HEAD commits and the candidate tree object, recomputes the task-content fingerprint (records of `path NUL mode NUL sha256(raw blob) NUL`, ordered by UTF-8 bytes, `deleted`/`-` for paths absent from the candidate tree but present in the base), and requires the index tree to equal the candidate tree with no working-file diff, no untracked non-ignored paths, and no ignored content under `Assets/`, `Packages/`, or `ProjectSettings/`. It writes `candidate-metadata.json`, then proves the editor: the binary's recorded version and revision (macOS `Info.plist` `CFBundleVersion` + `UnityBuildNumber`, Windows `ProductVersion`) must equal `m_EditorVersionWithRevision` from `ProjectSettings/ProjectVersion.txt`, and `Unity -version` must print the required version.

Each `run` wraps one Unity command. Before it, the guard writes a protected-content manifest (`<label>-pre-unity-protected-manifest.jsonl`) and the Git status; afterwards it writes the post manifest and status, the post index tree, and runs the meta check in a child process. Any protected-content, Git-state, index-tree, or metadata change fails the run and is reported together with the command's own failure. Labels must be unique within an attempt so evidence is never overwritten.

The required metadata binds every run to the recorded base, source HEAD, candidate index tree, and task-content fingerprint. The manifest recursively covers tracked, untracked, and ignored files plus directories under `Assets/`, `Packages/`, and `ProjectSettings/`. It rejects symlinks instead of following them. The before/after Git status and index-tree comparisons also catch mode/index-state changes; they are not substitutes for the raw-byte manifest. The metadata check runs in a child process because it exits with its own status code.

## Compile

```bash
python3 "$GUARD" run --label compile -- \
  "$UNITY_EDITOR" -batchmode -nographics -quit -projectPath "$PWD" -logFile "$AGENT_EVIDENCE_ROOT/UnityCompile.log"
```

Do not add `-accept-apiupdate` without explicit approval.

## Filtered EditMode Tests

```bash
TEST_FILTER="<fully-qualified fixture, namespace, or test>"
case "$TEST_FILTER" in "<"*) echo "Replace the EditMode test filter before running." >&2; exit 1;; esac

python3 "$GUARD" run --label editmode -- \
  "$UNITY_EDITOR" -batchmode -projectPath "$PWD" -runTests -testPlatform EditMode -testFilter "$TEST_FILTER" \
  -testResults "$AGENT_EVIDENCE_ROOT/EditMode.xml" -logFile "$AGENT_EVIDENCE_ROOT/EditMode.log"
```

## Filtered PlayMode Tests

Omit `-nographics` unless the selected tests are known to be headless-safe.

```bash
TEST_FILTER="<fully-qualified fixture, namespace, or test>"
case "$TEST_FILTER" in "<"*) echo "Replace the PlayMode test filter before running." >&2; exit 1;; esac

python3 "$GUARD" run --label playmode -- \
  "$UNITY_EDITOR" -batchmode -projectPath "$PWD" -runTests -testPlatform PlayMode -testFilter "$TEST_FILTER" \
  -testResults "$AGENT_EVIDENCE_ROOT/PlayMode.xml" -logFile "$AGENT_EVIDENCE_ROOT/PlayMode.log"
```

Never combine `-quit` with `-runTests`; the Test Framework exits after the run.

`unity test <project> --mode EditMode --report-format junit --output <path>` (Unity CLI) is an alternative runner for the wrapped command when the project uses it; keep it inside `run` so the guard still applies.

## Evidence Review

After the guarded call, parse result XML and logs; exit code alone is not evidence.

```bash
python3 "$GUARD" scan-logs
```

An intentional task-content mutation is not accepted in place. Return to the primary writer, update scope, materialize a new candidate tree, and rerun validation/review. Never auto-revert or delete a mutated candidate.

Use evidence-specific wording:

- "Unity <version> candidate-tree compile passed; protected content manifest unchanged."
- "Filtered EditMode tests passed: <filter>; results: <path>."
- "Unity changed protected content; validation is blocked and the candidate was preserved."
- "Static inspection only; Unity compilation remains unverified."
