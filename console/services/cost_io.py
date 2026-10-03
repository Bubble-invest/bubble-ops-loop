"""Stdlib atomic JSON publication without following destination symlinks."""
import json
import os
import secrets
import stat
from pathlib import Path


def atomic_json(path: Path, value: dict) -> None:
    path = Path(os.path.abspath(path))
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    temporary = None
    try:
        for part in path.parent.parts[1:]:
            if part in ('.', '..'):
                raise ValueError('unsafe destination')
            created = False
            try:
                os.mkdir(part, mode=0o755, dir_fd=directory)
                created = True
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            if created:
                os.fchmod(child, 0o755)
            os.close(directory)
            directory = child
        try:
            existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(existing.st_mode):
                raise ValueError('destination must be a regular file')
        except FileNotFoundError:
            pass
        temporary = f'.{path.name}.{secrets.token_hex(8)}.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o644, dir_fd=directory)
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o644)
            json.dump(value, stream, separators=(',', ':'), allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)


def public_directory(path: Path) -> None:
    """Make an existing summary directory traversable, using no-follow FDs."""
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(os.path.abspath(path)).parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        os.fchmod(directory, 0o755)
    finally:
        os.close(directory)
