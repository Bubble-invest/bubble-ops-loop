"""Read regular files through pinned, non-symlink path components."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat


@contextmanager
def open_text(path):
    path = Path(os.path.abspath(path))
    descriptors = []
    try:
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        for part in path.parts[1:-1]:
            info = os.lstat(part, dir_fd=fd)
            if not stat.S_ISDIR(info.st_mode):
                raise OSError('refusing non-directory path component')
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        info = os.lstat(path.name, dir_fd=fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError('refusing nonregular source')
        handle = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(handle, 'r', encoding='utf-8') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError('refusing nonregular source')
            yield stream
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
