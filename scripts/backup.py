"""Create a consistent SQLite backup; keep the latest 14 daily snapshots."""
import datetime as dt
import os
import sqlite3
from pathlib import Path

root = Path('/opt/job-board')
directory = root / 'backups'
directory.mkdir(mode=0o700, exist_ok=True)
os.umask(0o077)
stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
target = directory / f'jobs-{stamp}.sqlite3'
with sqlite3.connect(f'file:{root}/data/jobs.sqlite3?mode=ro', uri=True) as source:
    with sqlite3.connect(target) as destination:
        source.backup(destination)
        assert destination.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
for old in sorted(directory.glob('jobs-*.sqlite3'), reverse=True)[14:]:
    old.unlink()
print(f'Backup verified: {target.name}')
