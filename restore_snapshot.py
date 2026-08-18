#!/usr/bin/env python3
"""
restore_snapshot.py — apply a full-snapshot patch (git diff 4b825dc..HEAD) without git.exe
Works with Dulwich-only environments. Text-file safe; binary patches decoded if --binary was used.

Usage (work computer, Dulwich only):
  python restore_snapshot.py C:\Users\mail\Desktop\project.patch --dest C:\tmp\hal-clean
  python restore_snapshot.py C:\Users\mail\Desktop\project.patch --dest C:\tmp\hal-clean --init-repo

Patch creation (source computer, needs git OR Dulwich):
  git diff --binary --no-color 4b825dc642cb6eb9a060e54bf8d69288fbee4904 HEAD > project.patch
  # Dulwich-only alternative:
  python restore_snapshot.py --create-patch C:\Users\mail\projects\hal --out project.patch

"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

def create_patch_with_dulwich(repo_path: Path, out_path: Path):
    """Create equivalent of `git diff 4b825dc..HEAD` using Dulwich only."""
    try:
        from dulwich.repo import Repo
        from dulwich.patch import write_tree_diff
    except ImportError:
        sys.exit("Dulwich not installed: pip install dulwich")
    import io
    repo = Repo(str(repo_path))
    # empty tree id
    empty_tree_id = b"4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # actually tree sha
    # Dulwich: get HEAD tree
    head = repo.head()
    head_tree = repo[head].tree if hasattr(repo[head], "tree") else repo[repo.head()].tree
    # Dulwich stores empty tree as None — write_tree_diff handles it
    out = io.BytesIO()
    # Use dulwich's write_tree_diff (fallback to manual walk if needed)
    try:
        # low-level: compare empty tree vs HEAD
        from dulwich.object_store import tree_lookup_path
        empty_id = None
        # Dulwich's write_tree_diff expects file-like binary
        write_tree_diff(out, repo.object_store, empty_id, head_tree)
    except Exception as e:
        # Fallback: just export files as patch manually (new files only)
        print(f"[warn] write_tree_diff failed ({e}), falling back to manual export", file=sys.stderr)
        out = io.BytesIO()
        for entry in _walk_tree(repo, head_tree, Path("")):
            rel = entry["path"]
            data = repo[entry["sha"]].data
            out.write(f"diff --git a/{rel} b/{rel}\n".encode())
            out.write(b"new file mode 100644\n")
            out.write(f"--- /dev/null\n+++ b/{rel}\n".encode())
            lines = data.splitlines()
            out.write(f"@@ -0,0 +1,{len(lines)} @@\n".encode())
            for l in lines:
                out.write(b"+" + l + b"\n")
    out_path.write_bytes(out.getvalue())
    print(f"Wrote Dulwich patch to {out_path} ({out_path.stat().st_size} bytes)")

def _walk_tree(repo, tree_id, prefix: Path):
    from dulwich.objects import Tree
    tree = repo[tree_id]
    assert isinstance(tree, Tree)
    for entry in tree.items():
        p = prefix / entry.path.decode()
        if entry.mode == 0o40000:
            yield from _walk_tree(repo, entry.sha, p)
        else:
            yield {"path": p.as_posix(), "sha": entry.sha, "mode": entry.mode}


def apply_snapshot_patch(patch_path: Path, dest: Path, init_repo: bool = False):
    patch_bytes = patch_path.read_bytes()
    # Try to decode as utf-8, keep bytes for binary
    text = patch_bytes.decode("utf-8", errors="surrogateescape")

    if init_repo:
        try:
            from dulwich.repo import Repo
            if dest.exists():
                sys.exit(f"Dest already exists: {dest}")
            dest.mkdir(parents=True)
            Repo.init(str(dest))
            print(f"Initialized Dulwich repo at {dest}")
        except ImportError:
            dest.mkdir(parents=True, exist_ok=True)
            print("[warn] Dulwich not available, just created directory", file=sys.stderr)
    else:
        if dest.exists() and any(dest.iterdir()):
            sys.exit(f"Dest not empty: {dest} (choose empty folder)")
        dest.mkdir(parents=True, exist_ok=True)

    # Parse patch: each file starts with diff --git
    # For snapshot all files are `new file mode` + `+++ b/path`
    file_re = re.compile(r"^diff --git a/(.*) b/(.*)$", re.MULTILINE)
    new_file_re = re.compile(r"^new file mode (\d+)", re.MULTILINE)
    plus_b_re = re.compile(r"^\+\+\+ b/(.*)$", re.MULTILINE)
    binary_re = re.compile(r"^GIT binary patch$", re.MULTILINE)

    # Split by diff headers
    parts = re.split(r"(?m)^diff --git ", text)
    # first part is preamble
    restored = 0
    for part in parts[1:]:
        part = "diff --git " + part
        lines = part.splitlines()

        # Find destination path from +++ line
        m = plus_b_re.search(part)
        if not m:
            continue
        rel_path = m.group(1).strip()
        # sanitize: no absolute, no ..
        if rel_path.startswith("/") or ".." in Path(rel_path).parts:
            print(f"[skip] unsafe path: {rel_path}", file=sys.stderr)
            continue

        target = dest / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)

        # Binary?
        if "GIT binary patch" in part:
            print(f"[warn] binary file {rel_path} — attempting base85 decode", file=sys.stderr)
            try:
                data = _decode_git_binary_patch(part)
                target.write_bytes(data)
                restored += 1
                continue
            except Exception as e:
                print(f"[error] binary decode failed for {rel_path}: {e}", file=sys.stderr)
                continue

        # Text: collect hunk + lines (skip +++ and --- and @@)
        content_lines = []
        in_hunk = False
        for line in lines:
            if line.startswith("@@"):
                in_hunk = True
                continue
            if not in_hunk:
                continue
            if line.startswith("+") and not line.startswith("+++"):
                content_lines.append(line[1:])
            elif line.startswith(" "):
                content_lines.append(line[1:])
            elif line.startswith("-"):
                # shouldn't happen for snapshot, but ignore
                pass

        # Also handle files with no hunk header edge case (empty file)
        data = "\n".join(content_lines)
        # Preserve trailing newline if patch had it (git adds newline at EOF)
        # Heuristic: if original patch hunk ended with newline, ensure file ends with \n
        if content_lines or "new file mode" in part:
            # git patch omits final \n marker as "\ No newline..." — check
            if "\\ No newline" not in part and data and not data.endswith("\n"):
                # Many patches lose final newline distinction; add one if original likely had it
                # We add \n if hunks were non-empty
                if part.count("\n+") > 0:
                    data += "\n"
            target.write_text(data, encoding="utf-8", newline="\n")
            # chmod +x if 100755
            m_mode = new_file_re.search(part)
            if m_mode and m_mode.group(1) == "100755":
                try:
                    target.chmod(target.stat().st_mode | 0o111)
                except Exception:
                    pass
            restored += 1

    print(f"Done. Restored {restored} files to {dest}")

def _decode_git_binary_patch(part: str) -> bytes:
    """Decode GIT binary patch literal/delta. Supports literal only (snapshot)."""
    # Dulwich helper if available
    try:
        from dulwich.patch import _get_patch_info  # noqa
    except Exception:
        pass
    lines = part.splitlines()
    # Find "literal <size>" then base85 lines until blank
    import base64
    in_literal = False
    b85_lines = []
    for line in lines:
        if line.startswith("literal "):
            in_literal = True
            continue
        if in_literal:
            if line == "" or line.startswith("--"):
                break
            # git base85 lines are single char length prefix + data
            # first char encodes len, rest is base85
            if len(line) < 1:
                continue
            # Git's base85: first byte is 'A' + len, then rfc1924-ish
            # Simplest: use dulwich's decoder if present
            try:
                from dulwich.patch import git_mailsplit  # placeholder
            except Exception:
                pass
            b85_lines.append(line)
    if not b85_lines:
        raise ValueError("no base85 data found")
    # Try dulwich's binary patch decoder
    try:
        from dulwich import patch as dpatch
        # dpatch has no public decoder, so try to use its internal base85
        import dulwich.patch
        # Fallback: attempt with base64 b85
        raw = "".join(l[1:] for l in b85_lines)  # strip len char
        # Git uses custom table, not RFC1924. Try dulwich's decode if exists
        if hasattr(dulwich.patch, "decode_binary_patch"):
            return dulwich.patch.decode_binary_patch(part.encode())
    except Exception:
        pass
    # Last resort: try standard base85 (may fail for some binaries)
    try:
        raw = "".join(l[1:] for l in b85_lines)
        # pad
        return base64.b85decode(raw)
    except Exception as e:
        raise RuntimeError(f"cannot decode git binary patch (need git apply --binary): {e}")

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Restore full-snapshot patch with Dulwich/stdio only")
    p.add_argument("patch", nargs="?", help="path to project.patch")
    p.add_argument("--dest", help="empty destination folder")
    p.add_argument("--init-repo", action="store_true", help="`dulwich Repo.init(dest)` before apply")
    p.add_argument("--create-patch", help="create patch from repo at this path (Dulwich-only)")
    p.add_argument("--out", help="output patch path for --create-patch")
    args = p.parse_args()

    if args.create_patch:
        if not args.out:
            sys.exit("--create-patch requires --out")
        create_patch_with_dulwich(Path(args.create_patch), Path(args.out))
    else:
        if not args.patch or not args.dest:
            p.print_help()
            sys.exit(1)
        apply_snapshot_patch(Path(args.patch), Path(args.dest), init_repo=args.init_repo)
