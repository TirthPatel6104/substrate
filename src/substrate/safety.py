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
* Chained commands (&&, ;, ||, |, &, newlines) are split and each sub-command
  is classified; the worst result wins.
* Command substitution ($(...), backticks, <(...)), ``xargs``/``eval``/
  ``find -exec`` wrappers and ``-c`` interpreter payloads are recursively
  extracted and classified — the embedded payload cannot hide.
* Non-shell interpreter payloads (``python -c``) have their string literals
  scanned, so ``os.system("rm -rf /")`` is still caught.
* Variables that cannot be resolved statically are treated as *hostile* when
  they sit in a destructive position (``rm -rf $DIR/`` with an empty ``$DIR``
  is ``rm -rf /``).
* The gate is conservative: unknown commands default to NEEDS_CONFIRMATION.
* Danger = operation x target x reversibility — not just keyword matching.

Every bypass ever found lives in ``tests/corpus/commands.jsonl`` as a
regression case. See docs/SAFETY_GATE.md for the design write-up.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import IntEnum

__all__ = ["Level", "Classification", "safety_level", "tokenize_command", "has_shell_operators"]

_MAX_DEPTH = 8  # recursion guard for nested wrappers / substitutions


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

_WINDOWS_PATH_RE = re.compile(r'[A-Za-z]:\\|\\\\[A-Za-z]|(?<=\w)\\[A-Za-z]')


def _tokenize(command: str) -> list[str]:
    """Tokenise a shell command, falling back to whitespace split on error.

    POSIX mode eats backslashes, which turns ``C:\\Windows`` into ``C:Windows``
    and hides the target from the path checks. When a command carries a
    Windows-style path we tokenise in non-POSIX mode and strip the quotes
    ourselves.
    """
    if _WINDOWS_PATH_RE.search(command):
        try:
            return [t.strip('"\'') for t in shlex.split(command, posix=False)]
        except ValueError:
            return [t.strip('"\'') for t in command.split()]
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


# Separators: newlines ; & && || |  — but never the `>&` / `&>` / `2>&1`
# redirect forms, never the inside of `&&` / `||`, and never an escaped `\;`
# (the terminator of `find -exec ... \;`).
_SEPARATOR_RE = re.compile(
    r'(?<![|&\\])[|;\n](?![|&])'    # single | ; newline
    r'|&&|\|\|'                      # && ||
    r'|(?<![|&>])&(?![&>])'          # single & (background) — not >& &> &&
)


def _naive_split(command: str) -> list[str]:
    parts = _SEPARATOR_RE.split(command)
    return [p.strip() for p in parts if p.strip()]


def _split_subcommands(command: str) -> list[str]:
    """Split on shell separators (newline ; && || | &) *outside* quotes.

    Quote-awareness matters in both directions: ``python -c 'a; b'`` must keep
    its payload intact so the payload can be analysed, and ``grep "a|b" f``
    must not be split into a fake second command. Backtick and ``$( )``
    substitutions are kept atomic so their contents are classified by
    ``_extract_substitutions`` rather than torn apart here.

    If quoting is unbalanced we cannot trust the scan, so we fall back to the
    naive regex split, which over-splits (conservative: more sub-commands
    classified, never fewer).
    """
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(command)

    def flush() -> None:
        seg = ''.join(buf).strip()
        if seg:
            parts.append(seg)
        buf.clear()

    while i < n:
        ch = command[i]
        nxt = command[i + 1] if i + 1 < n else ''
        prev = command[i - 1] if i > 0 else ''
        if quote:
            buf.append(ch)
            if ch == '\\' and quote == '"' and nxt:
                buf.append(nxt)
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == '\\' and nxt:
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == '`':
            j = command.find('`', i + 1)
            if j == -1:
                return _naive_split(command)
            buf.append(command[i:j + 1])
            i = j + 1
            continue
        if ch == '$' and nxt == '(':
            j, depth = i + 2, 1
            while j < n and depth:
                if command[j] == '(':
                    depth += 1
                elif command[j] == ')':
                    depth -= 1
                j += 1
            if depth:
                return _naive_split(command)
            buf.append(command[i:j])
            i = j
            continue
        if ch in ('\n', ';'):
            flush()
            i += 1
            continue
        if ch == '|':
            flush()
            i += 2 if nxt in ('|', '&') else 1
            continue
        if ch == '&':
            if nxt == '&':
                flush()
                i += 2
                continue
            if nxt == '>' or prev in ('>', '<'):
                buf.append(ch)          # &> redirect, 2>&1 / <& dup
                i += 1
                continue
            flush()                     # background `&`
            i += 1
            continue
        buf.append(ch)
        i += 1
    if quote:
        return _naive_split(command)
    flush()
    return parts


# Command / process substitution: $(...)  `...`  <(...)  >(...)
_SUBST_RE = re.compile(r'\$\(([^()]*)\)|`([^`]*)`|[<>]\(([^()]*)\)')


def _extract_substitutions(command: str) -> list[str]:
    """Return the inner commands of any $(...) / `...` / <(...) substitutions."""
    found: list[str] = []
    for m in _SUBST_RE.finditer(command):
        inner = next((g for g in m.groups() if g is not None), None)
        if inner and inner.strip():
            found.append(inner.strip())
    return found


_PREFIX_SKIP = {
    'sudo', 'env', 'nice', 'nohup', 'time', 'doas', 'runas', 'su',
    'command', 'builtin', 'exec', 'stdbuf', 'timeout', 'ionice',
    'strace', 'ltrace', 'watch',
}
# Wrapper flags that take a value (so the value is not mistaken for the command).
_PREFIX_VALUE_FLAGS = {'-u', '-g', '-n', '-c', '-s', '-k', '-o', '-e', '-i', '--user', '--group'}
_ASSIGNMENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


def _strip_prefixes(tokens: list[str]) -> list[str]:
    """Drop privilege / environment wrappers and VAR=value assignments.

    ``sudo -u root env FOO=bar rm -rf /`` -> ``['rm', '-rf', '/']``.
    The flags belonging to the wrapper itself are dropped too, so they are
    never mistaken for flags of the real command.
    """
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower().replace('\\', '/').rsplit('/', 1)[-1]
        if _ASSIGNMENT_RE.match(tok):
            i += 1
            continue
        if low in _PREFIX_SKIP:
            i += 1
            if low == 'timeout' and i < len(tokens) and re.match(r'^\d+[smhd]?$', tokens[i]):
                i += 1
            while i < len(tokens) and tokens[i].startswith('-'):
                if tokens[i] in _PREFIX_VALUE_FLAGS and i + 1 < len(tokens):
                    i += 1
                i += 1
            continue
        break
    return tokens[i:]


def _base_command(tokens: list[str]) -> str:
    """Return the effective base command (basename, lower-case)."""
    if not tokens:
        return ''
    return tokens[0].lower().replace('\\', '/').rsplit('/', 1)[-1]


def _is_win_flag(tok: str) -> bool:
    return bool(re.match(r'^/[a-z?]', tok, re.IGNORECASE)) and len(tok) <= 3


def _flags(tokens: list[str]) -> set[str]:
    """Collect all flag tokens (start with - or Windows /flag)."""
    result: set[str] = set()
    for tok in tokens[1:]:
        if tok.startswith('-') or _is_win_flag(tok):
            result.add(tok.lower())
    return result


def _positional_args(tokens: list[str]) -> list[str]:
    """Return non-flag arguments (the actual targets)."""
    return [t for t in tokens[1:] if not t.startswith('-') and not _is_win_flag(t)]


_UNRESOLVED_VAR_RE = re.compile(
    r'(?<!\\)\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|%[A-Za-z_][A-Za-z0-9_]*%|\$env:[A-Za-z_]+'
)


def _has_unresolved_var(target: str) -> bool:
    return bool(_UNRESOLVED_VAR_RE.search(target))


# ---------------------------------------------------------------------------
# Threat knowledge bases
# ---------------------------------------------------------------------------

_SAFE_COMMANDS = {
    'ls', 'dir', 'pwd', 'echo', 'cat', 'type', 'head', 'tail', 'more',
    'less', 'grep', 'rg', 'which', 'where', 'whereis', 'wc', 'sort',
    'uniq', 'cut', 'tr', 'awk', 'diff', 'cmp', 'file', 'stat', 'tree',
    'whoami', 'hostname', 'id', 'uname', 'uptime', 'date', 'cal',
    'ps', 'top', 'htop', 'tasklist',
    'netstat', 'ss', 'ipconfig', 'ifconfig', 'ip',
    'df', 'du', 'free', 'vmstat', 'lscpu', 'lsblk', 'lsof',
    'printenv', 'set', 'true', 'false', 'test', 'basename', 'dirname',
    'realpath', 'readlink', 'md5sum', 'sha1sum', 'sha256sum',
    'man', 'help', 'jq', 'yq', 'column', 'nl', 'tac', 'rev', 'strings', 'xxd', 'od',
    'get-childitem', 'get-content', 'get-item', 'get-location',
    'get-command', 'get-help', 'get-module', 'get-process',
    'get-service', 'get-variable', 'get-date', 'get-host', 'select-string',
    'gci', 'gc', 'gi', 'gl', 'gcm', 'gps', 'gsv', 'sls', 'write-output', 'write-host',
}

_GIT_SAFE_SUBS = {
    'status', 'log', 'diff', 'show', 'branch', 'tag', 'fetch',
    'remote', 'describe', 'rev-parse', 'ls-files', 'ls-tree',
    'shortlog', 'blame', 'grep', 'reflog', 'cat-file', 'rev-list',
    'name-rev', 'var', 'version', 'help', 'worktree',
}
# Flags/sub-verbs that turn an otherwise read-only git sub-command into a write.
_GIT_SAFE_SUB_WRITE_FLAGS = {
    'branch': {'-d', '-D', '--delete', '-m', '-M', '--move', '-c', '-C', '--copy',
               '-f', '--force', '--set-upstream-to', '-u', '--unset-upstream'},
    'tag': {'-d', '--delete', '-f', '--force', '-a', '-s'},
    'remote': {'add', 'remove', 'rm', 'rename', 'set-url', 'prune', 'set-head', 'set-branches'},
    'worktree': {'add', 'remove', 'prune', 'move', 'lock', 'unlock'},
    'reflog': {'expire', 'delete'},
    'fetch': {'--prune', '-p', '--prune-tags'},
}

# git config / bisect mutate state or can plant command execution -> confirm.
_GIT_CONFIRM_SUBS = {
    'add', 'commit', 'push', 'pull', 'merge', 'rebase', 'cherry-pick',
    'checkout', 'switch', 'restore', 'stash', 'reset', 'clean',
    'apply', 'am', 'format-patch', 'config', 'bisect', 'init', 'clone',
    'mv', 'rm', 'revert', 'gc', 'prune', 'filter-branch', 'submodule',
    'notes', 'update-ref', 'symbolic-ref', 'replace',
}

_PKG_MANAGERS = {
    'pip', 'pip3', 'pipx', 'uv', 'poetry', 'conda', 'mamba',
    'npm', 'npx', 'yarn', 'pnpm', 'bun',
    'apt', 'apt-get', 'aptitude', 'dpkg',
    'yum', 'dnf', 'zypper', 'pacman', 'rpm',
    'brew', 'choco', 'winget', 'scoop',
    'snap', 'flatpak',
    'cargo', 'gem', 'go', 'composer', 'nuget', 'dotnet',
}

_PKG_READ_SUBS = {
    '--version', '-v', 'list', 'show', 'info', 'search', 'view',
    'outdated', 'audit', 'check', 'query', 'ls', 'why', 'tree',
    'freeze', 'env', 'help', 'doctor', 'version', 'inspect',
}
_PKG_MUTATE_SUBS = {
    'install', 'add', 'uninstall', 'remove', 'upgrade', 'i', 'un',
    'update', 'purge', 'autoremove', 'reinstall', 'sync', 'ci', 'link', 'unlink',
}
_PKG_EXEC_SUBS = {'run', 'exec', 'start', 'test', 'build', 'publish', 'x', 'dlx', 'create', 'init'}

_ALWAYS_HARD_BLOCK = {
    'mkfs', 'fdisk', 'parted', 'gdisk', 'sgdisk', 'diskpart', 'format',
    'shred', 'wipe', 'scrub', 'bcdedit', 'cipher', 'wipefs', 'blkdiscard',
    'format-volume', 'clear-disk', 'remove-partition', 'initialize-disk',
    'stop-computer', 'restart-computer',
}

# Roots: critical only when named exactly (anything *under* them is ordinary).
_CRITICAL_EXACT = {
    '/', '~', 'c:', '$home', '$homepath', '%userprofile%', '$env:userprofile', '$env:home',
}
# System directories: critical when named exactly *or* for anything beneath them.
_CRITICAL_PATHS = {
    '/etc', '/usr', '/bin', '/sbin', '/boot', '/opt', '/root',
    '/lib', '/lib32', '/lib64', '/var', '/proc', '/sys', '/dev', '/srv',
    '/system', '/library', '/applications',
    'c:/windows', 'c:/program files', 'c:/program files (x86)', 'c:/programdata',
    '%windir%', '%systemroot%', '%programfiles%', '$env:windir', '$env:systemroot',
    'hklm:', 'hkcr:', 'hkcc:',
}

_SHELLS = {'bash', 'sh', 'zsh', 'fish', 'dash', 'ksh', 'pwsh', 'powershell', 'cmd', 'cmd.exe'}
_SCRIPT_INTERPRETERS = {
    'python', 'python3', 'python2', 'py', 'node', 'nodejs', 'deno', 'bun',
    'ruby', 'perl', 'php', 'lua', 'rscript', 'julia',
}
_INTERPRETERS = _SHELLS | _SCRIPT_INTERPRETERS

# Flags that mean "run this inline string" per interpreter.
_INLINE_FLAGS = {
    'python': {'-c'}, 'python3': {'-c'}, 'python2': {'-c'}, 'py': {'-c'},
    'node': {'-e', '--eval', '-p', '--print'}, 'nodejs': {'-e', '--eval'},
    'deno': {'eval'}, 'bun': {'-e'},
    'ruby': {'-e'}, 'perl': {'-e', '-E'}, 'php': {'-r'}, 'lua': {'-e'},
    'rscript': {'-e'}, 'julia': {'-e'},
    'bash': {'-c'}, 'sh': {'-c'}, 'zsh': {'-c'}, 'dash': {'-c'}, 'ksh': {'-c'}, 'fish': {'-c'},
    'pwsh': {'-c', '-command'}, 'powershell': {'-c', '-command'},
    'cmd': {'/c', '/k'}, 'cmd.exe': {'/c', '/k'},
}
_PS_ENCODED_FLAGS = {'-encodedcommand', '-e', '-ec', '-enc', '-en', '-enco', '-encod'}


# Directories whose *children* are whole user homes: the directory itself and
# any direct child is critical, but deeper paths (a user's project) are not.
_HOME_ROOTS = ('/home', 'c:/users', '/users')


def _is_critical_path(target: str) -> bool:
    norm = target.strip().strip('"\'').lower().replace('\\', '/').rstrip('/')
    if not norm or norm in _CRITICAL_EXACT:
        return True
    if set(norm).issubset({'*', '.', '?', ' ', '/'}):
        return True
    for cp in _CRITICAL_PATHS:
        if norm == cp or norm.startswith(cp + '/'):
            return True
    for hr in _HOME_ROOTS:
        if norm == hr or (norm.startswith(hr + '/') and norm.count('/') == hr.count('/') + 1):
            return True
    if norm.startswith('/dev/'):
        return True
    return False


def _redirect_hits_critical_file(command: str) -> bool:
    targets = re.findall(r'(?<![<>])>{1,2}\s*(\S+)', command)
    return any(_is_critical_path(t) for t in targets if not t.startswith('&'))


_PIPE_TO_SHELL_RE = re.compile(
    r'\|\s*(?:sudo\s+)?(bash|sh|zsh|fish|dash|ksh|cmd(?:\.exe)?|powershell|pwsh|'
    r'python\d*|perl|ruby|node|iex|invoke-expression)\b',
    re.IGNORECASE,
)

# Patterns inside interpreter payloads that shell out or delete.
_PAYLOAD_SHELL_CALL_RE = re.compile(
    r'os\.system|os\.popen|os\.exec\w*|os\.remove|os\.unlink|os\.rmdir|os\.removedirs|'
    r'subprocess\.|shutil\.rmtree|shutil\.move|pty\.spawn|'
    r'child_process|execSync|spawnSync|fs\.rm|fs\.unlink|fs\.rmdir|rimraf|'
    r'`[^`]*`|\bsystem\s*\(|%x\(|\bexec\s*\(|\beval\s*\(|'
    r'Runtime\.getRuntime|Process\.Start|Invoke-Expression|\biex\b',
    re.IGNORECASE,
)
_STRING_LITERAL_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'')


def _classify_script_payload(payload: str, base: str, depth: int) -> Classification:
    """Classify an inline program for a *non-shell* interpreter.

    We cannot execute Python/JS statically, but we can (a) scan every string
    literal inside it as a shell command and (b) detect shell-out APIs.
    """
    for m in _STRING_LITERAL_RE.finditer(payload):
        lit = m.group(1) if m.group(1) is not None else m.group(2)
        if lit and lit.strip():
            inner = _safety_level(lit, depth + 1)
            if inner.level == Level.HARD_BLOCK:
                return Classification(Level.HARD_BLOCK,
                    f"Inline {base} program embeds a dangerous shell command: {inner.reason}")
    if _PAYLOAD_SHELL_CALL_RE.search(payload):
        return Classification(Level.NEEDS_CONFIRMATION,
            f"Inline {base} program shells out or deletes files — inspect before running")
    return Classification(Level.NEEDS_CONFIRMATION,
                          f"'{base}' executes an inline program — inspect the payload")


def _after_flag(tokens: list[str], flags: set[str]) -> str:
    """Return the argument following any of ``flags`` (or glued, e.g. -c'cmd')."""
    for i, tok in enumerate(tokens):
        low = tok.lower()
        if low in flags and i + 1 < len(tokens):
            return tokens[i + 1]
        for f in flags:
            if len(f) == 2 and f.startswith('-') and low.startswith(f) and len(tok) > 2:
                return tok[2:]  # glued form: -c"rm -rf /" tokenises to -crm -rf /
    return ''


def _find_exec_payloads(tokens: list[str]) -> list[str]:
    """Extract the command(s) given to find's -exec/-execdir/-ok/-okdir."""
    payloads: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i] in ('-exec', '-execdir', '-ok', '-okdir'):
            j = i + 1
            cmd: list[str] = []
            while j < len(tokens) and tokens[j] not in (';', '+', '\\;'):
                cmd.append(tokens[j])
                j += 1
            if cmd:
                payloads.append(' '.join(t if t != '{}' else './__match__' for t in cmd))
            i = j
        i += 1
    return payloads


def _rm_flag_set(flags: set[str], letter: str, longs: set[str], win: set[str]) -> bool:
    """True if any flag carries ``letter`` in a short cluster (-rf, -fr, -Rf)."""
    if longs & flags or win & flags:
        return True
    for f in flags:
        if f.startswith('--'):
            continue
        body = f.lstrip('-')
        if body.isalpha() and len(body) <= 4 and letter in body:
            return True
    return False


def _classify_single(command: str, depth: int) -> Classification:  # noqa: C901 - table-driven
    cmd = command.strip()
    # Unwrap sub-shell / group syntax: `(rm -rf /)` and `{ rm -rf /; }` run the
    # inner command just the same.
    while len(cmd) >= 2 and cmd[0] in '({' and cmd[-1] in ')}':
        cmd = cmd[1:-1].strip().rstrip(';').strip()
    if not cmd or cmd.startswith('#'):
        return Classification(Level.SAFE, "Empty or comment line")

    raw_tokens = _tokenize(cmd)
    tokens = _strip_prefixes(raw_tokens)
    if not tokens:
        return Classification(Level.SAFE, "Environment assignment / bare wrapper — no command")
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
            "Redirects output into a critical system file or device — would overwrite it")

    # ── 4. Always-dangerous base commands ─────────────────────────────────
    if base.startswith("mkfs"):
        return Classification(Level.HARD_BLOCK,
            f"'{base}' creates a filesystem — destroys existing data")
    if base in _ALWAYS_HARD_BLOCK:
        if base == 'cipher':
            if '/w' in flags or any(t.lower().startswith('/w:') for t in tokens):
                return Classification(Level.HARD_BLOCK,
                    "cipher /w performs a secure wipe of free space — irreversible")
            return Classification(Level.NEEDS_CONFIRMATION,
                "cipher modifies file encryption — review flags and target")
        return Classification(Level.HARD_BLOCK,
            f"'{base}' performs low-level irreversible disk, boot or power operations")

    # ── 5. dd — only dangerous when writing to a raw device ───────────────
    if base == 'dd':
        if any(re.match(r'of=/dev/', t, re.IGNORECASE) for t in tokens):
            return Classification(Level.HARD_BLOCK,
                "dd writing to a raw block device — will permanently destroy data")
        return Classification(Level.NEEDS_CONFIRMATION,
            "dd can overwrite files — verify 'of=' target carefully")

    # ── 6. Wrappers that execute their arguments ──────────────────────────
    if base in ('eval', 'xargs', 'parallel', 'invoke-expression', 'iex', 'source', '.'):
        inner_tokens = tokens[1:]
        if base in ('xargs', 'parallel'):
            k = 0
            while k < len(inner_tokens) and inner_tokens[k].startswith('-'):
                if inner_tokens[k] in ('-I', '-n', '-P', '-L', '-d', '-s', '-a', '-E') \
                        and k + 1 < len(inner_tokens):
                    k += 1
                k += 1
            inner_tokens = [t for t in inner_tokens[k:] if not t.startswith('<')]
        inner = ' '.join(inner_tokens).strip()
        if not inner:
            return Classification(Level.NEEDS_CONFIRMATION,
                f"'{base}' with no visible command — executes stdin; cannot be verified")
        res = _safety_level(inner, depth + 1)
        if base in ('xargs', 'parallel'):
            inner_base = _base_command(_strip_prefixes(_tokenize(inner)))
            if res.level == Level.NEEDS_CONFIRMATION and inner_base in (
                    'rm', 'del', 'rmdir', 'rd', 'remove-item', 'unlink'):
                return Classification(Level.HARD_BLOCK,
                    f"'{base}' feeds an unverifiable file list into a delete — refusing")
            return Classification(max(res.level, Level.NEEDS_CONFIRMATION),
                                  f"'{base}' wrapper: {res.reason}")
        return Classification(res.level, f"'{base}' wrapper: {res.reason}")

    # ── 7. find — read-only unless it deletes or executes ─────────────────
    if base == 'find':
        if '-delete' in flags:
            roots = [a for a in args if not a.startswith('-')] or ['.']
            filtered = bool({'-name', '-iname', '-path', '-regex', '-type', '-mtime',
                             '-newer', '-size', '-empty'} & flags)
            for r in roots:
                if r in ('.', './', '.\\') and filtered:
                    continue  # `find . -name '*.pyc' -delete` is routine cleanup
                if _is_critical_path(r) or _has_unresolved_var(r):
                    suffix = "" if filtered else " with no filter — would delete everything"
                    return Classification(Level.HARD_BLOCK,
                        "find -delete rooted at a critical or unverifiable path" + suffix)
            return Classification(Level.NEEDS_CONFIRMATION,
                f"find -delete removes every match under {', '.join(roots)}")
        payloads = _find_exec_payloads(tokens)
        if payloads:
            roots = [a for a in args if not a.startswith('-')] or ['.']
            filtered = bool({'-name', '-iname', '-path', '-regex', '-type', '-mtime',
                             '-newer', '-size', '-empty'} & flags)
            risky_root = any(_is_critical_path(r) and r not in ('.', './', '.\\')
                             or _has_unresolved_var(r) for r in roots)
            worst = Classification(Level.SAFE, "find -exec with a read-only command")
            for p in payloads:
                res = _safety_level(p, depth + 1)
                if res.level > worst.level:
                    worst = Classification(res.level, f"find -exec payload: {res.reason}")
                if res.level == Level.NEEDS_CONFIRMATION:
                    inner_base = _base_command(_strip_prefixes(_tokenize(p)))
                    deletes = inner_base in ('rm', 'del', 'rmdir', 'rd', 'remove-item', 'unlink')
                    if risky_root:
                        return Classification(Level.HARD_BLOCK,
                            f"find -exec mutating every match under a critical root: {res.reason}")
                    if deletes and not filtered:
                        return Classification(Level.HARD_BLOCK,
                            "find -exec deletes every file under the root with no filter")
            return worst
        if any(f in flags for f in ('-fprint', '-fprint0', '-fprintf', '-fls')):
            return Classification(Level.NEEDS_CONFIRMATION, "find writes results to a file")
        return Classification(Level.SAFE, "'find' without -delete/-exec is read-only")

    # ── 8. rm / del / rmdir / Remove-Item ─────────────────────────────────
    if base in ('rm', 'del', 'remove-item', 'ri', 'erase', 'rmdir', 'rd', 'unlink'):
        recursive = _rm_flag_set(flags, 'r', {'--recursive', '-recurse'}, {'/s'})
        force = _rm_flag_set(flags, 'f', {'--force', '-force'}, {'/f', '/q'})
        targets = [a for a in args if a.lower() not in ('-path', '-literalpath')]

        if not targets and (recursive or force):
            return Classification(Level.HARD_BLOCK,
                "Recursive/forced deletion with no explicit safe target specified")

        for target in targets:
            if _is_critical_path(target):
                return Classification(Level.HARD_BLOCK,
                    f"Targeting critical system path '{target}' for deletion")
            if (recursive or force) and _has_unresolved_var(target):
                return Classification(Level.HARD_BLOCK,
                    f"Recursive/forced deletion of unresolvable variable target '{target}' "
                    "— an empty variable would delete the root")
            if (recursive or force) and ('*' in target or target in ('.', './', '.\\', '..')):
                return Classification(Level.HARD_BLOCK,
                    f"Recursive/forced deletion with wildcard target '{target}'")

        if recursive and force:
            return Classification(Level.NEEDS_CONFIRMATION,
                f"Recursive forced deletion of '{', '.join(targets)}' — cannot be undone")

        return Classification(Level.NEEDS_CONFIRMATION,
            f"Deletes file(s): {', '.join(targets) or '(no target specified)'}")

    # ── 9. Write-to-file utilities that take a target path ────────────────
    if base in ('tee', 'truncate', 'install', 'ln', 'mknod', 'mkswap', 'swapoff', 'swapon'):
        for a in args:
            if _is_critical_path(a):
                return Classification(Level.HARD_BLOCK,
                    f"'{base}' writing to critical path or device '{a}'")
        return Classification(Level.NEEDS_CONFIRMATION, f"'{base}' writes to a file")

    # ── 10. kill / pkill — special case: kill all processes ───────────────
    if base in ('kill', 'pkill', 'killall', 'stop-process', 'taskkill', 'spps'):
        sigkill = '-9' in flags or '--sigkill' in flags or '-kill' in flags or '-s' in flags
        if '-1' in flags and (sigkill or base == 'kill'):
            return Classification(Level.HARD_BLOCK,
                "Signal to PID -1 (every process) — will crash the session or system")
        for a in args:
            if (sigkill or base == 'kill') and a in ('1', 'init', 'systemd'):
                return Classification(Level.HARD_BLOCK,
                    "SIGKILL to PID 1 — will crash the system")
        return Classification(Level.NEEDS_CONFIRMATION,
            f"Terminates process(es): {', '.join(args) or 'unspecified'}")

    # ── 11. shutdown / reboot / halt ──────────────────────────────────────
    if base in ('shutdown', 'reboot', 'halt', 'poweroff', 'init', 'telinit'):
        return Classification(Level.HARD_BLOCK,
            f"'{base}' terminates or restarts the OS — irreversible remotely")

    # ── 12. Windows registry ──────────────────────────────────────────────
    if base in ('reg', 'reg.exe'):
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
        if sub in ('query', 'compare', 'export'):
            return Classification(Level.SAFE, f"Registry {sub} — read-only")
        return Classification(Level.NEEDS_CONFIRMATION,
            f"Modifies the registry: reg {sub}")

    # ── 13. chmod / icacls on critical paths ──────────────────────────────
    if base in ('chmod', 'chown', 'chgrp', 'icacls', 'attrib', 'takeown', 'setfacl', 'chattr'):
        for a in args:
            if _is_critical_path(a):
                return Classification(Level.HARD_BLOCK,
                    f"Changing ownership/permissions on critical system path '{a}'")
        return Classification(Level.NEEDS_CONFIRMATION,
            "Changes file permissions or ownership — verify target and mode")

    # ── 14. Package managers ──────────────────────────────────────────────
    if base in _PKG_MANAGERS:
        sub = tokens[1].lower() if len(tokens) > 1 else ''
        if sub in _PKG_MUTATE_SUBS:
            return Classification(Level.NEEDS_CONFIRMATION,
                f"'{base} {sub}' modifies installed packages")
        if sub in _PKG_EXEC_SUBS:
            return Classification(Level.NEEDS_CONFIRMATION,
                f"'{base} {sub}' executes project scripts — review the scripts first")
        if sub == 'config' and any(t in ('set', 'unset', 'delete', 'edit', 'rm') for t in tokens[2:3]):
            return Classification(Level.NEEDS_CONFIRMATION, f"'{base} config' write")
        if sub in _PKG_READ_SUBS or not sub or sub == 'config':
            return Classification(Level.SAFE,
                                  f"'{' '.join(filter(None, (base, sub)))}' is a read-only query")
        return Classification(Level.NEEDS_CONFIRMATION,
            f"Package manager command '{base} {sub}' — review before running")

    # ── 15. Git ───────────────────────────────────────────────────────────
    if base == 'git':
        gtoks = tokens[1:]
        while gtoks and gtoks[0].startswith('-'):
            if gtoks[0] in ('-C', '-c', '--git-dir', '--work-tree') and len(gtoks) > 1:
                gtoks = gtoks[2:]
            else:
                gtoks = gtoks[1:]
        sub = gtoks[0].lower() if gtoks else ''
        rest = gtoks[1:]
        if not sub:
            return Classification(Level.SAFE, "'git' with no sub-command — prints help")
        if sub in _GIT_SAFE_SUBS:
            write_flags = _GIT_SAFE_SUB_WRITE_FLAGS.get(sub, set())
            if any(t in write_flags for t in rest):
                return Classification(Level.NEEDS_CONFIRMATION,
                    f"'git {sub}' with a write/delete flag modifies refs")
            return Classification(Level.SAFE, f"'git {sub}' is a read-only operation")
        if sub == 'push' and any(t in ('--force', '-f', '--force-with-lease', '--delete',
                                       '-d', '--mirror') or t.startswith('+') for t in rest):
            return Classification(Level.NEEDS_CONFIRMATION,
                "'git push --force/--delete' rewrites or removes remote history — confirm explicitly")
        if sub in _GIT_CONFIRM_SUBS:
            return Classification(Level.NEEDS_CONFIRMATION,
                f"'git {sub}' modifies repository or config state")
        return Classification(Level.NEEDS_CONFIRMATION,
            f"'git {sub}' — unknown git sub-command; require review")

    # ── 16. Interpreters with an inline payload — classify the payload ────
    if base in _INTERPRETERS:
        if base in ('pwsh', 'powershell') and (_PS_ENCODED_FLAGS & flags):
            return Classification(Level.HARD_BLOCK,
                "Base64-encoded PowerShell command cannot be inspected — refusing")
        inline_flags = _INLINE_FLAGS.get(base, set())
        payload = _after_flag(tokens, inline_flags) if inline_flags else ''
        if payload:
            if base in _SHELLS:
                inner = _safety_level(payload, depth + 1)
                return Classification(inner.level, f"Interpreter inline payload: {inner.reason}")
            return _classify_script_payload(payload, base, depth)
        if any(f in flags for f in inline_flags):
            return Classification(Level.NEEDS_CONFIRMATION,
                f"'{base}' executes an inline program — inspect the payload")
        if ({'--version', '--help', '-v', '-h'} & flags) and not args:
            return Classification(Level.SAFE,
                f"'{base}' version / help check — informational")
        if args:
            return Classification(Level.NEEDS_CONFIRMATION,
                f"Executes '{args[0]}' — inspect the script before running")
        return Classification(Level.SAFE, f"'{base}' with no script — opens interactive REPL")

    # ── 17. --version / --help long flags make a command safe ─────────────
    if ({'--version', '--help', '/help', '/?'} & flags) and not args:
        return Classification(Level.SAFE,
            f"'{base}' called with --version / --help — informational only")

    # ── 18. Output redirection (non-critical target) ──────────────────────
    if re.search(r'(?<![<>\d])>{1,2}\s*[^&\s]', cmd):
        return Classification(Level.NEEDS_CONFIRMATION,
            "Redirects output to a file — will create or overwrite it")

    # ── 19. Service management ────────────────────────────────────────────
    if base in ('systemctl', 'service', 'sc', 'sc.exe', 'net', 'launchctl',
                'start-service', 'stop-service', 'restart-service', 'set-service'):
        sub = tokens[1].lower() if len(tokens) > 1 else ''
        if base in ('stop-service', 'restart-service', 'set-service', 'start-service'):
            return Classification(Level.NEEDS_CONFIRMATION, f"'{base}' modifies service state")
        if base == 'systemctl' and sub in ('poweroff', 'reboot', 'halt', 'kexec'):
            return Classification(Level.HARD_BLOCK,
                f"'systemctl {sub}' powers off or reboots the machine")
        if sub in ('status', 'list', 'list-units', 'list-unit-files', 'is-active',
                   'is-enabled', 'show', 'cat', 'help', 'query', 'queryex', 'view', 'print'):
            return Classification(Level.SAFE, "Service status / list query — read-only")
        return Classification(Level.NEEDS_CONFIRMATION,
            f"'{base} {sub}' modifies service state")

    # ── 20. Network (download / remote shell) ─────────────────────────────
    if base in ('curl', 'wget', 'invoke-webrequest', 'iwr', 'invoke-restmethod', 'irm',
                'aria2c', 'http', 'https', 'nc', 'ncat', 'netcat', 'telnet', 'ssh', 'scp',
                'sftp', 'ftp'):
        return Classification(Level.NEEDS_CONFIRMATION,
            "Makes a network request — may download files or exfiltrate data")

    # ── 21. sed with -i (in-place edit) ───────────────────────────────────
    if base == 'sed':
        if any(f == '-i' or (f.startswith('-i') and not f.startswith('-in'))
               or f == '--in-place' or f.startswith('--in-place=') for f in flags):
            return Classification(Level.NEEDS_CONFIRMATION,
                "sed -i modifies files in place — backup recommended")
        return Classification(Level.SAFE, "sed without -i — read-only stream editor")

    # ── 22. Containers / infra ────────────────────────────────────────────
    if base in ('docker', 'podman', 'kubectl', 'helm', 'terraform', 'tofu', 'pulumi',
                'aws', 'gcloud', 'az', 'vagrant'):
        return _classify_infra(base, tokens, flags, raw_tokens)

    # ── 23. PowerShell cmdlets with state-changing verbs ──────────────────
    m = re.match(r'^(remove|clear|set|new|stop|restart|disable|enable|uninstall|rename|move|'
                 r'copy|invoke|start|register|unregister|reset|format|install|update|add|'
                 r'import|export)-', base)
    if m:
        if base.startswith(('remove-', 'clear-', 'set-', 'format-')):
            for a in args:
                if _is_critical_path(a):
                    return Classification(Level.HARD_BLOCK,
                        f"'{base}' targeting critical system path '{a}'")
        return Classification(Level.NEEDS_CONFIRMATION,
            f"PowerShell cmdlet '{base}' modifies state — review before running")

    # ── 24. Known read-only commands ──────────────────────────────────────
    if base in _SAFE_COMMANDS:
        return Classification(Level.SAFE, f"'{base}' is a read-only / informational command")

    # ── 25. File creation or movement ─────────────────────────────────────
    if base in ('mkdir', 'md', 'touch', 'new-item',
                'cp', 'copy', 'mv', 'move', 'xcopy', 'robocopy', 'ren', 'rename',
                'rsync', 'copy-item', 'move-item', 'rename-item'):
        movers = ('cp', 'copy', 'mv', 'move', 'xcopy', 'robocopy', 'rsync',
                  'copy-item', 'move-item')
        if len(args) >= 2 and base in movers:
            dest = args[-1]
            if _is_critical_path(dest):
                return Classification(Level.HARD_BLOCK,
                    f"'{base}' writing onto critical path or device '{dest}'")
            if base in ('mv', 'move', 'move-item', 'ren', 'rename') and any(
                    _is_critical_path(s) for s in args[:-1]):
                return Classification(Level.HARD_BLOCK,
                    f"'{base}' relocates a critical system path — the system would not boot")
            if base in ('rsync', 'robocopy') and any(
                    f in flags for f in ('--delete', '/mir', '/purge',
                                         '--delete-before', '--delete-after')):
                if _has_unresolved_var(dest):
                    return Classification(Level.HARD_BLOCK,
                        f"'{base}' with delete/mirror onto unresolvable destination '{dest}'")
                return Classification(Level.NEEDS_CONFIRMATION,
                    f"'{base}' mirror/delete mode removes files at '{dest}' absent from source")
        return Classification(Level.NEEDS_CONFIRMATION,
            f"'{base}' creates, copies or moves files — may overwrite destinations")

    # ── Default: unknown command — conservatively flag for review ──────────
    return Classification(Level.NEEDS_CONFIRMATION,
        f"Unknown command '{base}' — safety cannot be determined; require human review")


_INFRA_READ_SUBS = {
    'ps', 'images', 'logs', 'inspect', 'version', 'info', 'get', 'describe', 'top',
    'stats', 'history', 'diff', 'events', 'port', 'show', 'plan', 'validate', 'fmt',
    'output', 'list', 'ls', 'status', 'context', 'search', 'explain', 'graph',
    'api-resources', 'api-versions', 'cluster-info', 'whoami', 'providers', 'help',
}


def _classify_infra(base: str, tokens: list[str], flags: set[str],
                    raw_tokens: list[str]) -> Classification:
    sub = tokens[1].lower() if len(tokens) > 1 else ''
    third = tokens[2].lower() if len(tokens) > 2 else ''
    rest = [t.lower() for t in tokens[2:]]
    generated = any('$(' in t or '`' in t for t in raw_tokens)

    if base in ('terraform', 'tofu'):
        if sub == 'destroy':
            return Classification(Level.HARD_BLOCK, f"'{base} destroy' tears down real infrastructure")
        if sub == 'state' and third in ('rm', 'mv', 'push', 'replace-provider'):
            return Classification(Level.HARD_BLOCK,
                f"'{base} state {third}' mutates state — can orphan real infrastructure")
        if sub == 'apply' and any(f in flags for f in ('-auto-approve', '--auto-approve')):
            return Classification(Level.HARD_BLOCK,
                f"'{base} apply -auto-approve' changes infrastructure with no plan review")
        if sub in _INFRA_READ_SUBS and not (sub == 'state' and third not in ('list', 'show', 'pull')):
            return Classification(Level.SAFE, f"'{base} {sub}' is read-only")
        return Classification(Level.NEEDS_CONFIRMATION, f"'{base} {sub}' changes infrastructure")

    if base == 'kubectl':
        if sub == 'delete' and (any(f in flags for f in ('--all', '-A', '--all-namespaces'))
                                or third in ('namespace', 'namespaces', 'ns', 'node', 'nodes',
                                             'pv', 'persistentvolume', 'persistentvolumes', 'crd')):
            return Classification(Level.HARD_BLOCK,
                "'kubectl delete' of cluster-wide or all resources — unrecoverable")
        if sub == 'config' and third not in ('view', 'get-contexts', 'current-context', 'get-clusters', 'get-users'):
            return Classification(Level.NEEDS_CONFIRMATION, "kubectl config mutation")
        if sub in _INFRA_READ_SUBS or sub == 'config':
            return Classification(Level.SAFE, f"'kubectl {sub}' is read-only")
        return Classification(Level.NEEDS_CONFIRMATION, f"'kubectl {sub}' changes cluster state")

    if base in ('docker', 'podman'):
        if sub in ('system', 'volume', 'network', 'container', 'image', 'builder') \
                and third in ('prune', 'rm', 'rmi'):
            if _rm_flag_set(flags, 'a', {'--all', '--volumes'}, set()) \
                    or _rm_flag_set(flags, 'f', {'--force'}, set()):
                return Classification(Level.HARD_BLOCK,
                    f"'{base} {sub} {third}' with --all/--force destroys data that cannot be recovered")
            return Classification(Level.NEEDS_CONFIRMATION, f"'{base} {sub} {third}' removes resources")
        if sub in ('rm', 'rmi') and (generated or any(f in flags for f in ('-f', '--force')) and generated):
            return Classification(Level.HARD_BLOCK,
                f"'{base} {sub}' over a generated list removes every container/image")
        if sub == 'run' and any(t in ('--privileged', '-v', '--volume', '--mount') for t in rest) \
                and any(r.startswith(('/:', '/etc', '/var/run/docker.sock')) for r in rest):
            return Classification(Level.HARD_BLOCK,
                f"'{base} run' mounting host root or docker socket — container escape surface")
        if sub in _INFRA_READ_SUBS or (sub in ('system', 'volume', 'network', 'image', 'container')
                                       and third in ('ls', 'inspect', 'df', 'info', 'events', 'list')):
            return Classification(Level.SAFE,
                                  f"'{' '.join(filter(None, (base, sub, third)))}' is read-only")
        return Classification(Level.NEEDS_CONFIRMATION, f"'{base} {sub}' changes container state")

    # helm / pulumi / cloud CLIs: the verb may be the 2nd or 3rd token
    # (`helm uninstall x`, `aws s3 rb ...`, `gcloud compute instances delete ...`).
    verbs = {'destroy', 'uninstall', 'delete', 'rm', 'rb', 'remove', 'purge',
             'terminate', 'deregister', 'delete-stack', 'delete-bucket'}
    if sub in verbs or any(t in verbs for t in rest[:3]):
        if any(f in flags for f in ('--all', '--force', '-f', '--yes', '-y', '-q', '--quiet',
                                    '--skip-preview', '--no-wait', '--recursive')):
            return Classification(Level.HARD_BLOCK,
                f"'{base} {sub}' forced/unattended deletion of cloud resources")
        return Classification(Level.NEEDS_CONFIRMATION, f"'{base} {sub}' deletes cloud resources")
    if sub in _INFRA_READ_SUBS or third in ('list', 'describe', 'get', 'show', 'ls'):
        return Classification(Level.SAFE, f"'{base} {sub}' is read-only")
    return Classification(Level.NEEDS_CONFIRMATION, f"'{base} {sub}' changes cloud or infra state")


_GROUP_RE = re.compile(r'^\{\s*(.*?)\s*;?\s*\}$|^\((.*)\)$', re.S)


def _unwrap_group(command: str) -> str:
    """Strip one or more enclosing `{ ...; }` / `( ... )` group wrappers."""
    while True:
        m = _GROUP_RE.match(command)
        if not m:
            return command
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        # Only unwrap when the parens are balanced as a single group:
        # `(a) && (b)` must stay as two segments.
        if m.group(2) is not None:
            depth = 0
            for i, ch in enumerate(command):
                depth += ch == '('
                depth -= ch == ')'
                if depth == 0 and i < len(command) - 1:
                    return command
        command = inner.strip()


def _safety_level(command: str, depth: int) -> Classification:
    command = _unwrap_group(command.strip())
    if not command:
        return Classification(Level.SAFE, "Empty input")
    if depth > _MAX_DEPTH:
        return Classification(Level.HARD_BLOCK,
            "Command nesting too deep to analyse — refusing")

    # Pipe-to-shell is checked whole, before splitting, so `curl ... | bash`
    # is caught even though the split would separate the two segments.
    if _PIPE_TO_SHELL_RE.search(command):
        return Classification(Level.HARD_BLOCK,
            "Pipes output into a shell interpreter — arbitrary remote code execution")

    worst: Classification | None = None

    def consider(result: Classification) -> bool:
        nonlocal worst
        if worst is None or result.level > worst.level:
            worst = result
        return worst.level == Level.HARD_BLOCK

    for sub in _split_subcommands(command):
        # Classify any command-substitution payloads first — they run.
        for inner in _extract_substitutions(sub):
            if consider(_safety_level(inner, depth + 1)):
                return worst  # type: ignore[return-value]
        if consider(_classify_single(sub, depth)):
            return worst  # type: ignore[return-value]

    return worst or Classification(Level.SAFE, "Empty input")


def tokenize_command(command: str) -> list[str]:
    """Public, platform-aware tokenizer shared with the executor.

    Using the *same* tokenizer for classification and execution closes the
    parser-differential gap: what the gate judged is what runs.
    """
    return _tokenize(command)


_SHELL_OPERATOR_RE = re.compile(r'[|;&<>`]|\$\(|\n')


def has_shell_operators(command: str) -> bool:
    """True if the command needs a shell to run (pipes, chains, redirects, substitution)."""
    return bool(_SHELL_OPERATOR_RE.search(command))


def safety_level(command: str) -> Classification:
    """
    Classify a shell command string as SAFE, NEEDS_CONFIRMATION, or HARD_BLOCK.

    Handles chained commands (&&, ;, ||, |, &, newlines), command substitution
    ($(...), backticks, <(...)), ``xargs``/``eval``/``find -exec`` wrappers, and
    interpreter inline payloads. Returns the highest (worst-case)
    classification found anywhere in the command. Never raises.
    """
    try:
        return _safety_level(command, 0)
    except RecursionError:  # pragma: no cover - defensive
        return Classification(Level.HARD_BLOCK, "Command too complex to analyse — refusing")
    except Exception as e:  # pragma: no cover - the gate must never crash open
        return Classification(Level.NEEDS_CONFIRMATION,
                              f"Classifier error ({type(e).__name__}) — require review")
