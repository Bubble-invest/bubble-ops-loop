"""Bounded SQLite transcript index: no transcript-sized Python collections.

Only fingerprints, 128-bit identities/session keys, day/model and five counters
persist. SQLite transactions publish changes atomically; FULL auto-vacuum
reclaims deleted files. Global dedupe uses a disk-backed temporary table only
when sources change. Warm reports read daily aggregates and distinct run counts.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

VERSION = 4
COUNTERS = ('input', 'output', 'cache_read', 'cache_create', 'reasoning')
USAGE_COLUMNS = 'i,o,r,c,t'
MAXIMA = ','.join(f'{k}=max({k},excluded.{k})' for k in USAGE_COLUMNS.split(','))


def digest(value: str) -> bytes:
    return hashlib.blake2b(value.encode(), digest_size=16).digest()


class UsageCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError('usage cache must not be a symlink')
        self.db = sqlite3.connect(path, timeout=30)
        self.changed = False
        self.order = 0
        # Fixed 8 MiB page cache; sorting/dedupe spill to disk, not Python RAM.
        self.db.execute('PRAGMA cache_size=-8192')
        self.db.execute('PRAGMA temp_store=FILE')
        self.db.execute('PRAGMA temp.cache_size=-2048')
        self.db.execute('PRAGMA main.cache_spill=2048')
        self.db.execute('PRAGMA temp.cache_spill=512')
        self.db.execute('PRAGMA mmap_size=0')
        self.db.execute('PRAGMA foreign_keys=ON')
        if self.db.execute('PRAGMA auto_vacuum').fetchone() == (0,):
            self.db.execute('PRAGMA auto_vacuum=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY, path TEXT UNIQUE, mtime INTEGER,
                size INTEGER, version INTEGER, label TEXT, ordering INTEGER);
            CREATE TABLE IF NOT EXISTS models (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS records (
                file INTEGER REFERENCES files(id) ON DELETE CASCADE,
                identity BLOB, seq INTEGER, session BLOB, day INTEGER, model INTEGER,
                i INTEGER, o INTEGER, r INTEGER, c INTEGER, t INTEGER,
                PRIMARY KEY(file,identity)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS file_daily (
                file INTEGER REFERENCES files(id) ON DELETE CASCADE,
                day INTEGER, model INTEGER, i INTEGER, o INTEGER, r INTEGER,
                c INTEGER, t INTEGER, PRIMARY KEY(file,day,model)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS daily (
                day INTEGER, label TEXT, model INTEGER, i INTEGER, o INTEGER,
                r INTEGER, c INTEGER, t INTEGER,
                PRIMARY KEY(day,label,model)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS runs (
                day INTEGER, label TEXT, session BLOB,
                PRIMARY KEY(day,label,session)) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS runs_by_session ON runs(label,session,day);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER);
            CREATE TEMP TABLE seen (id INTEGER PRIMARY KEY);
        ''')
        self.db.execute('BEGIN IMMEDIATE')

    def close(self):
        # An unfinished scan rolls back its entire publication.
        self.db.close()

    def sync_file(self, path, fingerprint, label, stream, meta, paris_day, resolve):
        self.order += 1
        old = self.db.execute('SELECT id,mtime,size,version,label,ordering FROM files WHERE path=?',
                              (str(path),)).fetchone()
        if old:
            self.db.execute('INSERT INTO seen VALUES (?)', (old[0],))
            if old[1:4] == (*fingerprint, VERSION):
                if old[5] != self.order or (label != '_p_crons' and old[4] != label):
                    self.db.execute('UPDATE files SET ordering=?,label=? WHERE id=?',
                                    (self.order, old[4] if label == '_p_crons' else label, old[0]))
                    self.changed = True
                return
        self.db.execute('SAVEPOINT source')
        try:
            if old:
                fid = old[0]
                self.db.execute('DELETE FROM records WHERE file=?', (fid,))
                self.db.execute('DELETE FROM file_daily WHERE file=?', (fid,))
            else:
                fid = self.db.execute('INSERT INTO files(path) VALUES (?)', (str(path),)).lastrowid
            # stream yields one JSONL row at a time, even for huge individual files.
            for seq, rec in enumerate(stream()):
                self.db.execute('INSERT OR IGNORE INTO models(name) VALUES (?)', (rec['model'],))
                model = self.db.execute('SELECT id FROM models WHERE name=?', (rec['model'],)).fetchone()[0]
                self.db.execute(f'''INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(file,identity) DO UPDATE SET {MAXIMA}''',
                    (fid, digest(rec['identity']), seq, digest(str(rec['session_id'])),
                     paris_day(rec['timestamp']), model, *(rec['usage'][k] for k in COUNTERS)))
            resolved = resolve(meta['first_user_text']) if label == '_p_crons' else label
            self.db.execute('UPDATE files SET mtime=?,size=?,version=?,label=?,ordering=? WHERE id=?',
                            (*fingerprint, VERSION, resolved, self.order, fid))
            self.db.execute('''INSERT INTO file_daily SELECT file,day,model,
                sum(i),sum(o),sum(r),sum(c),sum(t) FROM records
                WHERE file=? AND day IS NOT NULL GROUP BY day,model''', (fid,))
            if not old:
                self.db.execute('INSERT INTO seen VALUES (?)', (fid,))
            self.db.execute('RELEASE source')
            self.changed = True
        except OSError:
            self.db.execute('ROLLBACK TO source')
            self.db.execute('RELEASE source')
            raise

    def finish(self, prune=True):
        # Incomplete discovery retains inaccessible sources, but confirmed
        # deleted files must still be removed (one bad directory cannot pin
        # an ever-growing history of unrelated deleted transcripts).
        if prune:
            stale = self.db.execute('SELECT 1 FROM files WHERE id NOT IN (SELECT id FROM seen) LIMIT 1').fetchone()
            if stale:
                self.db.execute('DELETE FROM files WHERE id NOT IN (SELECT id FROM seen)')
                self.changed = True
        else:
            for fid, path in self.db.execute('SELECT id,path FROM files WHERE id NOT IN (SELECT id FROM seen)'):
                try:
                    Path(path).lstat()
                except FileNotFoundError:
                    self.db.execute('DELETE FROM files WHERE id=?', (fid,))
                    self.changed = True
                except OSError:
                    pass
        if self.changed:
            self.db.execute('''CREATE TEMP TABLE merged (
                identity BLOB PRIMARY KEY, session BLOB, day INTEGER, model INTEGER,
                label TEXT, i INTEGER, o INTEGER, r INTEGER, c INTEGER, t INTEGER
                ) WITHOUT ROWID''')
            # First discovered copy owns day/model/agent/session; cumulative
            # streaming usage takes maxima across all files, including other days.
            self.db.execute(f'''INSERT INTO merged
                SELECT r.identity,r.session,r.day,r.model,f.label,r.i,r.o,r.r,r.c,r.t
                FROM records r JOIN files f ON r.file=f.id WHERE true
                ORDER BY f.ordering,r.seq
                ON CONFLICT(identity) DO UPDATE SET {MAXIMA}''')
            self.db.execute('DELETE FROM daily')
            self.db.execute('DELETE FROM runs')
            self.db.execute('''INSERT INTO daily SELECT day,label,model,
                sum(i),sum(o),sum(r),sum(c),sum(t) FROM merged
                WHERE day IS NOT NULL GROUP BY day,label,model''')
            self.db.execute('''INSERT INTO runs SELECT DISTINCT day,label,session
                FROM merged WHERE day IS NOT NULL''')
            self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                            ('invalid_timestamp', bool(self.db.execute('SELECT 1 FROM merged WHERE day IS NULL LIMIT 1').fetchone())))
            self.db.execute('DELETE FROM models WHERE id NOT IN (SELECT DISTINCT model FROM records)')
        self.db.commit()
        # Pin a read snapshot so concurrent refreshes cannot mix aggregates/runs.
        self.db.execute('BEGIN')

    def invalid_timestamp(self):
        return bool(self.db.execute("SELECT value FROM metadata WHERE key='invalid_timestamp'").fetchone() == (1,))

    def rows(self, start, end, day):
        for date, label, model, *usage in self.db.execute('''SELECT d.day,d.label,m.name,d.i,d.o,d.r,d.c,d.t
            FROM daily d JOIN models m ON d.model=m.id
            WHERE (day BETWEEN ? AND ?) OR day=? ORDER BY d.day,d.label,m.name''', (start, end, day)):
            yield date, label, model, dict(zip(COUNTERS, usage))

    def run_counts(self, start, end):
        # Stream distinct sessions from a covering index. COUNT(DISTINCT) with
        # a day-first index otherwise creates a transcript-sized temporary set.
        labels = self.db.execute('SELECT DISTINCT label FROM daily WHERE day BETWEEN ? AND ?', (start, end))
        return {label: self.db.execute('''SELECT count(*) FROM (
            SELECT session FROM runs INDEXED BY runs_by_session
            WHERE label=? AND day BETWEEN ? AND ? GROUP BY session)''',
            (label, start, end)).fetchone()[0] for label, in labels}
