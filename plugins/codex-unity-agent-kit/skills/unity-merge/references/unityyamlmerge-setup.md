# UnityYAMLMerge Setup And Audit Commands

bash templates (macOS/Linux, or Git Bash on Windows). Run from the repo root containing the Unity project.

## Locate UnityYAMLMerge

```bash
ver=$(sed -n 's/^m_EditorVersion: *//p' ProjectSettings/ProjectVersion.txt)
editor_dir=$(unity editors path "$ver" 2>/dev/null | tail -1)   # Unity CLI; empty if not installed
for tool in \
  "$editor_dir/Unity.app/Contents/Helpers/UnityYAMLMerge" \
  "/Applications/Unity/Hub/Editor/$ver/Unity.app/Contents/Helpers/UnityYAMLMerge" \
  "$HOME/Unity/Hub/Editor/$ver/Editor/Data/Tools/UnityYAMLMerge" \
  "/c/Program Files/Unity/Hub/Editor/$ver/Editor/Data/Tools/UnityYAMLMerge.exe"; do
  [ -x "$tool" ] && break
done
[ -x "$tool" ] || find /Applications/Unity "$HOME/Unity" -name 'UnityYAMLMerge*' -type f 2>/dev/null | head -3
echo "$tool"
```

`unity editors --installed` lists every installed editor if the project version is missing. Any installed editor's UnityYAMLMerge can merge YAML from other versions; matching the project version is preferred, not required.

## .gitattributes

Append to the repo's `.gitattributes` (create if missing):

```
*.unity  merge=unityyamlmerge
*.prefab merge=unityyamlmerge
*.asset  merge=unityyamlmerge
```

`*.asset` also matches assets that stay binary even under Force Text (TerrainData, LightingData); the driver fails cleanly on them and leaves a normal conflict.

## Merge driver

```bash
git config merge.unityyamlmerge.name "Unity SmartMerge"
git config merge.unityyamlmerge.driver "'$tool' merge -p %O %B %A %A"
```

Argument order: base, theirs, mine, destination. Exit 0 means fully merged; nonzero leaves the conflict for manual handling.

## Mergetool fallback (optional, interactive)

```bash
git config merge.tool unityyamlmerge
git config mergetool.unityyamlmerge.trustExitCode false
git config mergetool.unityyamlmerge.cmd "'$tool' merge -p \"\$BASE\" \"\$REMOTE\" \"\$LOCAL\" \"\$MERGED\""
git config mergetool.keepBackup false
```

With `trustExitCode false`, git asks after each file whether the merge succeeded; check for leftover conflict markers before answering yes.

`mergespecfile.txt` (same folder as the tool) defines the fallback tool UnityYAMLMerge launches on chunks it cannot resolve. Do not edit it; it is shared machine state. Treat unresolved files as manual cases.

## Per-file smart merge on an existing conflict

Use when the driver was configured after conflicts already appeared:

```bash
git diff --name-only --diff-filter=U
git checkout -m -- "Assets/Scenes/Main.unity"           # re-runs the merge for this path, now through the driver
grep -nE '^(<{7}|={7}|>{7})' "Assets/Scenes/Main.unity"  # empty output = clean
git add "Assets/Scenes/Main.unity"
```

## Manual audit commands

Duplicate fileIDs (must return nothing):

```bash
file="Assets/Scenes/Main.unity"
grep -oE '^--- !u![0-9]+ &[0-9]+' "$file" | sed 's/.*&//' | sort | uniq -d
```

GUID references with no defining `.meta` (slow on large projects):

```bash
grep -oE 'guid: [0-9a-f]{32}' "$file" | cut -c7- | sort -u > /tmp/refs.txt
find Assets Packages -name '*.meta' -exec grep -hoE '^guid: [0-9a-f]{32}' {} + | cut -c7- | sort -u > /tmp/known.txt
comm -23 /tmp/refs.txt /tmp/known.txt
```

Expected misses: built-in resource GUIDs (`0000...`) and registry packages, whose `.meta` files live in `Library/PackageCache`. Investigate anything else.

## .meta conflict resolution

```bash
git checkout --theirs -- "Assets/Art/Rock.png.meta"   # or --ours; keep the GUID other assets reference
guid=$(sed -n 's/^guid: *//p' "Assets/Art/Rock.png.meta")
grep -rl --include='*.unity' --include='*.prefab' --include='*.asset' "guid: $guid" Assets | head -10
git add "Assets/Art/Rock.png.meta"
python3 .agents/scripts/check_unity_meta.py            # confirms no orphan/missing .meta remains
```

Never delete a `.meta` to let Unity regenerate it; the new GUID breaks every existing reference.

## Post-merge verification

Use unity-validate for compile and test templates. Quick log scan after a batchmode compile:

```bash
grep -nE "Broken text asset|Could not extract GUID|missing script|is missing|meta data file" Logs/*.log
```

If Unity MCP or a connected Unity CLI editor (`unity status`) is available, read the Console (`unity command console get --level error`) and open the affected scene instead of relying on logs alone.
