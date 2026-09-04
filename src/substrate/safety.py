"""
Deterministic, LLM-independent safety gate for shell commands.

Classification levels
---------------------
SAFE               – read-only / informational; no state is changed
NEEDS_CONFIRMATION – modifies state, but the change is recoverable
HARD_BLOCK         – potentially irreversible; must never auto-execute

Design principles
-----------------
* No LLM dependency — deterministic, always-on, unit-testable.
* Chained commands (&&, ;, ||, |, newlines) are split and each sub-command is
  classified; the worst result wins.
* Command substitution ($(...), backticks) and ``-c`` interpreter payloads are
  recursively extracted and classified — the embedded payload cannot hide.
* The gate is conservative: unknown commands default to NEEDS_CONFIRMATION.
* Danger = operation x target x reversibility — not just keyword matching.

This module is the hardened successor to the original ``safety_classification.py``
prototype. See docs/CODE_REVIEW.md for the bypasses that motivated each fix.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import IntEnum

__all__ = ["Level", "Classification", "safety_level"]


class Level(IntEnum):
    SAFE = 0
    NEEDS_CONFIRMATION = 1
    HARD_BLOCK = 2

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name


@dataclass
class Classification:
    level: Level
    reason: str

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.level}] {self.reason}"


# ---------------------------------------------------------------------------
# Tokenisation helpers
# ---------------------------------------------------------------------------

def _tokenize(command: str) -> list[str]:
    """Tokenise a shell command, falling back to whitespace split on error."""
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


# Separators: newlines, ; & && || |  (but not && / || collapsed wrongly).
_SEPARATOR_RE = re.compile(r'(?<![|&])[|;\n](?![|&])|&&|\|\|')


def _split_subcommands(command: str) -> list[str]:
    """Split on shell separators (newline ; && || |) outside of quotes."""
    parts = _SEPARATOR_RE.split(command)
    return [p.strip() for p in parts if p.strip()]


# Command substitution: $(...) and `...`
_SUBST_RE = re.compile(r'\$\(([^()]*)\)|`([^`]*)`')


def _extract_substitutions(command: str) -> list[str]:
    """Return the inner commands of any $(...) or `...` substitutions."""
    found: list[str] = []
    for m in _SUBST_RE.finditer(command):
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        if inner and inner.strip():
            found.append(inner.strip())
    return found


def _base_command(tokens: list[str]) -> str:
    """Return the effective base command, skipping privilege-escalation prefixes."""
    skip = {'sudo', 'env', 'nice', 'nohup', 'time', 'doas', 'runas', 'su'}
    for tok in tokens:
        if tok not in skip:
            return tok.lower().replace('\\', '/').rsplit('/', 1)[-1]
    return ''


def _flags(tokens: list[str]) -> set[str]:
    """Collect all flag tokens (start with - or Windows /flag)."""
    result: set[str] = set()
    for tok in tokens[1:]:
        if tok.startswith('-'):
            result.add(tok.lower())
        elif re.match(r'^/[a-z]', tok, re.IGNORECASE):
            result.add(tok.lower())
    return result


def _positional_args(tokens: list[str]) -> list[str]:
    """Return non-flag arguments (the actual targets)."""
    return [
        t for t in tokens[1:]
        if not t.startswith('-') and not re.match(r'^/[a-z]', t, re.IGNORECASE)
    ]


# ---------------------------------------------------------------------------
# Threat knowledge bases
# ---------------------------------------------------------------------------

_SAFE_COMMANDS = {
    'ls', 'dir', 'pwd', 'echo', 'cat', 'type', 'head', 'tail', 'more',
    'less', 'find', 'grep', 'rg', 'which', 'where', 'whereis',
    'whoami', 'hostname', 'id', 'uname', 'uptime', 'date',
    'ps', 'top', 'htop', 'tasklist',
    'netstat', 'ss', 'ipconfig', 'ifconfig', 'ip',
    'df', 'du', 'free', 'vmstat', 'lscpu', 'lsblk',
    'printenv', 'env', 'set',
    'man', 'help',
    'get-childitem', 'get-content', 'get-item', 'get-location',
    'get-command', 'get-help', 'get-module', 'get-process',
    'get-service', 'get-variable',
}

_GIT_SAFE_SUBS = {
    'status', 'log', 'diff', 'show', 'branch', 'tag', 'fetch',
    'remote', 'describe', 'rev-parse', 'ls-files', 'ls-tree',
    'shortlog', 'blame', 'grep',
}

# git config / bisect mutate state or can plant command execution -> confirm.
_GIT_CONFIRM_SUBS = {
    'add', 'commit', 'push', 'pull', 'merge', 'rebase', 'cherry-pick',
    'checkout', 'switch', 'restore', 'stash', 'reset', 'clean',
    'apply', 'am', 'format-patch', 'config', 'bisect',
}

_PKG_MANAGERS = {
    'pip', 'pip3', 'pipx',
    'npm', 'npx', 'yarn', 'pnpm',
    'apt', 'apt-get', 'aptitude',
    'yum', 'dnf', 'zypper', 'pacman',
    'brew', 'choco', 'winget', 'scoop',
    'snap', 'flatpak',
    'cargo', 'gem', 'go',
}

_PKG_READ_SUBS = {
    '--version', '-v', 'list', 'show', 'info', 'search',
    'outdated', 'audit', 'check', 'query',
}

_ALWAYS_HARD_BLOCK = {
    'mkfs', 'fdisk', 'parted', 'gdisk', 'diskpart', 'format',
    'shred', 'wipe', 'scrub', 'bcdedit', 'cipher',
}

_CRITICAL_PATHS = {
    '/', '~', '$home',
    '/etc', '/usr', '/bin', '/sbin', '/boot',
    '/lib', '/lib64', '/var', '/proc', '/sys', '/dev',
    '/etc/passwd', '/etc/shadow', '/etc/sudoers',
    '/etc/hosts', '/etc/fstab', '/boot/grub',
    'c:\\', 'c:/', 'c:',
    'c:\\windows', 'c:/windows',
    'c:\\windows\\system32', 'c:/windows/system32',
    'c:\\program files', 'c:/program files',
    '$homepath', '%userprofile%', '%windir%', '%systemroot%',
}

_INTERPRETERS = {
    'python', 'python3', 'python2', 'node', 'ruby',
    'perl', 'php', 'bash', 'sh', 'zsh', 'fish',
    'pwsh', 'powershell', 'cmd',
}


def _is_critical_path(target: str) -> bool:
    norm = target.strip().lower().rstrip('/\\')
    if not norm or norm in ('/', '~', 'c:', 'c:\\', 'c:/'):
        return True
    if set(norm).issubset({'*', '.', '?', ' '}):
        return True
    for cp in _CRITICAL_PATHS:
        if norm == cp or norm.startswith(cp + '/') or norm.startswith(cp + '\\'):
            return True
    return False


def _redirect_hits_critical_file(command: str) -> bool:
    targets = re.findall(r'>{1,2}\s*(\S+)', command)
    return any(_is_critical_path(t) for t in targets)


_PIPE_TO_SHELL_RE = re.compile(
    r'\|\s*(bash|sh|zsh|fish|cmd\.exe|powershell|pwsh|python\d*|perl|ruby)\b',
    re.IGNORECASE,
)

# `-c "payload"` for interpreters — the payload must be classified, not the wrapper.
_DASH_C_RE = re.compile(r'-c\b')


def _classify_single(command: str) -> Classification:
    cmd = command.strip()
    if not cmd or cmd.startswith('#'):
        return Classification(Level.SAFE, "Empty or comment line")

    tokens = _tokenize(cmd)
    base = _base_command(tokens)
    flags = _flags(tokens)
    args = _positional_args(tokens)

    # ── 1. Fork bomb ──────────────────────────────────────────────────────
    if ':|:' in cmd or re.search(r':\s*\(\s*\)\s*\{', cmd):
        return Classification(Level.HARD_BLOCK,
            "Fork bomb pattern — would exhaust system resources")

    # ── 2. Pipe-to-shell (remote code execution) ──────────────────────────
    if _PIPE_TO_SHELL_RE.search(cmd):
        return Classification(Level.HARD_BLOCK,
            "Pipes output into a shell interpreter — arbitrary remote code execution")

    # ── 3. Output redirect to critical file ───────────────────────────────
    if _redirect_hits_critical_file(cmd):
        return Classification(Level.HARD_BLOCK,
            "Redirects output into a critical system file — would overwrite it")

    # ── 4. Always-dangerous base commands ─────────────────────────────────
    # mkfs has filesystem-specific variants (mkfs.ext4, mkfs.xfs, ...).
    if base.startswith("mkfs"):
        return Classification(Level.HARD_BLOCK,
            f"'{base}' creates a filesystem — destroys existing data")
    if base in _ALWAYS_HARD_BLOCK:
        if base == 'cipher':
            if '/w' in flags:
                return Classification(Level.HARD_BLOCK,
                    "cipher /w performs a secure wipe of free space — irreversible")
            return Classification(Level.NEEDS_CONFIRMATION,
                "cipher modifies file encryption — review flags and target")
        return Classification(Level.HARD_BLOCK,
            f"'{base}' performs low-level irreversible disk or boot operations")

    # ── 5. dd — only dangerous when writing to a raw device ───────────────
    if base == 'dd':
        device_write = any(re.match(r'of=/dev/', t, re.IGNORECASE) for t in tokens)
        if device_write:
            return Classification(Level.HARD_BLOCK,
                "dd writing to a raw block device — will permanently destroy data")
        return Classification(Level.NEEDS_CONFIRMATION,
            "dd can overwrite files — verify 'of=' target carefully")

    # ── 6. rm / del / Remove-Item ─────────────────────────────────────────
    if base in ('rm', 'del', 'remove-item', 'ri', 'erase'):
        recursive = bool({'--recursive', '-r', '-rf', '-fr', '-rrf'} & flags
                         or {'/s'} & flags)
        force = bool({'--force', '-f', '-rf', '-fr'} & flags or {'/f'} & flags)

        if not args and (recursive or force):
            return Classification(Level.HARD_BLOCK,
                "Recursive/forced deletion with no explicit safe target specified")

        for target in args:
            if _is_critical_path(target):
                return Classification(Level.HARD_BLOCK,
                    f"Targeting critical system path '{target}' for deletion")
            if (recursive or force) and ('*' in target or target in ('.', './', '.\\')):
                return Classification(Level.HARD_BLOCK,
                    f"Recursive/forced deletion with wildcard target '{target}'")

        if recursive and force:
            return Classification(Level.NEEDS_CONFIRMATION,
                f"Recursive forced deletion of '{', '.join(args)}' — cannot be undone")

        return Classification(Level.NEEDS_CONFIRMATION,
            f"Deletes file(s): {', '.join(args) or '(no target specified)'}")

    # ── 7. kill / pkill — special case: kill all processes ────────────────
    if base in ('kill', 'pkill', 'killall'):
        sigkill = '-9' in flags or '--sigkill' in flags
        for a in args:
            if sigkill and a in ('-1', '1', 'init', 'systemd'):
                return Classification(Level.HARD_BLOCK,
                    "SIGKILL to PID 1 / all processes — will crash the system")
        return Classification(Level.NEEDS_CONFIRMATION,
            f"Terminates process(es): {', '.join(args) or 'unspecified'}")

    # ── 8. shutdown / reboot / halt ───────────────────────────────────────
    if base in ('shutdown', 'reboot', 'halt', 'poweroff', 'init', 'telinit'):
        return Classification(Level.HARD_BLOCK,
            f"'{base}' terminates or restarts the OS — irreversible remotely")

    # ── 9. Windows registry ───────────────────────────────────────────────
    if base == 'reg':
        sub = tokens[1].lower() if len(tokens) > 1 else ''
        if sub == 'delete':
            key = tokens[2].lower() if len(tokens) > 2 else ''
            system_hives = ('hklm', 'hkey_local_machine', 'hkcr',
                            'hkey_classes_root', 'hkcc', 'hkey_current_config')
            if any(key.startswith(h) for h in system_hives):
                return Classification(Level.HARD_BLOCK,
                    f"Deletes system registry hive key '{key}' — can brick the OS")
            return Classification(Level.NEEDS_CONFIRMATION,
                f"Deletes registry key '{key}'")
        if sub == 'query':
            return Classification(Level.SAFE, "Registry query — read-only")
        return Classification(Level.NEEDS_CONFIRMATION,
            f"Modifies the registry: reg {sub}")

    # ── 10. chmod / icacls on critical paths ──────────────────────────────
    if base in ('chmod', 'chown', 'icacls', 'attrib', 'takeown'):
        for a in args:
            if _is_critical_path(a):
                return Classification(Level.HARD_BLOCK,
                    f"Changing ownership/permissions on critical system path '{a}'")
        return Classification(Level.NEEDS_CONFIRMATION,
            "Changes file permissions or ownership — verify target and mode")

    # ── 11. Package managers ──────────────────────────────────────────────
    if base in _PKG_MANAGERS:
        sub = tokens[1].lower() if len(tokens) > 1 else ''
        if sub in _PKG_READ_SUBS or not sub:
            return Classification(Level.SAFE, f"'{base} {sub}' is a read-only query")
        if sub in ('install', 'add', 'uninstall', 'remove', 'upgrade',
                   'update', 'purge', 'autoremove', 'reinstall'):
            return Classification(Level.NEEDS_CONFIRMATION,
                f"'{base} {sub}' modifies installed packages")
        return Classification(Level.NEEDS_CONFIRMATION,
            f"Package manager command '{base} {sub}' — review before running")

    # ── 12. Git ───────────────────────────────────────────────────────────
    if base == 'git':
        sub = tokens[1].lower() if len(tokens) > 1 else ''
        if not sub or sub in _GIT_SAFE_SUBS:
            return Classification(Level.SAFE, f"'git {sub}' is a read-only operation")
        if sub in _GIT_CONFIRM_SUBS:
            return Classification(Level.NEEDS_CONFIRMATION,
                f"'git {sub}' modifies repository or config state")
        return Classification(Level.NEEDS_CONFIRMATION,
            f"'git {sub}' — unknown git sub-command; require review")

    # ── 13. Interpreter with -c payload — classify the payload ────────────
    if base in _INTERPRETERS and _DASH_C_RE.search(cmd):
        # Extract the argument following -c and classify it recursively.
        payload = _extract_dash_c_payload(tokens)
        if payload:
            inner = safety_level(payload)
            return Classification(inner.level,
                f"Interpreter '-c' payload: {inner.reason}")
        return Classification(Level.NEEDS_CONFIRMATION,
            f"'{base} -c' executes an inline program — inspect the payload")

    # ── 14. --version / --help long flags make a command safe ─────────────
    # Restricted to unambiguous long flags (or the informational forms that
    # are their own only argument) so a lone '-v' on an unknown tool is not a
    # free pass. See CODE_REVIEW.md (the '-v false-safe' finding).
    if ({'--version', '--help', '/help', '/?'} & flags) and not args:
        return Classification(Level.SAFE,
            f"'{base}' called with --version / --help — informational only")

    # ── 15. Output redirection (non-critical target) ──────────────────────
    if re.search(r'>{1,2}\s*\S+', cmd):
        return Classification(Level.NEEDS_CONFIRMATION,
            "Redirects output to a file — will create or overwrite it")

    # ── 16. Service management ────────────────────────────────────────────
    if base in ('systemctl', 'service', 'sc', 'net', 'launchctl'):
        sub = tokens[1].lower() if len(tokens) > 1 else ''
        if sub in ('status', 'list', 'list-units', 'is-active',
                   'is-enabled', 'show', 'cat', 'help'):
            return Classification(Level.SAFE, "Service status / list query — read-only")
        return Classification(Level.NEEDS_CONFIRMATION,
            f"'{base} {sub}' modifies service state")

    # ── 17. Network download (curl / wget etc.) ───────────────────────────
    if base in ('curl', 'wget', 'invoke-webrequest', 'iwr', 'invoke-restmethod'):
        return Classification(Level.NEEDS_CONFIRMATION,
            "Makes an HTTP request — may download files or exfiltrate data")

    # ── 18. Script / interpreter execution (no -c) ────────────────────────
    if base in _INTERPRETERS:
        if ({'--version', '--help'} & flags) and not args:
            return Classification(Level.SAFE,
                f"'{base}' version / help check — informational")
        if args:
            return Classification(Level.NEEDS_CONFIRMATION,
                f"Executes '{args[0]}' — inspect the script before running")
        return Classification(Level.SAFE, f"'{base}' with no script — opens interactive REPL")

    # ── 19. sed with -i (in-place edit) ───────────────────────────────────
    if base == 'sed':
        if '-i' in flags or '--in-place' in flags:
            return Classification(Level.NEEDS_CONFIRMATION,
                "sed -i modifies files in place — backup recommended")
        return Classification(Level.SAFE, "sed without -i — read-only stream editor")

    # ── 20. Known read-only commands ──────────────────────────────────────
    if base in _SAFE_COMMANDS:
        return Classification(Level.SAFE, f"'{base}' is a read-only / informational command")

    # ── 21. File creation or movement (non-dangerous) ─────────────────────
    if base in ('mkdir', 'md', 'touch', 'new-item',
                'cp', 'copy', 'mv', 'move', 'xcopy', 'robocopy',
                'rsync', 'scp', 'copy-item', 'move-item', 'rename-item'):
        return Classification(Level.NEEDS_CONFIRMATION,
            f"'{base}' creates, copies or moves files — may overwrite destinations")

    # ── Default: unknown command — conservatively flag for review ──────────
    return Classification(Level.NEEDS_CONFIRMATION,
        f"Unknown command '{base}' — safety cannot be determined; require human review")


def _extract_dash_c_payload(tokens: list[str]) -> str:
    """Return the argument that follows a ``-c`` flag, if any."""
    for i, tok in enumerate(tokens):
        if tok == '-c' and i + 1 < len(tokens):
            return tokens[i + 1]
    return ''


def safety_level(command: str) -> Classification:
    """
    Classify a shell command string as SAFE, NEEDS_CONFIRMATION, or HARD_BLOCK.

    Handles chained commands (&&, ;, ||, |, newlines), command substitution
    ($(...), backticks), and interpreter ``-c`` payloads. Returns the highest
    (worst-case) classification found anywhere in the command.
    """
    command = command.strip()
    if not command:
        return Classification(Level.SAFE, "Empty input")

    # Pipe-to-shell is checked whole, before splitting, so `curl ... | bash`
    # is caught even though the split would separate the two segments.
    if _PIPE_TO_SHELL_RE.search(command):
        return Classification(Level.HARD_BLOCK,
            "Pipes output into a shell interpreter — arbitrary remote code execution")

    worst = Classification(Level.SAFE, "No sub-commands")

    def consider(result: Classification) -> bool:
        nonlocal worst
        if result.level > worst.level:
            worst = result
        return worst.level == Level.HARD_BLOCK

    for sub in _split_subcommands(command):
        # Classify any command-substitution payloads first — they run.
        for inner in _extract_substitutions(sub):
            if consider(safety_level(inner)):
                return worst
        if consider(_classify_single(sub)):
            return worst

    return worst
