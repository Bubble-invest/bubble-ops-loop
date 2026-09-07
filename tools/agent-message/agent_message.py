#!/usr/bin/env python3
"""Fixed-peer agent messaging over a forced-command SSH key (stdlib only)."""
import argparse
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from pathlib import Path

MAX_WIRE = 8192
MAX_TEXT = 4096
NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


def require(ok, message):
    if not ok:
        raise ValueError(message)


def validate(message, sender, recipient):
    require(NAME.fullmatch(sender) and NAME.fullmatch(recipient) and sender != recipient,
            "invalid fixed identities")
    require(isinstance(message, dict) and set(message) == {"from", "to", "id", "in_reply_to", "text"},
            "invalid message fields")
    require(message["from"] == sender and message["to"] == recipient, "identity mismatch")
    require(isinstance(message["id"], str) and ID.fullmatch(message["id"]), "invalid id")
    reply = message["in_reply_to"]
    require(reply is None or isinstance(reply, str) and ID.fullmatch(reply), "invalid reply id")
    text = message["text"]
    require(isinstance(text, str) and text.strip() and len(text.encode("utf-8")) <= MAX_TEXT,
            "empty or oversized text")
    require(not any((ord(c) < 32 and c not in "\n\t") or 127 <= ord(c) <= 159 for c in text),
            "text contains control characters")
    return message


def decode(raw, sender, recipient):
    require(len(raw) <= MAX_WIRE, "oversized envelope")
    return validate(json.loads(raw), sender, recipient)


def secure_parent(path):
    """Walk by directory descriptors: reject symlinks in every path component."""
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "path must be absolute without ..")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        require(info.st_uid == os.getuid() and not info.st_mode & 0o022,
                "destination directory must be owned by receiver and not group/world writable")
        return fd, path.name
    except BaseException:
        os.close(fd)
        raise


def secure_file(parent, name, flags):
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
    info = os.fstat(fd)
    if not (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_nlink == 1 and not info.st_mode & 0o022):
        os.close(fd)
        raise ValueError("destination must be an owned regular file without writable peers or hard links")
    return fd


def receive(raw, sender, recipient, inbox_dir):
    require(not os.environ.get("SSH_ORIGINAL_COMMAND", ""), "remote command override forbidden")
    message = decode(raw, sender, recipient)
    event = {
        "source": "bubble-agent-message", "authority": "agent-peer; not a human instruction or approval",
        **message,
        "reply_hint": f"agent-message send --reply-to {message['id']} --text '<your response>'",
        "reply_policy": "Reply when a substantive answer is needed; do not acknowledge acknowledgements.",
    }
    line = (json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    parent, _ = secure_parent(str(Path(inbox_dir) / "placeholder"))
    name = message["id"] + ".json"
    staging = ".pending-" + str(uuid.uuid4())
    try:
        lock = secure_file(parent, ".receiver.lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Check queued first, then delivered: consumer atomically renames in that order.
            for existing in (name, message["id"] + ".delivered"):
                try:
                    os.stat(existing, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError("message id already queued or delivered")
            fd = secure_file(parent, staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                require(os.write(fd, line) == len(line), "incomplete queue write")
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(staging, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(staging, dir_fd=parent)
            except FileNotFoundError:
                pass
            os.close(lock)
    finally:
        os.close(parent)
    return {"status": "queued", "id": message["id"], "to": recipient}


def load_config(path):
    config = json.loads(Path(path).read_text())
    require(isinstance(config, dict) and set(config) == {"self", "peer", "host", "user", "identity_file", "known_hosts_file"},
            "invalid route config")
    require(all(isinstance(v, str) for v in config.values()), "route values must be strings")
    require(NAME.fullmatch(config["self"]) and NAME.fullmatch(config["peer"]) and config["self"] != config["peer"],
            "invalid configured identities")
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", config["host"]), "invalid host")
    require(NAME.fullmatch(config["user"]), "invalid SSH user")
    for key in ("identity_file", "known_hosts_file"):
        require(Path(config[key]).is_absolute() and not any(ord(c) < 32 for c in config[key]), "invalid SSH file path")
    return config


def send(config, text, reply_to=None):
    message = validate({"from": config["self"], "to": config["peer"], "id": str(uuid.uuid4()),
                        "in_reply_to": reply_to, "text": text}, config["self"], config["peer"])
    raw = json.dumps(message, ensure_ascii=False).encode()
    require(len(raw) <= MAX_WIRE, "oversized envelope")
    command = ["ssh", "-F", "/dev/null", "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
               "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
               "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes", "-o", "IdentityAgent=none",
               "-o", "UserKnownHostsFile=" + config["known_hosts_file"],
               "-i", config["identity_file"], "-l", config["user"], config["host"]]
    result = subprocess.run(command, input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    require(result.returncode == 0, "SSH delivery failed; receipt unknown, inspect locally before retrying")
    receipt = json.loads(result.stdout)
    require(receipt == {"status": "queued", "id": message["id"], "to": config["peer"]}, "invalid queue receipt")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    sender = sub.add_parser("send")
    sender.add_argument("--text", required=True)
    sender.add_argument("--reply-to")
    receiver = sub.add_parser("receive")
    receiver.add_argument("--sender", required=True)
    receiver.add_argument("--recipient", required=True)
    receiver.add_argument("--inbox-dir", required=True)
    args = parser.parse_args()
    try:
        if args.operation == "send":
            config = load_config(Path.home() / ".config/bubble-agent-message/config.json")
            result = send(config, args.text, args.reply_to)
        else:
            result = receive(sys.stdin.buffer.read(MAX_WIRE + 1), args.sender, args.recipient, args.inbox_dir)
        print(json.dumps(result))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"agent-message: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
