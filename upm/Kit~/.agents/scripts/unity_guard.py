#!/usr/bin/env python3
"""Guarded Unity validation for an isolated candidate worktree.

Python port of the kit's original PowerShell guard (Invoke-GuardedUnity and
Invoke-GuardedPlayerBuild); usage is documented in the unity-validate and unity-build
skill references. Standard library only.

  unity_guard.py preflight                      bind the attempt to base/head/tree/fingerprint, prove the editor
  unity_guard.py run --label NAME -- CMD...     run one Unity command between protected-content snapshots
  unity_guard.py build --label NAME -- CMD...   player build from a detached linked worktree at AGENT_DELIVERY_COMMIT
  unity_guard.py scan-logs [--dir D]            list error/exception lines from the attempt's (or D's) logs

Every subcommand reads the same environment: AGENT_EVIDENCE_ROOT, AGENT_EVIDENCE_ATTEMPT,
AGENT_META_CHECK, AGENT_TASK_PATHS_FILE, AGENT_BASE_SHA, AGENT_SOURCE_HEAD,
AGENT_CANDIDATE_TREE, AGENT_TASK_FINGERPRINT, and UNITY_EDITOR; build also reads
AGENT_DELIVERY_COMMIT. Run it from the candidate root.
Exit codes: 0 success, 1 validation or guard failure, 2 usage error.
"""

import argparse
import ctypes
import difflib
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys

PROTECTED_ROOTS = ("Assets", "Packages", "ProjectSettings")
OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40,64}$")
LOG_PATTERN = re.compile(r"error CS|Exception|Compilation failed|Build failed|Test run failed", re.IGNORECASE)


class GuardError(Exception):
    pass


def git(root, *args, binary=False):
    proc = subprocess.run(["git", "-C", root] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    out = proc.stdout if binary else proc.stdout.decode("utf-8", errors="replace")
    return proc.returncode, out


def git_lines(root, *args):
    code, out = git(root, *args)
    return code, [line for line in out.splitlines() if line != ""]


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def write_lines(path, lines):
    write_text(path, "".join(line + "\n" for line in lines))


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Settings:
    """Validated guard inputs, shared by every subcommand."""

    def __init__(self):
        env = os.environ
        self.project_root = os.path.realpath(".")
        evidence_root = env.get("AGENT_EVIDENCE_ROOT", "")
        attempt = env.get("AGENT_EVIDENCE_ATTEMPT", "")
        meta_check = env.get("AGENT_META_CHECK", "")
        task_paths = env.get("AGENT_TASK_PATHS_FILE", "")
        values = {
            "AGENT_BASE_SHA": env.get("AGENT_BASE_SHA", ""),
            "AGENT_SOURCE_HEAD": env.get("AGENT_SOURCE_HEAD", ""),
            "AGENT_CANDIDATE_TREE": env.get("AGENT_CANDIDATE_TREE", ""),
            "AGENT_TASK_FINGERPRINT": env.get("AGENT_TASK_FINGERPRINT", ""),
        }

        if not evidence_root.strip():
            raise GuardError("AGENT_EVIDENCE_ROOT is required.")
        if not re.match(r"^[1-9][0-9]*$", attempt):
            raise GuardError("AGENT_EVIDENCE_ATTEMPT must be a positive integer.")
        if not meta_check.strip() or not os.path.exists(meta_check):
            raise GuardError("AGENT_META_CHECK must point to the primary checkout's check_unity_meta.py.")
        if not task_paths.strip() or not os.path.isfile(task_paths):
            raise GuardError("AGENT_TASK_PATHS_FILE must point to the frozen task-path JSON array.")
        for name, value in values.items():
            if not value.strip():
                raise GuardError(name + " is required.")
        for name in ("AGENT_BASE_SHA", "AGENT_SOURCE_HEAD", "AGENT_CANDIDATE_TREE"):
            if not OBJECT_ID.match(values[name]):
                raise GuardError("Base, source HEAD, and candidate tree must be full Git object IDs.")
        if not re.match(r"^[0-9a-fA-F]{64}$", values["AGENT_TASK_FINGERPRINT"]):
            raise GuardError("AGENT_TASK_FINGERPRINT must be a SHA-256 value.")

        self.base_sha = values["AGENT_BASE_SHA"].lower()
        self.source_head = values["AGENT_SOURCE_HEAD"].lower()
        self.candidate_tree = values["AGENT_CANDIDATE_TREE"].lower()
        self.task_fingerprint = values["AGENT_TASK_FINGERPRINT"].lower()
        self.attempt = int(attempt)
        self.evidence_root = os.path.abspath(evidence_root)
        self.meta_check = os.path.realpath(meta_check)
        self.task_paths_file = os.path.realpath(task_paths)

        parent = os.path.dirname(self.evidence_root)
        if os.path.basename(parent).lower() != self.candidate_tree or os.path.basename(self.evidence_root) != "attempt-%d" % self.attempt:
            raise GuardError("AGENT_EVIDENCE_ROOT must end with %s/attempt-%d." % (self.candidate_tree, self.attempt))

    def evidence(self, name):
        return os.path.join(self.evidence_root, name)


# --- Git and fingerprint -------------------------------------------------------

def candidate_index_tree(root):
    code, lines = git_lines(root, "write-tree")
    if code != 0 or len(lines) != 1:
        raise GuardError("Could not compute the candidate index tree.")
    return lines[0].strip().lower()


def assert_object_type(root, object_id, expected, label):
    code, lines = git_lines(root, "cat-file", "-t", object_id)
    if code != 0 or len(lines) != 1 or lines[0].strip() != expected:
        raise GuardError("%s %s is not an available Git %s object." % (label, object_id, expected))


def tree_path_entry(root, tree_id, relative_path):
    code, lines = git_lines(root, "ls-tree", tree_id, "--format=%(objectmode) %(objecttype) %(objectname)", "--", ":(literal)" + relative_path)
    if code != 0:
        raise GuardError("Could not inspect '%s' in tree %s." % (relative_path, tree_id))
    if not lines:
        return None
    match = re.match(r"^(100644|100755|120000) blob ([0-9a-fA-F]{40,64})$", lines[0]) if len(lines) == 1 else None
    if not match:
        raise GuardError("Task path must resolve to one supported Git blob: " + relative_path)
    return match.group(1), match.group(2).lower()


def blob_sha256(root, object_id):
    code, data = git(root, "cat-file", "blob", object_id, binary=True)
    if code != 0:
        raise GuardError("Could not read Git blob " + object_id)
    return hashlib.sha256(data).hexdigest()


def canonical_task_fingerprint(settings):
    with open(settings.task_paths_file, "r", encoding="utf-8-sig") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, list):
        loaded = [loaded]
    if not loaded:
        raise GuardError("Frozen task path set is empty.")

    seen = set()
    for item in loaded:
        path = str(item)
        if (not path.strip() or "\\" in path or path.startswith("/") or path.endswith("/")
                or re.search(r"(^|/)\.\.?(/|$)", path) or "\0" in path):
            raise GuardError("Task paths must be normalized repository-relative file paths: '%s'" % path)
        if path in seen:
            raise GuardError("Duplicate task path: " + path)
        seen.add(path)

    # Records are ordered by the UTF-8 bytes of the path, like the PowerShell guard.
    digest = hashlib.sha256()
    for path in sorted(seen, key=lambda p: p.encode("utf-8")):
        entry = tree_path_entry(settings.project_root, settings.candidate_tree, path)
        if entry is None:
            if tree_path_entry(settings.project_root, settings.base_sha, path) is None:
                raise GuardError("Deleted task path does not exist in the recorded base: " + path)
            mode, raw_hash = "deleted", "-"
        else:
            mode, raw_hash = entry[0], blob_sha256(settings.project_root, entry[1])
        digest.update((path + "\0" + mode + "\0" + raw_hash + "\0").encode("utf-8"))
    return digest.hexdigest()


# --- Editor identity -------------------------------------------------------------

def read_project_version(root):
    with open(os.path.join(root, "ProjectSettings", "ProjectVersion.txt"), "r", encoding="utf-8-sig") as handle:
        lines = handle.read().splitlines()
    version_line = next((l for l in lines if l.startswith("m_EditorVersion:")), "")
    revision_line = next((l for l in lines if l.startswith("m_EditorVersionWithRevision:")), "")
    required_version = version_line.split(":", 1)[1].strip() if ":" in version_line else ""
    match = re.match(r"^m_EditorVersionWithRevision:\s*(\S+)\s+\(([0-9a-fA-F]+)\)\s*$", revision_line)
    if not match:
        raise GuardError("ProjectVersion.txt must contain m_EditorVersionWithRevision for exact editor proof.")
    if match.group(1) != required_version:
        raise GuardError("ProjectVersion.txt editor version lines disagree.")
    return required_version, match.group(2).lower()


def editor_product_version(editor_path):
    """Returns "<version>_<revision>" as recorded in the editor binary's metadata."""
    if sys.platform == "darwin":
        # .../Unity.app/Contents/MacOS/Unity -> .../Unity.app/Contents/Info.plist
        plist_path = os.path.join(os.path.dirname(os.path.dirname(editor_path)), "Info.plist")
        if not os.path.isfile(plist_path):
            raise GuardError("UNITY_EDITOR must be the Unity.app/Contents/MacOS/Unity executable (no Info.plist beside it).")
        with open(plist_path, "rb") as handle:
            info = plistlib.load(handle)
        return "%s_%s" % (info.get("CFBundleVersion", ""), str(info.get("UnityBuildNumber", "")).lower())
    if os.name == "nt":
        return _windows_product_version(editor_path)
    raise GuardError("Exact editor revision proof is only implemented for macOS and Windows editors.")


def _windows_product_version(path):
    version = ctypes.windll.version
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        raise GuardError("Could not read the version resource of " + path)
    buffer = ctypes.create_string_buffer(size)
    version.GetFileVersionInfoW(path, 0, size, buffer)
    pointer, length = ctypes.c_void_p(), ctypes.c_uint()
    version.VerQueryValueW(buffer, "\\VarFileInfo\\Translation", ctypes.byref(pointer), ctypes.byref(length))
    language, codepage = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort * 2)).contents
    key = "\\StringFileInfo\\%04x%04x\\ProductVersion" % (language, codepage)
    if not version.VerQueryValueW(buffer, key, ctypes.byref(pointer), ctypes.byref(length)):
        raise GuardError("Unity binary has no ProductVersion.")
    return ctypes.wstring_at(pointer, length.value - 1)


def prove_editor(settings):
    required_version, required_revision = read_project_version(settings.project_root)
    editor = os.environ.get("UNITY_EDITOR", "")
    if not editor.strip() or not os.path.isfile(editor):
        raise GuardError("UNITY_EDITOR must point to the exact Unity %s executable." % required_version)
    editor = os.path.realpath(editor)
    product_version = editor_product_version(editor)
    expected = "%s_%s" % (required_version, required_revision)
    if product_version != expected:
        raise GuardError("Unity binary ProductVersion '%s' does not match '%s'." % (product_version, expected))

    with open(settings.evidence("unity-version.txt"), "wb") as out, open(settings.evidence("unity-version-error.txt"), "wb") as err:
        exit_code = subprocess.run([editor, "-version"], stdout=out, stderr=err, check=False).returncode
    with open(settings.evidence("unity-version.txt"), "r", encoding="utf-8", errors="replace") as handle:
        reported = handle.read().strip()
    if exit_code != 0 or reported != required_version:
        raise GuardError("Unity -version reported '%s' with exit %d; expected '%s'." % (reported, exit_code, required_version))
    write_text(settings.evidence("unity-editor-product-version.txt"), product_version)
    return editor


# --- Protected content manifest ----------------------------------------------------

def write_protected_manifest(root, output_path):
    records = {}
    for relative_root in PROTECTED_ROOTS:
        absolute_root = os.path.join(root, relative_root)
        if not os.path.lexists(absolute_root):
            continue
        stack = [absolute_root]
        while stack:
            current = stack.pop()
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode):
                raise GuardError("Protected path contains a symlink; refusing traversal: " + current)
            relative = os.path.relpath(current, root).replace(os.sep, "/")
            if stat.S_ISDIR(info.st_mode):
                records[relative] = {"path": relative, "kind": "directory", "length": 0, "sha256": None}
                stack.extend(os.path.join(current, name) for name in os.listdir(current))
            else:
                records[relative] = {"path": relative, "kind": "file", "length": info.st_size, "sha256": sha256_file(current)}
    lines = [json.dumps(records[path], separators=(",", ":")) for path in sorted(records)]
    write_lines(output_path, lines)
    return sha256_file(output_path)


def git_status(root):
    code, lines = git_lines(root, "-c", "core.quotepath=off", "status", "--short", "--untracked-files=all")
    if code != 0:
        raise GuardError("Could not capture the candidate Git status.")
    return lines


# --- Subcommands -------------------------------------------------------------------

def cmd_preflight(settings):
    root = settings.project_root
    if os.path.exists(settings.evidence_root):
        raise GuardError("Evidence attempt directory already exists; use the next attempt number instead of overwriting it.")
    os.makedirs(settings.evidence_root)

    assert_object_type(root, settings.base_sha, "commit", "Base SHA")
    assert_object_type(root, settings.source_head, "commit", "Source HEAD")
    assert_object_type(root, settings.candidate_tree, "tree", "Candidate tree")
    computed = canonical_task_fingerprint(settings)
    if computed != settings.task_fingerprint:
        raise GuardError("Candidate task fingerprint %s does not match AGENT_TASK_FINGERPRINT %s." % (computed, settings.task_fingerprint))
    pre_tree = candidate_index_tree(root)
    if pre_tree != settings.candidate_tree:
        raise GuardError("Candidate index tree %s does not match AGENT_CANDIDATE_TREE %s." % (pre_tree, settings.candidate_tree))
    if git(root, "diff", "--quiet", "--")[0] != 0:
        raise GuardError("Candidate working files differ from the reviewed index tree.")
    code, untracked = git_lines(root, "ls-files", "--others", "--exclude-standard")
    if code != 0:
        raise GuardError("Could not inspect untracked candidate paths.")
    if untracked:
        raise GuardError("Candidate contains untracked non-ignored paths; materialize and stage the complete task scope first.")
    code, ignored = git_lines(root, "ls-files", "--others", "--ignored", "--exclude-standard", "--", *PROTECTED_ROOTS)
    if code != 0:
        raise GuardError("Could not inspect ignored protected paths.")
    if ignored:
        raise GuardError("Candidate contains ignored protected content not represented by its tree; use a fresh candidate worktree.")

    write_text(settings.evidence("candidate-metadata.json"), json.dumps({
        "base_sha": settings.base_sha,
        "source_head": settings.source_head,
        "candidate_tree": settings.candidate_tree,
        "task_content_fingerprint": settings.task_fingerprint,
        "task_paths_file_sha256": sha256_file(settings.task_paths_file),
        "evidence_attempt": settings.attempt,
    }, indent=2))
    editor = prove_editor(settings)
    print("unity-guard: preflight passed; editor " + editor + "; evidence " + settings.evidence_root)


def cmd_run(settings, label, command, after_command=None):
    root = settings.project_root
    if not os.path.isfile(settings.evidence("candidate-metadata.json")):
        raise GuardError("Run 'unity_guard.py preflight' for this attempt first.")
    if not re.match(r"^[A-Za-z0-9._-]+$", label):
        raise GuardError("--label must be a simple file-name token.")
    if not command:
        raise GuardError("Pass the Unity command after '--'.")
    prefix = label + "-"
    for name in ("pre-unity-protected-manifest.jsonl", "post-unity-status.txt"):
        if os.path.exists(settings.evidence(prefix + name)):
            raise GuardError("Label '%s' was already used in this attempt; pick a new label." % label)

    pre_manifest = settings.evidence(prefix + "pre-unity-protected-manifest.jsonl")
    post_manifest = settings.evidence(prefix + "post-unity-protected-manifest.jsonl")
    pre_hash = write_protected_manifest(root, pre_manifest)
    pre_status = git_status(root)
    write_lines(settings.evidence(prefix + "pre-unity-status.txt"), pre_status)
    write_text(settings.evidence(prefix + "command.json"), json.dumps(command, indent=2))

    validation_failure = None
    mutation_failures = []
    try:
        exit_code = subprocess.run(command, cwd=root, check=False).returncode
        if exit_code != 0:
            validation_failure = "%s failed with exit code %d." % (label, exit_code)
    except OSError as exc:
        validation_failure = "%s could not start: %s" % (label, exc)
    finally:
        try:
            if after_command:
                after_command()
        except Exception as exc:  # noqa: BLE001
            validation_failure = ((validation_failure or "") + " Cleanup failed: " + str(exc)).strip()
        try:
            post_hash = write_protected_manifest(root, post_manifest)
            post_status = git_status(root)
            write_lines(settings.evidence(prefix + "post-unity-status.txt"), post_status)
            if pre_hash != post_hash:
                with open(pre_manifest, encoding="utf-8") as a, open(post_manifest, encoding="utf-8") as b:
                    write_text(settings.evidence(prefix + "protected-manifest-delta.txt"),
                               "".join(difflib.unified_diff(a.readlines(), b.readlines(), "pre", "post")))
                mutation_failures.append("Unity changed protected project content; inspect the manifest delta and preserve the candidate.")
            if pre_status != post_status:
                write_lines(settings.evidence(prefix + "git-status-delta.txt"),
                            list(difflib.unified_diff(pre_status, post_status, "pre", "post", lineterm="")))
                mutation_failures.append("Unity changed the candidate Git state; inspect the status delta and preserve the candidate.")
            post_tree = candidate_index_tree(root)
            write_text(settings.evidence(prefix + "post-unity-index-tree.txt"), post_tree)
            if post_tree != settings.candidate_tree:
                mutation_failures.append("Unity changed the candidate index tree; expected %s but found %s." % (settings.candidate_tree, post_tree))
            # The meta check runs as a child process because it exits with its own status code.
            if subprocess.run([sys.executable, settings.meta_check, "--project-root", root], check=False).returncode != 0:
                mutation_failures.append("Unity metadata validation failed.")
        except Exception as exc:  # noqa: BLE001 - every postflight failure blocks the attempt
            mutation_failures.append("Postflight failed: " + str(exc))

    mutation_failure = " ".join(mutation_failures)
    if validation_failure and mutation_failure:
        raise GuardError("Validation failed: %s Mutation guard failed: %s" % (validation_failure, mutation_failure))
    if mutation_failure or validation_failure:
        raise GuardError(mutation_failure or validation_failure)
    print("unity-guard: %s passed; protected content manifest unchanged." % label)


ADDRESSABLES_PROBES = (
    "Assets/StreamingAssets/aa/.agent-guard-probe",
    "Assets/StreamingAssets/aa.meta",
    "Assets/AddressableAssetsData/AgentGuard/agent.bin",
)


def is_ignored(root, relative):
    return git(root, "check-ignore", "-q", "--no-index", "--", relative)[0] == 0


def cmd_build(settings, label, command):
    """Player build for Prepare delivery: proves a detached linked worktree at the
    reviewed commit, then runs the guarded command and removes only newly generated,
    ignored Addressables player data before the postflight."""
    root = settings.project_root
    delivery = os.environ.get("AGENT_DELIVERY_COMMIT", "")
    if not OBJECT_ID.match(delivery):
        raise GuardError("AGENT_DELIVERY_COMMIT must be the full reviewed local delivery commit ID.")
    delivery = delivery.lower()
    assert_object_type(root, delivery, "commit", "Delivery commit")

    code, head = git_lines(root, "rev-parse", "--verify", "HEAD")
    if code != 0 or len(head) != 1 or head[0].strip().lower() != delivery:
        raise GuardError("Build worktree HEAD must equal AGENT_DELIVERY_COMMIT %s." % delivery)
    if git(root, "symbolic-ref", "-q", "HEAD")[0] == 0:
        raise GuardError("Build worktree must be detached at the delivery commit.")
    code, tree = git_lines(root, "rev-parse", "--verify", delivery + "^{tree}")
    if code != 0 or len(tree) != 1 or tree[0].strip().lower() != settings.candidate_tree:
        raise GuardError("Delivery commit tree must equal AGENT_CANDIDATE_TREE %s." % settings.candidate_tree)
    code_a, git_dir = git_lines(root, "rev-parse", "--absolute-git-dir")
    code_b, common_dir = git_lines(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if code_a != 0 or code_b != 0 or len(git_dir) != 1 or len(common_dir) != 1:
        raise GuardError("Could not prove the build checkout is a linked worktree.")
    git_dir = os.path.abspath(git_dir[0].strip()).rstrip("/\\")
    common_dir = os.path.abspath(common_dir[0].strip()).rstrip("/\\")
    if os.path.normcase(git_dir) == os.path.normcase(common_dir):
        raise GuardError("Player builds for Prepare delivery must run from a separate linked worktree, not the primary checkout.")
    write_text(settings.evidence(label + "-build-commit-preflight.json"), json.dumps({
        "delivery_commit": delivery, "delivery_tree": settings.candidate_tree, "detached": True,
        "git_dir": git_dir, "git_common_dir": common_dir,
    }, indent=2))

    with open(os.path.join(root, "Packages", "manifest.json"), "r", encoding="utf-8-sig") as handle:
        uses_addressables = "com.unity.addressables" in (json.load(handle).get("dependencies") or {})
    streaming_root = os.path.join(root, "Assets", "StreamingAssets", "aa")
    streaming_meta = os.path.join(root, "Assets", "StreamingAssets", "aa.meta")
    data_root = os.path.join(root, "Assets", "AddressableAssetsData")

    def addressables_bins():
        for dirpath, _, filenames in os.walk(data_root):
            for name in filenames:
                if ".bin" in name:
                    yield os.path.join(dirpath, name)

    if uses_addressables:
        if os.path.lexists(streaming_root) or os.path.lexists(streaming_meta):
            raise GuardError("Fresh build worktree already contains ignored Addressables player data; refusing cleanup.")
        if any(True for _ in addressables_bins()):
            raise GuardError("Fresh build worktree already contains Addressables .bin output; refusing cleanup.")
        for probe in ADDRESSABLES_PROBES:
            if not is_ignored(root, probe):
                raise GuardError("Expected generated Addressables path is not ignored: " + probe)

    def directories_under(*tops):
        found = set()
        for top in tops:
            for dirpath, _, _ in os.walk(top):
                found.add(dirpath)
        return found

    addressables_tops = (os.path.dirname(streaming_root), data_root)
    pre_existing_dirs = directories_under(*addressables_tops)

    def remove_generated_addressables():
        if not uses_addressables:
            return
        generated = []
        if os.path.isdir(streaming_root):
            for dirpath, _, filenames in os.walk(streaming_root):
                generated += [os.path.relpath(os.path.join(dirpath, n), root).replace(os.sep, "/") for n in filenames]
            shutil.rmtree(streaming_root)
        if os.path.lexists(streaming_meta):
            generated.append("Assets/StreamingAssets/aa.meta")
            os.remove(streaming_meta)
        for path in list(addressables_bins()):
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if not is_ignored(root, relative):
                raise GuardError("Refusing to remove non-ignored Addressables output: " + relative)
            generated.append(relative)
            os.remove(path)
        # Folders the build created only to hold that output are generated too;
        # anything that existed before the build, or is not empty, stays.
        for directory in sorted(directories_under(*addressables_tops) - pre_existing_dirs, key=len, reverse=True):
            if not os.listdir(directory):
                os.rmdir(directory)
                generated.append(os.path.relpath(directory, root).replace(os.sep, "/") + "/")
        write_lines(settings.evidence(label + "-addressables-generated-paths.txt"), generated)

    cmd_run(settings, label, command, after_command=remove_generated_addressables)


def scan_logs(directory, pattern=None, first=0):
    regex = re.compile(pattern, re.IGNORECASE) if pattern else LOG_PATTERN
    found = 0
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".log"):
            continue
        with open(os.path.join(directory, name), "r", encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, start=1):
                if regex.search(line):
                    print("%s:%d:%s" % (name, number, line.rstrip("\n")))
                    found += 1
                    if first and found >= first:
                        return
    print("unity-guard: %d matching log line(s)." % found)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    run = sub.add_parser("run")
    run.add_argument("--label", required=True)
    run.add_argument("argv", nargs=argparse.REMAINDER)
    build = sub.add_parser("build")
    build.add_argument("--label", required=True)
    build.add_argument("argv", nargs=argparse.REMAINDER)
    scan = sub.add_parser("scan-logs")
    scan.add_argument("--dir", default=None, help="log directory (default: the attempt's evidence root)")
    scan.add_argument("--pattern", default=None, help="regex to search for (default: compiler/test failure markers)")
    scan.add_argument("--first", type=int, default=0, help="stop after N matches")
    args = parser.parse_args(argv)

    try:
        if args.command == "scan-logs" and args.dir:
            scan_logs(args.dir, args.pattern, args.first)
            return 0
        settings = Settings()
        if args.command == "preflight":
            cmd_preflight(settings)
        elif args.command in ("run", "build"):
            command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            (cmd_run if args.command == "run" else cmd_build)(settings, args.label, command)
        else:
            scan_logs(settings.evidence_root, args.pattern, args.first)
    except GuardError as exc:
        print("unity-guard: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
