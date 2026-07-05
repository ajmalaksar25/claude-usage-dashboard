#!/usr/bin/env python3
"""ccm.py — Claude Config Manager.

Manage multiple Claude Code accounts on one machine. Claude Code reads its
config from CLAUDE_CONFIG_DIR, so each account ("profile") is just a
directory: ~/.claude for the default account, ~/.claude-work,
~/.claude-personal, and so on. Credentials stay per-profile; the parts you
actually want everywhere — skills/, agents/, commands/, rules/ — live once
in ~/.claude-shared and are linked into every profile (symlinks on
macOS/Linux, symlinks or directory junctions on Windows).

What this script will NEVER touch: .credentials.json, settings.json,
projects/, todos/, memory/. Those are per-profile private data. Only the
four shared resource dirs (skills, agents, commands, rules) are managed.

Pure stdlib, Python 3.10+, no dependencies. Set CCM_HOME to override the
home directory (useful for testing).

Example session:

    # Move your existing skills/agents/commands/rules into ~/.claude-shared
    # and leave links behind in ~/.claude:
    $ python ccm.py init-shared --from-profile default

    # Create a second account dir, pre-linked to the shared resources:
    $ python ccm.py create work

    # Paste the printed alias into your shell rc, then:
    $ claude-work        # runs claude with CLAUDE_CONFIG_DIR=~/.claude-work

    # See everything at a glance / print aliases for every shell / audit:
    $ python ccm.py list
    $ python ccm.py aliases --shell all
    $ python ccm.py doctor

Commands: list, create <name>, init-shared [--from-profile <name>],
link [<name> | --all], aliases [--shell zsh|bash|powershell|all], doctor.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

SHARED_DIRS = ["skills", "agents", "commands", "rules"]
SHARED_NAME = ".claude-shared"

# Windows FILE_ATTRIBUTE_REPARSE_POINT - set for both symlinks and junctions.
_REPARSE = 0x400


def home_dir() -> Path:
    override = os.environ.get("CCM_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home()


def shared_home() -> Path:
    return home_dir() / SHARED_NAME


# ---------------------------------------------------------------- link utils

def is_link(path: Path) -> bool:
    """True for symlinks everywhere, plus junctions on Windows."""
    if os.path.islink(path):
        return True
    if os.name == "nt":
        try:
            st = os.lstat(path)
        except OSError:
            return False
        return bool(getattr(st, "st_file_attributes", 0) & _REPARSE)
    return False


def link_target(path: Path) -> str | None:
    """Raw target of a link/junction, for display. None if unreadable."""
    try:
        t = os.readlink(path)
    except OSError:
        return None
    if t.startswith("\\\\?\\"):
        t = t[4:]
    return t


def link_points_to(link: Path, target: Path) -> bool:
    """True if `link` resolves to the same directory as `target`."""
    if not os.path.exists(link):  # follows links -> False when dangling
        return False
    try:
        return os.path.samefile(link, target)
    except OSError:
        return False


def make_link(target: Path, link: Path) -> str:
    """Create a directory link. Returns 'symlink' or 'junction'.

    Tries a real symlink first; on Windows without Developer Mode /
    privileges that raises OSError, so fall back to an NTFS junction,
    which needs no special rights.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except OSError:
        if os.name != "nt":
            raise
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise OSError(
            f"mklink /J failed for {link}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return "junction"


def remove_link(link: Path) -> None:
    """Remove a symlink or junction without touching what it points at."""
    try:
        os.unlink(link)
    except OSError:
        os.rmdir(link)


def dir_has_content(path: Path) -> bool:
    try:
        return any(path.iterdir())
    except OSError:
        return False


# ------------------------------------------------------------------ profiles

def profile_path(name: str) -> Path:
    if name == "default":
        return home_dir() / ".claude"
    return home_dir() / f".claude-{name}"


def discover_profiles() -> list[tuple[str, Path]]:
    """[(name, path)] - ~/.claude as 'default', plus every ~/.claude-<name>."""
    home = home_dir()
    profiles: list[tuple[str, Path]] = []
    default = home / ".claude"
    if default.is_dir():
        profiles.append(("default", default))
    for p in sorted(home.glob(".claude-*")):
        if p.name == SHARED_NAME or not p.is_dir():
            continue
        profiles.append((p.name[len(".claude-"):], p))
    return profiles


def session_count(profile: Path) -> int:
    projects = profile / "projects"
    if not projects.is_dir():
        return 0
    return sum(1 for p in projects.iterdir() if p.is_dir())


def dir_status(profile: Path, name: str) -> str:
    """Status of one shared dir inside a profile.

    One of: 'linked', 'dangling', 'wrong-target', 'real-dir', 'empty-dir',
    'file', 'missing'.
    """
    path = profile / name
    target = shared_home() / name
    if is_link(path):
        if link_points_to(path, target):
            return "linked"
        return "dangling" if not os.path.exists(path) else "wrong-target"
    if path.is_dir():
        return "real-dir" if dir_has_content(path) else "empty-dir"
    if path.exists():
        return "file"
    return "missing"


def alias_lines(name: str, shell: str) -> str:
    suffix = ".claude" if name == "default" else f".claude-{name}"
    if shell == "powershell":
        return (f"function claude-{name} {{ "
                f"$env:CLAUDE_CONFIG_DIR=\"$HOME\\{suffix}\"; claude @args }}")
    return f"alias claude-{name}='CLAUDE_CONFIG_DIR=\"$HOME/{suffix}\" claude'"


# ------------------------------------------------------------------ commands

def cmd_list(_args: argparse.Namespace) -> int:
    profiles = discover_profiles()
    if not profiles:
        print(f"No profiles found under {home_dir()} (looked for .claude and .claude-*).")
        print("Create one with: python ccm.py create <name>")
        return 0
    shared = shared_home()
    print(f"Shared resources: {shared} ({'present' if shared.is_dir() else 'not created - run init-shared'})")
    print()
    for name, path in profiles:
        creds = "yes" if (path / ".credentials.json").is_file() else "no"
        sessions = session_count(path)
        statuses = {d: dir_status(path, d) for d in SHARED_DIRS}
        linked = sum(1 for s in statuses.values() if s == "linked")
        extras = [f"{d}:{s}" for d, s in statuses.items() if s not in ("linked", "missing")]
        detail = f" ({', '.join(extras)})" if extras else ""
        print(f"  {name:<12} {path}")
        print(f"  {'':<12} credentials: {creds} | sessions: {sessions} | shared linked: {linked}/{len(SHARED_DIRS)}{detail}")
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    name = args.name
    if name == "default":
        print("The default profile is just ~/.claude - nothing to create.")
        return 1
    if not all(c.isalnum() or c in "-_" for c in name) or not name:
        print(f"Invalid profile name '{name}': use letters, digits, - or _.")
        return 1
    path = profile_path(name)
    if path.exists():
        print(f"Profile '{name}' already exists at {path} - linking shared dirs only.")
    else:
        path.mkdir(parents=True)
        print(f"Created profile '{name}' at {path}")
    rc = 0
    if shared_home().is_dir():
        rc = _link_profile(name, path)
    else:
        print(f"Note: {shared_home()} does not exist yet; run init-shared first, then 'link {name}'.")
    shell = "powershell" if os.name == "nt" else "zsh"
    print()
    print(f"Add this to your shell profile ({shell}; see 'aliases --shell all' for others):")
    print(f"  {alias_lines(name, shell)}")
    print()
    print(f"Then run 'claude-{name}' and log in - credentials stay private to this profile.")
    return rc


def cmd_init_shared(args: argparse.Namespace) -> int:
    shared = shared_home()
    shared.mkdir(parents=True, exist_ok=True)
    rc = 0
    src_profile = None
    if args.from_profile:
        src_profile = profile_path(args.from_profile)
        if not src_profile.is_dir():
            print(f"Profile '{args.from_profile}' not found at {src_profile}.")
            return 1
    for name in SHARED_DIRS:
        dest = shared / name
        if src_profile is not None:
            src = src_profile / name
            if is_link(src):
                print(f"  {name}: already a link in profile '{args.from_profile}', skipped.")
                dest.mkdir(exist_ok=True)
                continue
            if src.is_dir():
                if dest.is_dir() and dir_has_content(dest):
                    print(f"  {name}: REFUSED - {dest} already has content; "
                          f"merge or remove it manually, then re-run.")
                    rc = 1
                    continue
                if dest.is_dir():
                    dest.rmdir()  # empty placeholder, safe to replace
                shutil.move(str(src), str(dest))
                kind = make_link(dest, src)
                print(f"  {name}: moved {src} -> {dest}, left a {kind} behind.")
                continue
            # fall through: nothing to move, just make sure dest exists
        if dest.exists():
            print(f"  {name}: {dest} already exists, kept.")
        else:
            dest.mkdir()
            print(f"  {name}: created {dest}")
    print(f"Shared home ready: {shared}")
    return rc


def _link_profile(name: str, path: Path) -> int:
    rc = 0
    for d in SHARED_DIRS:
        target = shared_home() / d
        status = dir_status(path, d)
        link = path / d
        if status == "linked":
            print(f"  {name}/{d}: ok")
        elif status in ("dangling", "wrong-target"):
            remove_link(link)
            kind = make_link(target, link)
            print(f"  {name}/{d}: fixed ({status} -> {kind} to {target})")
        elif status == "empty-dir":
            link.rmdir()
            kind = make_link(target, link)
            print(f"  {name}/{d}: replaced empty dir with {kind} to {target}")
        elif status == "missing":
            kind = make_link(target, link)
            print(f"  {name}/{d}: {kind} -> {target}")
        elif status == "real-dir":
            print(f"  {name}/{d}: CONFLICT - real directory with content, not touched. "
                  f"Run 'init-shared --from-profile {name}' to move it into the shared home.")
            rc = 1
        else:  # file
            print(f"  {name}/{d}: CONFLICT - a file is in the way at {link}, not touched.")
            rc = 1
    return rc


def cmd_link(args: argparse.Namespace) -> int:
    if not shared_home().is_dir():
        print(f"{shared_home()} does not exist - run 'init-shared' first.")
        return 1
    for d in SHARED_DIRS:
        (shared_home() / d).mkdir(exist_ok=True)
    if args.all:
        targets = discover_profiles()
    elif args.name:
        path = profile_path(args.name)
        if not path.is_dir():
            print(f"Profile '{args.name}' not found at {path}.")
            return 1
        targets = [(args.name, path)]
    else:
        print("Specify a profile name or --all.")
        return 1
    rc = 0
    for name, path in targets:
        rc |= _link_profile(name, path)
    return rc


def cmd_aliases(args: argparse.Namespace) -> int:
    shell = args.shell or ("powershell" if os.name == "nt" else "zsh")
    profiles = discover_profiles() or [("default", profile_path("default"))]
    shells = ["zsh", "bash", "powershell"] if shell == "all" else [shell]
    for sh in shells:
        if len(shells) > 1:
            rc_file = {"zsh": "~/.zshrc", "bash": "~/.bashrc",
                       "powershell": "$PROFILE"}[sh]
            print(f"# {sh} ({rc_file})")
        for name, _path in profiles:
            print(alias_lines(name, sh))
        if len(shells) > 1:
            print()
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    problems = 0
    shared = shared_home()
    if shared.is_dir():
        print(f"[ok]   shared home exists: {shared}")
        for d in SHARED_DIRS:
            if not (shared / d).is_dir():
                print(f"[warn] shared dir missing: {shared / d} (run init-shared)")
                problems += 1
    else:
        print(f"[warn] shared home missing: {shared} (run init-shared)")
        problems += 1
    profiles = discover_profiles()
    if not profiles:
        print(f"[warn] no profiles found under {home_dir()}")
        return 1
    for name, path in profiles:
        if not (path / ".credentials.json").is_file():
            print(f"[warn] {name}: no .credentials.json - run 'claude-{name}' and log in.")
            problems += 1
        for d in SHARED_DIRS:
            status = dir_status(path, d)
            loc = path / d
            if status == "linked":
                print(f"[ok]   {name}/{d} -> {shared / d}")
            elif status == "dangling":
                print(f"[warn] {name}/{d}: dangling link (-> {link_target(loc)}). Fix: python ccm.py link {name}")
                problems += 1
            elif status == "wrong-target":
                print(f"[warn] {name}/{d}: links to {link_target(loc)}, expected {shared / d}. Fix: python ccm.py link {name}")
                problems += 1
            elif status == "real-dir":
                print(f"[warn] {name}/{d}: real directory with content (not shared). "
                      f"Fix: python ccm.py init-shared --from-profile {name}")
                problems += 1
            elif status in ("empty-dir", "missing"):
                print(f"[warn] {name}/{d}: not linked. Fix: python ccm.py link {name}")
                problems += 1
            else:
                print(f"[warn] {name}/{d}: unexpected file in the way at {loc}.")
                problems += 1
    print()
    if problems:
        print(f"doctor: {problems} issue(s) found.")
        return 1
    print("doctor: everything looks good.")
    return 0


# ---------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccm",
        description="Claude Config Manager - multiple Claude Code accounts with shared skills/agents/commands/rules.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show all profiles and their status").set_defaults(func=cmd_list)

    p = sub.add_parser("create", help="create a new profile dir and link shared resources")
    p.add_argument("name", help="profile name (creates ~/.claude-<name>)")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("init-shared", help="create ~/.claude-shared (optionally seeding it from a profile)")
    p.add_argument("--from-profile", metavar="NAME",
                   help="MOVE skills/agents/commands/rules out of this profile and link them back")
    p.set_defaults(func=cmd_init_shared)

    p = sub.add_parser("link", help="ensure a profile's shared dirs are links into ~/.claude-shared")
    p.add_argument("name", nargs="?", help="profile name")
    p.add_argument("--all", action="store_true", help="link every discovered profile")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("aliases", help="print shell aliases for every profile")
    p.add_argument("--shell", choices=["zsh", "bash", "powershell", "all"],
                   help="default: powershell on Windows, zsh elsewhere")
    p.set_defaults(func=cmd_aliases)

    sub.add_parser("doctor", help="audit shared home, links, and credentials").set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except OSError as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
