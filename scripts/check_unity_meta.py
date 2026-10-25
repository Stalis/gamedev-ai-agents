#!/usr/bin/env python3
"""Unity .meta / GUID hygiene check. Python port of check-unity-meta.ps1.

Quick mode (default) checks only files touched per git status; --full scans all of Assets/.
Standard library only.
"""

import argparse
import os
import re
import subprocess
import sys

GUID_PATTERN = re.compile(r"^guid:\s*([0-9a-f]{32})", re.IGNORECASE)
FORBIDDEN_PREFIXES = ("Library/", "Temp/", "Logs/", "UserSettings/", "Build/", "Builds/")


def get_git_status(root):
    # core.quotepath=off keeps non-ASCII paths raw instead of octal-escaped.
    try:
        proc = subprocess.run(
            ["git", "-C", root, "-c", "core.quotepath=off", "status", "--porcelain"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    lines = proc.stdout.decode("utf-8", errors="replace").splitlines()
    return [line for line in lines if line]


def get_status_path(line):
    # Extracts the current path from one porcelain line; renames report "old -> new".
    rel = line[3:]
    arrow = rel.find(" -> ")
    if arrow >= 0:
        rel = rel[arrow + 4:]
    return rel.strip('"')


class Checker:
    def __init__(self, root):
        self.root = root
        self.assets_root = os.path.join(root, "Assets")
        self.issues = 0

    def rel(self, path):
        return os.path.relpath(path, self.root)

    def needs_meta(self, path):
        name = os.path.basename(path)
        # Unity ignores hidden files/folders, dot-prefixed, tilde-suffixed, and .meta itself.
        if name.startswith(".") or name.endswith("~") or name.endswith(".meta"):
            return False
        # Anything inside a hidden/ignored ancestor is also ignored.
        relative = os.path.relpath(path, self.assets_root)
        for segment in re.split(r"[\\/]", relative):
            if segment.startswith(".") or segment.endswith("~"):
                return False
        return True

    def check_pair(self, path):
        if path.endswith(".meta"):
            if not os.path.exists(path[:-5]):
                print("ORPHAN META: %s has no matching asset" % self.rel(path))
                self.issues += 1
        elif self.needs_meta(path):
            if not os.path.exists(path + ".meta"):
                print("MISSING META: %s" % self.rel(path))
                self.issues += 1

    def check_tree(self, top):
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames.sort()
            for name in dirnames + sorted(filenames):
                self.check_pair(os.path.join(dirpath, name))

    def full_scan(self):
        self.check_tree(self.assets_root)
        # Duplicate GUID scan (full mode only - it reads every .meta). Hidden
        # entries are skipped, like Get-ChildItem without -Force.
        guid_map = {}
        for dirpath, dirnames, filenames in os.walk(self.assets_root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith(".") or not name.endswith(".meta"):
                    continue
                meta_path = os.path.join(dirpath, name)
                guid = None
                try:
                    with open(meta_path, "r", encoding="utf-8", errors="replace") as handle:
                        for line in handle:
                            match = GUID_PATTERN.match(line)
                            if match:
                                guid = match.group(1)
                                break
                except OSError:
                    continue
                if not guid:
                    continue
                if guid in guid_map:
                    print("DUPLICATE GUID: %s in %s and %s" % (guid, self.rel(meta_path), guid_map[guid]))
                    self.issues += 1
                else:
                    guid_map[guid] = self.rel(meta_path)

    def quick_scan(self, status):
        for line in status or []:
            if "D" in line[:2]:
                continue
            rel = get_status_path(line)
            if not rel.replace("\\", "/").startswith("Assets/"):
                continue
            full = os.path.join(self.root, rel)
            if not os.path.exists(full):
                continue
            # Untracked directories appear as a single "?? dir/" line; check contents.
            if os.path.isdir(full):
                full = full.rstrip("/\\")
                self.check_pair(full)
                self.check_tree(full)
            else:
                self.check_pair(full)

    def forbidden_paths(self, status):
        # Pending changes must never touch Unity's generated folders.
        for line in status or []:
            rel = get_status_path(line)
            for prefix in FORBIDDEN_PREFIXES:
                if rel.startswith(prefix):
                    print("FORBIDDEN PATH IN DIFF: %s" % rel)
                    self.issues += 1
                    break
            # Compiler artifact folders are forbidden anywhere in the tree, not only
            # at the root (embedded packages and nested csproj builds generate them).
            if re.search(r"(^|/)obj/", rel, re.IGNORECASE):
                print("FORBIDDEN PATH IN DIFF: %s" % rel)
                self.issues += 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", "-ProjectRoot", "-TargetProject", dest="project_root", default=".")
    parser.add_argument("--full", "-Full", dest="full", action="store_true")
    args = parser.parse_args(argv)

    root = os.path.realpath(args.project_root)
    if not os.path.isdir(os.path.join(root, "Assets")):
        print("No Assets/ folder under %s - not a Unity project root." % root, file=sys.stderr)
        return 1

    checker = Checker(root)
    status = get_git_status(root)
    if args.full:
        checker.full_scan()
    else:
        checker.quick_scan(status)
    checker.forbidden_paths(status)

    if checker.issues == 0:
        print("check-unity-meta: clean")
        return 0
    print("check-unity-meta: %d issue(s) found" % checker.issues)
    return 1


if __name__ == "__main__":
    sys.exit(main())
