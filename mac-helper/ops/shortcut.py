"""Run one of the user's Shortcuts for the Mac helper. Run as: python3 -I shortcut.py shortcut_run '<json arguments>'. Prints one JSON document.

A shortcut runs only if its exact name is listed in the Mac's own allowlist file, one name per line:
    ~/Library/Application Support/icloud-mac-helper/shortcuts-allow.txt
The server has its own list (SHORTCUTS_ALLOW) too; both must allow a name. So a compromised server cannot run a shortcut the owner
never put on the Mac's list, and the list on the Mac can only be changed by someone at the Mac.

Apple's own `shortcuts` command runs it. Optional text input goes in through a private temporary file, and the output is read back
as plain text (truncated). Nothing else is ever executed.
"""
import json
import os
import subprocess
import sys
import tempfile

ALLOW_FILE = os.environ.get("ICLOUD_SHORTCUTS_ALLOW_FILE") or os.path.expanduser(
    "~/Library/Application Support/icloud-mac-helper/shortcuts-allow.txt")
SHORTCUTS = os.environ.get("ICLOUD_SHORTCUTS_BIN") or "/usr/bin/shortcuts"
MAX_OUTPUT = 20000


class ShortcutError(Exception):
    pass


def allowed():
    try:
        with open(ALLOW_FILE, encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")}
    except OSError:
        return set()


def op_run(a):
    name = str(a.get("name") or "").strip()
    if not name:
        raise ShortcutError("a shortcut name is required")
    names = allowed()
    if name not in names:
        raise ShortcutError("'%s' is not in this Mac's list of shortcuts the assistant may run (%s). Add its exact name there, one "
                            "per line, to allow it." % (name, ALLOW_FILE) if names else
                            "no shortcuts are allowed on this Mac yet. List the exact names the assistant may run, one per line, in %s."
                            % ALLOW_FILE)
    budget = max(5, (a.get("budget") or 60) - 5)
    with tempfile.TemporaryDirectory(prefix="icloud-shortcut-") as tmp:
        out_path = os.path.join(tmp, "output.txt")
        cmd = [SHORTCUTS, "run", name, "--output-path", out_path, "--output-type", "public.plain-text"]
        if a.get("input"):
            in_path = os.path.join(tmp, "input.txt")
            fd = os.open(in_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(a["input"]))
            cmd += ["--input-path", in_path]
        try:
            p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=budget)
        except subprocess.TimeoutExpired:
            raise ShortcutError("the shortcut did not finish within %ds and was stopped" % budget)
        if p.returncode != 0:
            raise ShortcutError("the shortcut failed: " + (p.stderr.decode("utf-8", "replace").strip() or "exit %d" % p.returncode)[:300])
        output = ""
        if os.path.exists(out_path):
            with open(out_path, encoding="utf-8", errors="replace") as f:
                output = f.read(MAX_OUTPUT + 1)
    return {"shortcut": name, "ran": True, "output": output[:MAX_OUTPUT], "truncated": len(output) > MAX_OUTPUT}


OPS = {"shortcut_run": op_run}


def main(argv):
    if len(argv) != 3 or argv[1] not in OPS:
        print("usage: shortcut.py shortcut_run '<json>'", file=sys.stderr)
        return 2
    try:
        result = OPS[argv[1]](json.loads(argv[2]))
    except ShortcutError as e:
        print("execution error: Error: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
