#!/bin/bash
# Render and load a per-user LaunchAgent, following the Mac gui/<uid> pattern.
set -euo pipefail
framework_root="$(cd "$(dirname "$0")/.." && pwd)"
template="$framework_root/deploy/templates/com.bubble.export-kpis.plist.template"
slug=""; dept_dir=""; transcripts_dir=""; dry_run=0
usage() {
    echo "usage: $0 --slug S --dept-dir D --transcripts-dir T [--framework-root F] [--dry-run]" >&2
    exit 2
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        --slug|--dept-dir|--transcripts-dir|--framework-root)
            [[ $# -ge 2 && -n "$2" ]] || usage
            case "$1" in
                --slug) slug="$2" ;;
                --dept-dir) dept_dir="$2" ;;
                --transcripts-dir) transcripts_dir="$2" ;;
                --framework-root) framework_root="$2" ;;
            esac
            shift 2 ;;
        --dry-run) dry_run=1; shift ;;
        *) usage ;;
    esac
done
[[ -n "$slug" && -n "$dept_dir" && -n "$transcripts_dir" ]] || usage
# Escape XML values without sed replacement ambiguities (&, quotes, spaces).
# Python also makes installation transactional if replacement bootstrap fails.
python3 -I - "$template" "$slug" "$framework_root" "$dept_dir" "$transcripts_dir" "$HOME" "$dry_run" <<'PY'
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape

template, slug, framework, dept, transcripts, home, dry = sys.argv[1:]
if not re.fullmatch(r'[a-z][a-z0-9-]{0,79}', slug):
    raise SystemExit('invalid slug')
values = dict(SLUG=slug, FRAMEWORK_ROOT=os.path.abspath(framework),
              DEPT_DIR=os.path.abspath(dept), TRANSCRIPTS_DIR=os.path.abspath(transcripts),
              HOME=os.path.abspath(home))
rendered = re.sub(r'@(SLUG|FRAMEWORK_ROOT|DEPT_DIR|TRANSCRIPTS_DIR|HOME)@',
                  lambda match: escape(values[match[1]]), Path(template).read_text())
plistlib.loads(rendered.encode())
if shutil.which('plutil'):
    subprocess.run(['plutil', '-lint', '-'], input=rendered.encode(), check=True,
                   stdout=subprocess.DEVNULL)
label = 'com.bubble.export-kpis-' + slug
domain = f'gui/{os.getuid()}'
service = domain + '/' + label
plist = Path(home) / 'Library/LaunchAgents' / (label + '.plist')
probe = ['launchctl', 'print', service]
bootout = ['launchctl', 'bootout', service]
bootstrap = ['launchctl', 'bootstrap', domain, str(plist)]
if dry == '1':
    print(rendered, end='')
    print('# Write the rendered plist to ' + shlex.quote(str(plist)))
    print('if ' + shlex.join(probe) + ' >/dev/null 2>&1; then ' + shlex.join(bootout) + '; fi')
    print(shlex.join(bootstrap))
    raise SystemExit(0)
if not shutil.which('launchctl'):
    raise SystemExit('launchctl is required to install this LaunchAgent')
plist.parent.mkdir(parents=True, exist_ok=True)
(Path(home) / 'Library/Logs').mkdir(parents=True, exist_ok=True)
previous = plist.read_bytes() if plist.exists() else None
loaded = subprocess.run(probe, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

def publish(data):
    fd, temporary = tempfile.mkstemp(prefix='.' + label, dir=plist.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(data)
        os.replace(temporary, plist)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

publish(rendered.encode())
try:
    if loaded:
        subprocess.run(bootout, check=True)
    subprocess.run(bootstrap, check=True)
except subprocess.CalledProcessError:
    # Restore the previous definition and reload it if it was loaded before.
    if previous is not None:
        publish(previous)
    else:
        plist.unlink()
    if loaded:
        if previous is None:
            # A loaded agent can outlive its plist. Retain a reloadable definition.
            publish(rendered.encode())
        if subprocess.run(probe, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            subprocess.run(bootstrap, check=True)
    raise
print(f'Installed and bootstrapped {plist}')
PY
