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
SCHEMA_VERSION = 1  # Bump for table/index changes; incompatible databases rebuild.
BUSY_TIMEOUT_SECONDS = 0.1
COUNTERS = ('input', 'output', 'cache_read', 'cache_create', 'reasoning')
USAGE_COLUMNS = 'i,o,r,c,t'
MAXIMA = ','.join(f'{k}=max({k},excluded.{k})' for k in USAGE_COLUMNS.split(','))


def digest(value: str) -> bytes:
    return hashlib.blake2b(value.encode(), digest_size=16).digest()


class UsageCache:
    def __init__(self, path: Path | None):
        if path is not None:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.parent.chmod(0o700)
            if path.is_symlink():
                raise ValueError('usage cache must not be a symlink')
        # Empty filename gives a private, automatically deleted disk cache.
        self.db = sqlite3.connect(str(path) if path is not None else '',
                                  timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        self.changed = False
        self.order = 0
        try:
            self._open()
        except BaseException:
            self.db.close()
            raise

    def _open(self):
        # Fixed 8 MiB page cache; sorting/dedupe spill to disk, not Python RAM.
        self.db.execute('PRAGMA cache_size=-8192')
        self.db.execute('PRAGMA temp_store=FILE')
        self.db.execute('PRAGMA temp.cache_size=-2048')
        self.db.execute('PRAGMA main.cache_spill=2048')
        self.db.execute('PRAGMA temp.cache_spill=512')
        self.db.execute('PRAGMA mmap_size=0')
        self.db.execute('PRAGMA foreign_keys=ON')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
        if exists and version != SCHEMA_VERSION:
            raise sqlite3.DatabaseError('usage cache schema version mismatch')
        if not exists:
            self.db.execute('PRAGMA auto_vacuum=FULL')
            self.db.executescript(f'''BEGIN IMMEDIATE;
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
                INSERT OR IGNORE INTO metadata VALUES ('generation',0);
                PRAGMA user_version={SCHEMA_VERSION};
                COMMIT;
            ''')
        self.db.executescript('''
            CREATE TEMP TABLE seen (id INTEGER PRIMARY KEY);
            CREATE TEMP TABLE staged_files AS SELECT *,0 AS parsed FROM files WHERE false;
            CREATE TEMP TABLE staged_models (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
            CREATE TEMP TABLE staged_records (
                file INTEGER, identity BLOB, seq INTEGER, session BLOB, day INTEGER,
                model INTEGER, i INTEGER, o INTEGER, r INTEGER, c INTEGER, t INTEGER,
                PRIMARY KEY(file,identity)) WITHOUT ROWID;
        ''')
        # Read a consistent source index, but never reserve the persistent DB
        # for writing while walking/statting/parsing transcripts.
        self.db.execute('BEGIN')
        self.generation = self.db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()
        if self.generation is None:
            raise sqlite3.DatabaseError('usage cache missing generation metadata')
        self.db.execute('INSERT INTO staged_models SELECT * FROM models')
        self.next_file = self.db.execute('SELECT coalesce(max(id),0)+1 FROM files').fetchone()[0]

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
                    self.db.execute('INSERT INTO staged_files VALUES (?,?,?,?,?,?,?,0)',
                                    (old[0], str(path), *fingerprint, VERSION,
                                     old[4] if label == '_p_crons' else label, self.order))
                    self.changed = True
                return
        self.db.execute('SAVEPOINT source')
        try:
            fid = old[0] if old else self.next_file
            for seq, rec in enumerate(stream()):
                self.db.execute('INSERT OR IGNORE INTO staged_models(name) VALUES (?)', (rec['model'],))
                model = self.db.execute('SELECT id FROM staged_models WHERE name=?', (rec['model'],)).fetchone()[0]
                self.db.execute(f'''INSERT INTO staged_records VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(file,identity) DO UPDATE SET {MAXIMA}''',
                    (fid, digest(rec['identity']), seq, digest(str(rec['session_id'])),
                     paris_day(rec['timestamp']), model, *(rec['usage'][k] for k in COUNTERS)))
            resolved = resolve(meta['first_user_text']) if label == '_p_crons' else label
            self.db.execute('INSERT INTO staged_files VALUES (?,?,?,?,?,?,?,1)',
                            (fid, str(path), *fingerprint, VERSION, resolved, self.order))
            if not old:
                self.db.execute('INSERT INTO seen VALUES (?)', (fid,))
                self.next_file += 1
            self.db.execute('RELEASE source')
            self.changed = True
        except BaseException:
            self.db.execute('ROLLBACK TO source')
            self.db.execute('RELEASE source')
            raise

    def finish(self, prune=True):
        # Incomplete discovery retains inaccessible sources, but confirmed
        # deleted files must still be removed (one bad directory cannot pin
        # an ever-growing history of unrelated deleted transcripts).
        if not prune:
            for fid, path in self.db.execute('SELECT id,path FROM files WHERE id NOT IN (SELECT id FROM seen)'):
                try:
                    Path(path).lstat()
                except FileNotFoundError:
                    continue
                except OSError:
                    pass
                self.db.execute('INSERT INTO seen VALUES (?)', (fid,))
        stale = self.db.execute('SELECT 1 FROM files WHERE id NOT IN (SELECT id FROM seen) LIMIT 1').fetchone()
        if not self.changed and not stale:
            return  # Keep the read snapshot for aggregates; no persistent writes.
        self.db.commit()
        self.db.execute('BEGIN IMMEDIATE')
        if self.db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone() != self.generation:
            # A different process published while we scanned. Never combine its
            # files with our old fingerprints/model IDs: rebuild privately.
            raise sqlite3.OperationalError('database is locked: cache changed during scan')
        self.db.execute('DELETE FROM records WHERE file IN (SELECT id FROM staged_files WHERE parsed=1)')
        self.db.execute('DELETE FROM file_daily WHERE file IN (SELECT id FROM staged_files WHERE parsed=1)')
        self.db.execute('''INSERT INTO files SELECT id,path,mtime,size,version,label,ordering
            FROM staged_files WHERE true ON CONFLICT(id) DO UPDATE SET
            mtime=excluded.mtime,size=excluded.size,version=excluded.version,
            label=excluded.label,ordering=excluded.ordering''')
        self.db.execute('INSERT OR IGNORE INTO models SELECT * FROM staged_models')
        self.db.execute('INSERT INTO records SELECT * FROM staged_records')
        self.db.execute('''INSERT INTO file_daily SELECT file,day,model,
            sum(i),sum(o),sum(r),sum(c),sum(t) FROM staged_records
            WHERE day IS NOT NULL GROUP BY file,day,model''')
        self.db.execute('DELETE FROM files WHERE id NOT IN (SELECT id FROM seen)')
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
        self.db.execute("UPDATE metadata SET value=value+1 WHERE key='generation'")
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
