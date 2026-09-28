"""Canonical UTF-8 A2A envelope and OpenSSH detached signature transport."""
import argparse
import base64
import datetime
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--to', required=True)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--from', dest='sender', required=True)
    parser.add_argument('--key', default=os.path.expanduser('~/.ssh/a2a_ed25519'))
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', args.sender):
        parser.error('invalid sender principal')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', args.to):
        parser.error('invalid SSH host/alias')
    if not args.state_dir.startswith('/') or any(c in args.state_dir for c in '\r\n\0'):
        parser.error('state-dir must be an absolute remote path')
    # Dedicated Ed25519 key required; no fallback to an ambient signing identity.
    pub = subprocess.run(['ssh-keygen', '-y', '-P', '', '-f', args.key],
                         check=True, capture_output=True).stdout
    if not pub.startswith(b'ssh-ed25519 '):
        parser.error('signing key must be Ed25519 (noninteractive)')
    envelope = dict(v=1, **{'from': args.sender}, to_state_dir=args.state_dir,
                    ts=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    nonce=uuid.uuid4().hex, body=sys.stdin.buffer.read().decode('utf-8'))
    data = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    with tempfile.TemporaryDirectory(prefix='a2a-send-') as tmp:
        message = Path(tmp) / 'envelope'
        message.write_bytes(data)
        message.chmod(0o600)
        subprocess.run(['ssh-keygen', '-Y', 'sign', '-n', 'bubble-a2a', '-f', args.key,
                        str(message)], check=True, capture_output=True)
        signature = Path(str(message) + '.sig').read_bytes()
    line = b'BUBBLE-A2A-SIGNED ' + base64.b64encode(data) + b' ' + base64.b64encode(signature) + b'\n'
    command = 'umask 077; cat >> ' + shlex.quote(args.state_dir.rstrip('/') + '/inject')
    subprocess.run(['ssh', '-o', 'BatchMode=yes', '--', args.to, command], input=line, check=True)


if __name__ == '__main__':
    main()
