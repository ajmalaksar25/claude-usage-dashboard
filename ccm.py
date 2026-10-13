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
link [<name> | --all], aliases [--shell zsh|bash|fish|powershell|all], doctor.
"""

from __future__ import annotations

import argparse
import os
import re
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


# ------------------------------------------------------------ sharing report

def _raw_state(path: Path) -> str:
    """'missing' | 'broken' | 'link' | 'dir' | 'file' for a path (no resolving)."""
    if is_link(path):
        return "link" if os.path.exists(path) else "broken"
    if path.is_dir():
        return "dir"
    if path.exists():
        return "file"
    return "missing"


def _real(path: Path) -> str:
    return os.path.realpath(path)


def sharing_report(profiles: list[tuple[str, Path]] | None = None) -> dict[str, dict]:
    """Per shared dir: {"shared_source": real path | None, "statuses": {profile: status}}.

    Statuses: source | linked | separate | missing | broken. Works for both
    layouts: a dedicated ~/.claude-shared, or one profile (usually the
    default ~/.claude) holding the real dirs that other profiles link to.
    """
    if profiles is None:
        profiles = discover_profiles()
    report: dict[str, dict] = {}
    for d in SHARED_DIRS:
        shared = shared_home() / d
        shared_real = _real(shared) if shared.is_dir() else None
        default_real = _real(profile_path("default") / d)
        states = {n: _raw_state(p / d) for n, p in profiles}
        counts: dict[str, int] = {}
        for n, p in profiles:
            if states[n] == "link" and (p / d).is_dir():
                r = _real(p / d)
                counts[r] = counts.get(r, 0) + 1
        source: str | None = None
        if counts:
            best = max(counts.values())
            tied = [r for r, c in counts.items() if c == best]
            if len(tied) == 1:
                pick = tied[0]
            elif shared_real in tied:
                pick = shared_real
            elif default_real in tied:
                pick = default_real
            else:
                pick = sorted(tied)[0]
            if counts[pick] >= 2 or pick in (shared_real, default_real):
                source = pick
        if source is None and shared_real is not None:
            source = shared_real
        statuses: dict[str, str] = {}
        for n, p in profiles:
            st = states[n]
            if st in ("missing", "broken"):
                statuses[n] = st
            elif source is not None and _real(p / d) == source:
                statuses[n] = "linked" if st == "link" else "source"
            else:
                statuses[n] = "separate"
        report[d] = {"shared_source": source, "statuses": statuses}
    return report


# ------------------------------------------------------- shell / alias utils

_KNOWN_SHELLS = ["zsh", "bash", "fish", "powershell"]


def detect_shell(env: dict | None = None, os_name: str | None = None,
                 sys_platform: str | None = None) -> dict:
    """{"platform", "shell", "rc_file"} for the current (or injected) environment."""
    env = os.environ if env is None else env
    os_name = os.name if os_name is None else os_name
    sys_platform = sys.platform if sys_platform is None else sys_platform
    if os_name == "nt" or sys_platform.startswith(("win", "cygwin", "msys")):
        platform = "windows"
    elif sys_platform == "darwin":
        platform = "darwin"
    else:
        platform = "linux"
    if platform == "windows":
        shell = "powershell"
    else:
        shell = os.path.basename((env.get("SHELL") or "").rstrip("/\\")) or \
            ("zsh" if platform == "darwin" else "bash")
    return {"platform": platform, "shell": shell,
            "rc_file": _rc_file(shell, platform, env)}


def _rc_file(shell: str, platform: str, env: dict) -> str:
    home = home_dir()
    if shell == "zsh":
        zd = env.get("ZDOTDIR")
        base = Path(zd).expanduser() if zd else home
        return str(base / ".zshrc")
    if shell == "bash":
        rc = home / ".bashrc"
        bp = home / ".bash_profile"
        if platform == "darwin" and not rc.exists() and bp.exists():
            return str(bp)
        return str(rc)
    if shell == "fish":
        return str(home / ".config" / "fish" / "config.fish")
    if shell == "powershell":
        prof = env.get("PROFILE")
        if prof:
            return str(Path(prof).expanduser())
        return str(home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1")
    return str(home / ".profile")


def alias_lines(name: str, shell: str) -> str:
    suffix = ".claude" if name == "default" else f".claude-{name}"
    if shell == "powershell":
        return (f"function claude-{name} {{ "
                f"$env:CLAUDE_CONFIG_DIR=\"$HOME\\{suffix}\"; claude @args }}")
    if shell == "fish":
        return f"alias claude-{name} 'CLAUDE_CONFIG_DIR=\"$HOME/{suffix}\" claude'"
    return f"alias claude-{name}='CLAUDE_CONFIG_DIR=\"$HOME/{suffix}\" claude'"


def installed_alias(name: str, rc_file: str) -> str | None:
    """Name of the alias/function in rc_file that launches this profile, or None.

    Matches `claude-<name>` by name, or any alias/function whose line sets CLAUDE_CONFIG_DIR to the
    profile's dir (e.g. `alias claudex='CLAUDE_CONFIG_DIR=$HOME/.claude-codex claude'`)."""
    try:
        text = Path(rc_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    n = re.escape(f"claude-{name}")
    pat = re.compile(rf"^\s*(?:alias\s+{n}(?:[=\s]|$)|function\s+{n}(?:[\s({{]|$))", re.MULTILINE)
    if pat.search(text):
        return f"claude-{name}"
    suffix = re.escape(".claude" if name == "default" else f".claude-{name}")
    by_dir = re.compile(r"^\s*(?:alias\s+([\w.-]+)|function\s+([\w.-]+))[^\n]*CLAUDE_CONFIG_DIR[^\n]*?"
                        rf"[/\\]{suffix}(?![\w-])", re.MULTILINE)
    m = by_dir.search(text)
    if m:
        return m.group(1) or m.group(2)
    return "claude" if name == "default" else None  # plain `claude` already uses ~/.claude


def alias_installed(name: str, rc_file: str) -> bool:
    """True if rc_file defines an alias/function that launches this profile (see installed_alias)."""
    return installed_alias(name, rc_file) is not None


def alias_block(name: str, shell: str | None = None) -> dict:
    det = detect_shell()
    shell = shell or det["shell"]
    rc = det["rc_file"] if shell == det["shell"] else _rc_file(shell, det["platform"], os.environ)
    found = installed_alias(name, rc)
    return {
        "primary": {"shell": shell, "rc_file": rc, "line": alias_lines(name, shell)},
        "other": {sh: alias_lines(name, sh) for sh in _KNOWN_SHELLS if sh != shell},
        "installed": found is not None,
        "installed_name": found,
    }


def profile_report(profiles: list[tuple[str, Path]] | None = None) -> list[dict]:
    if profiles is None:
        profiles = discover_profiles()
    sharing = sharing_report(profiles)
    out = []
    for name, path in profiles:
        statuses = {d: sharing[d]["statuses"][name] for d in SHARED_DIRS}
        out.append({
            "name": name,
            "path": str(path),
            "credentials": (path / ".credentials.json").is_file(),
            "sessions": session_count(path),
            "shared": statuses,
            "shared_source": {d: sharing[d]["shared_source"] for d in SHARED_DIRS},
            "linked": sum(1 for s in statuses.values() if s in ("linked", "source")),
            "alias": alias_block(name),
        })
    return out


def layout_report() -> dict:
    profiles = discover_profiles()
    sharing = sharing_report(profiles)
    det = detect_shell()
    return {
        "profiles": profile_report(profiles),
        "shared_sources": {d: sharing[d]["shared_source"] for d in SHARED_DIRS},
        "shared_root": str(shared_home()),
        "shared_exists": shared_home().is_dir(),
        "shared_dirs": SHARED_DIRS,
        "platform": det["platform"],
        "shell": det["shell"],
        "rc_file": det["rc_file"],
    }


# ------------------------------------------------------------------ commands

def cmd_list(_args: argparse.Namespace) -> int:
    profiles = discover_profiles()
    if not profiles:
        print(f"No profiles found under {home_dir()} (looked for .claude and .claude-*).")
        print("Create one with: python ccm.py create <name>")
        return 0
    shared = shared_home()
    print(f"Shared home: {shared} ({'present' if shared.is_dir() else 'not created'})")
    sharing = sharing_report(profiles)
    for d in SHARED_DIRS:
        src = sharing[d]["shared_source"]
        print(f"  {d:<9} source: {src or '(none)'}")
    print()
    for name, path in profiles:
        creds = "yes" if (path / ".credentials.json").is_file() else "no"
        statuses = {d: sharing[d]["statuses"][name] for d in SHARED_DIRS}
        linked = sum(1 for s_ in statuses.values() if s_ in ("linked", "source"))
        extras = [f"{d}:{s_}" for d, s_ in statuses.items() if s_ not in ("linked", "source")]
        detail = f" ({', '.join(extras)})" if extras else ""
        print(f"  {name:<12} {path}")
        print(f"  {'':<12} credentials: {creds} | sessions: {session_count(path)} | shared: {linked}/{len(SHARED_DIRS)}{detail}")
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
    if shared_home().is_dir() or any(
            v["shared_source"] for v in sharing_report().values()):
        rc = _link_profile(name, path)
    else:
        print(f"Note: no shared source found ({shared_home()} missing); run init-shared first, then 'link {name}'.")
    shell = detect_shell()["shell"]
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
    profiles = discover_profiles()
    if not any(n == name for n, _ in profiles):
        profiles.append((name, path))
    report = sharing_report(profiles)
    rc = 0
    for d in SHARED_DIRS:
        link = path / d
        status = report[d]["statuses"][name]
        source = report[d]["shared_source"]
        target = shared_home() / d if shared_home().is_dir() else (Path(source) if source else None)
        if target is None:
            print(f"  {name}/{d}: no shared source found, skipped.")
            continue
        if status in ("source", "linked"):
            print(f"  {name}/{d}: ok ({status})")
            continue
        if _real(link) == _real(target) and os.path.exists(link):
            print(f"  {name}/{d}: ok")
            continue
        raw = _raw_state(link)
        if raw == "missing":
            kind = make_link(target, link)
            print(f"  {name}/{d}: {kind} -> {target}")
        elif raw in ("broken", "link"):
            remove_link(link)
            kind = make_link(target, link)
            print(f"  {name}/{d}: fixed ({status} -> {kind} to {target})")
        elif raw == "dir" and not dir_has_content(link):
            link.rmdir()
            kind = make_link(target, link)
            print(f"  {name}/{d}: replaced empty dir with {kind} to {target}")
        elif raw == "dir":
            print(f"  {name}/{d}: CONFLICT - real directory with content, not touched. "
                  f"Run 'init-shared --from-profile {name}' to move it into the shared home.")
            rc = 1
        else:
            print(f"  {name}/{d}: CONFLICT - a file is in the way at {link}, not touched.")
            rc = 1
    return rc


def cmd_link(args: argparse.Namespace) -> int:
    if shared_home().is_dir():
        for d in SHARED_DIRS:
            (shared_home() / d).mkdir(exist_ok=True)
    elif not any(v["shared_source"] for v in sharing_report().values()):
        print(f"{shared_home()} does not exist and no shared source was detected - run 'init-shared' first.")
        return 1
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
    det = detect_shell()
    shell = args.shell or (det["shell"] if det["shell"] in _KNOWN_SHELLS else "zsh")
    profiles = discover_profiles() or [("default", profile_path("default"))]
    shells = list(_KNOWN_SHELLS) if shell == "all" else [shell]
    for sh in shells:
        if len(shells) > 1:
            rc_file = {"zsh": "~/.zshrc", "bash": "~/.bashrc",
                       "fish": "~/.config/fish/config.fish",
                       "powershell": "$PROFILE"}[sh]
            print(f"# {sh} ({rc_file})")
        for name, _path in profiles:
            print(alias_lines(name, sh))
        if len(shells) > 1:
            print()
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    problems = 0
    profiles = discover_profiles()
    if not profiles:
        print(f"[warn] no profiles found under {home_dir()}")
        return 1
    sharing = sharing_report(profiles)
    shared = shared_home()
    if shared.is_dir():
        print(f"[ok]   shared home exists: {shared}")
    else:
        print(f"[info] no {shared}; using detected sources where present")
    for d in SHARED_DIRS:
        src = sharing[d]["shared_source"]
        if src:
            print(f"[ok]   {d} shared source: {src}")
        else:
            print(f"[warn] {d}: no shared source (no links, no {shared / d}). Fix: python ccm.py init-shared --from-profile default")
            problems += 1
    for name, path in profiles:
        if not (path / ".credentials.json").is_file():
            print(f"[warn] {name}: no .credentials.json - run 'claude-{name}' and log in.")
            problems += 1
        for d in SHARED_DIRS:
            status = sharing[d]["statuses"][name]
            loc = path / d
            if status in ("source", "linked"):
                print(f"[ok]   {name}/{d}: {status}")
            elif status == "broken":
                print(f"[warn] {name}/{d}: dangling link (-> {link_target(loc)}). Fix: python ccm.py link {name}")
                problems += 1
            elif status == "separate":
                print(f"[warn] {name}/{d}: separate, not shared. Fix: python ccm.py link {name}")
                problems += 1
            else:
                print(f"[warn] {name}/{d}: missing. Fix: python ccm.py link {name}")
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
    p.add_argument("--shell", choices=["zsh", "bash", "fish", "powershell", "all"],
                   help="default: the detected shell")
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
