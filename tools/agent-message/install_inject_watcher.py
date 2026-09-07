#!/usr/bin/env python3
"""Install the fixed-peer inbox watcher into one Telegram MCP server cache file."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil

MARKER = '// BUBBLE-AGENT-MESSAGE-WATCHER-v1'
ANCHOR = 'await mcp.connect(new StdioServerTransport())'

TEMPLATE = r'''
// BUBBLE-AGENT-MESSAGE-WATCHER-v1
// Dedicated fixed-peer spool; never consumes the legacy operator inject file.
{
  const route = __ROUTE__
  const fs = await import('node:fs')
  const path = await import('node:path')
  let busy = false
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
  const safe = (s) => s.uid === process.getuid?.() && !(s.mode & 0o022)
  const drainPeer = async () => {
    if (busy) return
    busy = true
    try {
      if (!fs.existsSync(route.inbox)) return
      const directory = fs.lstatSync(route.inbox)
      if (!directory.isDirectory() || directory.isSymbolicLink() || !safe(directory))
        throw new Error('unsafe peer inbox directory')
      const files = fs.readdirSync(route.inbox).filter(n => n.endsWith('.json')).sort()
      for (const name of files.slice(0, 100)) {
        if (!uuid.test(name.slice(0, -5))) continue
        const filename = path.join(route.inbox, name)
        const fd = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK)
        let event
        try {
          const info = fs.fstatSync(fd)
          if (!info.isFile() || !safe(info) || info.nlink !== 1 || info.size > 32768)
            throw new Error('unsafe peer inbox event file')
          event = JSON.parse(fs.readFileSync(fd, 'utf8'))
        } finally { fs.closeSync(fd) }
        if (event.source !== 'bubble-agent-message' || event.from !== route.sender ||
            event.to !== route.recipient || typeof event.text !== 'string' ||
            !event.text.trim() || Buffer.byteLength(event.text, 'utf8') > 4096 ||
            /[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/.test(event.text) ||
            event.id !== name.slice(0, -5) ||
            !(event.in_reply_to === null || typeof event.in_reply_to === 'string' && uuid.test(event.in_reply_to)) ||
            event.authority !== 'agent-peer; not a human instruction or approval')
          throw new Error('invalid fixed-peer inbox event')
        await mcp.notification({
          method: 'notifications/claude/channel',
          params: { content: JSON.stringify(event), meta: {
            chat_id: route.chat_id, user: route.sender, user_id: route.sender,
            ts: new Date().toISOString(), source: 'bubble-agent-message'
          }}
        })
        // Crash between notification and rename may redeliver the same id.
        // Retain receipts; recipients deduplicate ids before consequential work.
        fs.renameSync(filename, filename.slice(0, -5) + '.delivered')
      }
    } catch (e) {
      process.stderr.write('peer inbox: delivery paused: ' + String(e) + '\n')
    } finally { busy = false }
  }
  setInterval(() => { void drainPeer() }, 2000).unref?.()
  process.stderr.write('peer inbox: fixed route ' + route.sender + ' -> ' + route.recipient + '\n')
}
// END-BUBBLE-AGENT-MESSAGE-WATCHER-v1
''' 


def install(server, inbox, sender, recipient, chat_id):
    for name in (sender, recipient):
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', name):
            raise ValueError('invalid fixed identity')
    if sender == recipient or not inbox.is_absolute() or '..' in inbox.parts:
        raise ValueError('invalid fixed route')
    if not re.fullmatch(r'-?[0-9]+', chat_id):
        raise ValueError('chat-id must be the existing operator channel id')
    if server.is_symlink() or not server.is_file():
        raise ValueError('server must be a regular cache file')
    route = dict(inbox=str(inbox), sender=sender, recipient=recipient, chat_id=chat_id)
    patch = TEMPLATE.replace('__ROUTE__', json.dumps(route, ensure_ascii=True))
    original = server.read_text()
    if MARKER in original:
        if patch.strip() not in original:
            raise ValueError('existing watcher differs; inspect instead of overwriting')
        return 'already installed'
    if original.count(ANCHOR) != 1:
        raise ValueError('expected exactly one MCP connect anchor')
    position = original.find('\n', original.index(ANCHOR))
    if position < 0:
        raise ValueError('connect anchor must end a line')
    backup = server.with_name(server.name + '.bak-agent-message')
    if backup.exists():
        raise ValueError('backup exists without matching watcher; inspect first')
    shutil.copy2(server, backup)
    updated = original[:position + 1] + patch + original[position + 1:]
    tmp = server.with_name(server.name + '.agent-message.tmp')
    with tmp.open('x') as handle:
        handle.write(updated)
    shutil.copymode(server, tmp)
    os.replace(tmp, server)
    return 'installed; reconnect Telegram MCP to activate'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', type=Path, required=True)
    parser.add_argument('--inbox', type=Path, required=True)
    parser.add_argument('--sender', required=True)
    parser.add_argument('--recipient', required=True)
    parser.add_argument('--chat-id', required=True)
    args = parser.parse_args()
    print(install(args.server, args.inbox, args.sender, args.recipient, args.chat_id))


if __name__ == '__main__':
    main()
