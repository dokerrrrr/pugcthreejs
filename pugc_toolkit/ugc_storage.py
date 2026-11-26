"""Atomic single-file writes and recoverable publication of related UGC outputs."""
from contextlib import ExitStack, contextmanager
from filecmp import cmp
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from uuid import uuid4

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class PublicationError(ValueError):
    pass


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _identity(path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


def output_paths(outputs, *, inputs=()):
    paths = tuple(Path(path).resolve() for path in outputs)
    used = {Path(path).resolve() for path in inputs if path is not None}
    identities = {_identity(path) for path in used} - {None}
    for path in paths:
        if re.fullmatch(r"\.ugc-(?:lock-[0-9a-f]{32}|transaction-[0-9a-f]{32}\.(?:json|next)"
                        r"|[0-9a-f]{32}-\d+\.(?:new|old))",
                        path.name):
            raise PublicationError(f"Path is reserved for publication data: {path}")
        identity = _identity(path)
        if path in used or identity in identities:
            raise PublicationError(f"Conflicting input or output path: {path}")
        if identity is not None and not path.is_file():
            raise PublicationError(f"Output is not a regular file: {path}")
        if not path.parent.is_dir():
            raise PublicationError(f"Output directory does not exist: {path.parent}")
        used.add(path)
        if identity is not None:
            identities.add(identity)
    return paths


@contextmanager
def lock_outputs(paths):
    with ExitStack() as stack:
        for path in sorted(set(paths)):
            lock_name = ".ugc-lock-" + sha256(os.fsencode(path.name)).hexdigest()[:32]
            stream = stack.enter_context(path.with_name(lock_name).open("a+b"))
            try:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise PublicationError(f"Another command is writing {path}; retry when it finishes") from exc
        yield


def _sync_directory(path):
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_bytes(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _backup(source, destination):
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer)
        writer.flush()
        os.fsync(writer.fileno())
    shutil.copymode(source, destination)


def atomic_write(path, data):
    path = Path(path).resolve()
    with TemporaryDirectory(prefix=".ugc-write-", dir=path.parent) as directory:
        staged = Path(directory) / path.name
        _write_bytes(staged, data)
        install_staged(staged, path)


def install_staged(staged, path):
    """Replace an output with a verified, synced file on the same filesystem."""
    if path.is_file():
        shutil.copymode(path, staged)
    staged.replace(path)
    _sync_directory(path.parent)


def _journal(binary):
    binary = Path(binary).resolve()
    key = sha256(os.fsencode(binary.name)).hexdigest()[:32]
    return binary.with_name(f".ugc-transaction-{key}.json")


def _write_journal(journal, state):
    staged = journal.with_suffix(".next")
    staged.unlink(missing_ok=True)
    _write_bytes(staged, json_bytes(state))
    staged.replace(journal)
    _sync_directory(journal.parent)


def require_no_transaction(binary):
    if _journal(binary).exists():
        raise PublicationError(f"Incomplete publication for {binary}; run recover {binary} with the original --raw-json option")


def _transaction_paths(paths, token):
    return [(path.with_name(f".ugc-{token}-{index}.new"), path.with_name(f".ugc-{token}-{index}.old"))
            for index, path in enumerate(paths)]


def recover_outputs(paths):
    """Recover under the same locks used for publication. Return the recorded phase."""
    journal = _journal(paths[0])
    if not journal.exists():
        journal.with_suffix(".next").unlink(missing_ok=True)
        return None
    state = json.loads(journal.read_bytes())
    if (not isinstance(state, dict) or state.get("schema") != 1
            or not re.fullmatch(r"[0-9a-f]{32}", str(state.get("token")))
            or state.get("phase") not in ("preparing", "publishing", "committed", "restored")
            or state.get("paths") != [str(path) for path in paths]
            or not isinstance(state.get("existed"), list) or len(state["existed"]) != len(paths)
            or any(type(existed) is not bool for existed in state["existed"])
            or not isinstance(state.get("new_sha256"), list) or len(state["new_sha256"]) != len(paths)
            or any(not re.fullmatch(r"[0-9a-f]{64}", str(digest)) for digest in state["new_sha256"])):
        raise PublicationError(f"Invalid publication journal or different output paths: {journal}")
    phase = state["phase"]
    temporary = _transaction_paths(paths, state["token"])
    if phase == "publishing":
        restore_targets = []
        for path, (new, old), existed, digest in zip(paths, temporary, state["existed"], state["new_sha256"]):
            if path.is_file():
                if existed and cmp(path, old, shallow=False):
                    continue
                with path.open("rb") as stream:
                    if file_digest(stream, "sha256").hexdigest() != digest:
                        raise PublicationError(f"Output changed since the interrupted build: {path}")
            restore_targets.append((path, new, old, existed))
        # Keep every backup until the entire rollback is recorded, so recovery can be retried.
        for path, new, old, existed in restore_targets:
            if existed:
                new.unlink(missing_ok=True)
                _backup(old, new)
                new.replace(path)
            else:
                path.unlink(missing_ok=True)
            _sync_directory(path.parent)
        state["phase"] = "restored"
        _write_journal(journal, state)
    for new, old in temporary:
        new.unlink(missing_ok=True)
        old.unlink(missing_ok=True)
        _sync_directory(new.parent)
    journal.unlink()
    journal.with_suffix(".next").unlink(missing_ok=True)
    _sync_directory(journal.parent)
    return phase


def publish_outputs(paths, contents, verify):
    """Stage and verify all outputs, then publish under the caller's locks."""
    require_no_transaction(paths[0])
    journal = _journal(paths[0])
    state = {"schema": 1, "token": uuid4().hex, "phase": "preparing",
             "paths": [str(path) for path in paths], "existed": [path.exists() for path in paths],
             "new_sha256": [sha256(data).hexdigest() for data in contents]}
    temporary = _transaction_paths(paths, state["token"])
    _write_journal(journal, state)
    try:
        for path, data, (new, old), existed in zip(paths, contents, temporary, state["existed"], strict=True):
            _write_bytes(new, data)
            if existed:
                _backup(path, old)
                shutil.copymode(path, new)
            _sync_directory(path.parent)
        verify(tuple(new for new, _ in temporary))
        state["phase"] = "publishing"
        _write_journal(journal, state)
        for path, (new, _) in zip(paths, temporary):
            new.replace(path)
            _sync_directory(path.parent)
        state["phase"] = "committed"
        _write_journal(journal, state)
    except Exception:
        recover_outputs(paths)
        raise
    recover_outputs(paths)
