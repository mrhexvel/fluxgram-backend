import os
import stat
from pathlib import Path


class SecureFileError(RuntimeError):
    pass


def read_bounded_regular_file(
    path: Path,
    max_bytes: int,
    allowed_modes: frozenset[int],
    allowed_uids: frozenset[int],
) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise SecureFileError("Secure file is unavailable") from error

    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in allowed_uids
            or stat.S_IMODE(before.st_mode) not in allowed_modes
            or before.st_size > max_bytes
        ):
            raise SecureFileError("Secure file metadata is invalid")

        encoded = bytearray()
        while len(encoded) <= max_bytes:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(encoded)))
            if not chunk:
                break
            encoded.extend(chunk)
        if len(encoded) > max_bytes:
            raise SecureFileError("Secure file exceeds size limit")

        after = os.fstat(descriptor)
        if _identity(before) != _identity(after):
            raise SecureFileError("Secure file changed while reading")
    except OSError as error:
        raise SecureFileError("Secure file cannot be read") from error
    finally:
        os.close(descriptor)

    return bytes(encoded)


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_uid,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
