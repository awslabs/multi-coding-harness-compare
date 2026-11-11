# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Verified S3 archives, temporary hydration, and crash-recoverable local eviction.

An uncompressed streaming tar keeps memory bounded and permits range reads of
individual files. Multipart checksums are verified by S3 and checked again before
committing the manifest. A committed manifest is required before local deletion.
The SQLite index, locks, and authentication material are application runtime state.
"""
import base64
import fcntl
import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import tempfile
import uuid
from contextlib import contextmanager, closing
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .bridge import atomic_json
from .isolation import remove_tree  # noqa: F401 — re-exported for backward compatibility
from .store import now
from .session_storage import is_session_database, split_database, restore_runtime


class StorageError(RuntimeError):
    pass


def digest64(value):
    return base64.b64encode(hashlib.sha256(value).digest()).decode()


def safe_name(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or str(path) != value:
        raise StorageError("Unsafe artifact path")
    return path


def identity(path):
    s = path.lstat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_mode]


class HashReader:
    def __init__(self, file):
        self.file, self.hash = file, hashlib.sha256()

    def read(self, size=-1):
        value = self.file.read(size)
        self.hash.update(value)
        return value


class MultipartWriter:
    """One buffered part, no local tar copy, and durable multipart ownership."""
    part_size = 32 * 1024 * 1024

    def __init__(self, client, bucket, key, save_upload, progress=None):
        self.client, self.bucket, self.key = client, bucket, key
        self.buffer, self.parts, self.digests = bytearray(), [], []
        self.hash, self.size = hashlib.sha256(), 0
        self.progress, self.uploaded_bytes = progress, 0
        result = client.create_multipart_upload(
            Bucket=bucket, Key=key, ChecksumAlgorithm="SHA256",
            ContentType="application/x-tar")
        self.upload_id = result["UploadId"]
        try:
            save_upload(self.upload_id)
        except BaseException:
            self.abort()
            raise

    def write(self, value):
        self.hash.update(value)
        self.size += len(value)
        self.buffer.extend(value)
        while len(self.buffer) >= self.part_size:
            self.flush_part(self.part_size)
        return len(value)

    def flush_part(self, size):
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        checksum = digest64(value)
        number = len(self.parts) + 1
        result = self.client.upload_part(
            Bucket=self.bucket, Key=self.key, UploadId=self.upload_id,
            PartNumber=number, Body=value, ChecksumSHA256=checksum)
        if result.get("ChecksumSHA256") != checksum:
            raise StorageError("S3 did not confirm the uploaded part checksum")
        self.parts.append({"PartNumber": number, "ETag": result["ETag"],
                           "ChecksumSHA256": checksum})
        self.digests.append(base64.b64decode(checksum))
        self.uploaded_bytes += len(value)
        if self.progress:
            self.progress(self.uploaded_bytes, len(self.parts))

    def finish(self):
        if self.buffer:
            self.flush_part(len(self.buffer))
        self.client.complete_multipart_upload(
            Bucket=self.bucket, Key=self.key, UploadId=self.upload_id,
            MultipartUpload={"Parts": self.parts})
        checksum = digest64(b"".join(self.digests)) + "-" + str(len(self.parts))
        head = self.client.head_object(Bucket=self.bucket, Key=self.key, ChecksumMode="ENABLED")
        if head.get("ChecksumSHA256") != checksum or head["ContentLength"] != self.size:
            raise StorageError("Uploaded archive failed S3 checksum/length verification")
        return {"key": self.key, "bytes": self.size, "sha256": self.hash.hexdigest(),
                "checksum": checksum, "version_id": head.get("VersionId")}

    def abort(self):
        self.client.abort_multipart_upload(
            Bucket=self.bucket, Key=self.key, UploadId=self.upload_id)


class ArtifactStorage:
    def __init__(self, config, store, client=None):
        self.config, self.store, self._client = config, store, client
        target = urlsplit(config.artifact_s3_uri or "")
        self.bucket, self.prefix = target.netloc, target.path.strip("/")
        self.enabled = bool(config.artifact_s3_uri)
        self.control = config.root / "artifact-control"

    @property
    def client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config
            self._client = boto3.Session(
                profile_name=self.config.artifact_s3_profile,
                region_name=self.config.artifact_s3_region).client(
                    "s3", config=Config(connect_timeout=10, read_timeout=90,
                                       retries={"mode": "standard", "total_max_attempts": 4}))
        return self._client

    def root(self, id):
        safe_name(id)
        if "/" in id:
            raise StorageError("Invalid comparison ID")
        return self.config.root / "comparisons" / id

    def state(self, id):
        try:
            return self.store.get("artifact_archive", id)
        except KeyError:
            return {"id": id, "status": "local"}

    @contextmanager
    def lock(self, id, *, blocking=True, shared=False):
        safe_name(id)
        if "/" in id:
            raise StorageError("Invalid archive ID")
        self.control.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.control / (id + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a+") as handle:
            fcntl.flock(handle, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
                        | (0 if blocking else fcntl.LOCK_NB))
            yield

    def _save(self, id, **fields):
        data = {**self.state(id), **fields, "id": id, "updated_at": now()}
        self.store.put("artifact_archive", data)
        return data

    def _object_args(self, state, key, version=None):
        # Old archives remain readable after the configured destination changes.
        args = {"Bucket": state["bucket"], "Key": key}
        if version and version != "null":
            args["VersionId"] = version
        return args

    def manifest(self, id):
        state = self.state(id)
        if not state.get("manifest_key"):
            raise StorageError("No committed S3 archive is available")
        result = self.client.get_object(**self._object_args(
            state, state["manifest_key"], state.get("manifest_version")))
        with closing(result["Body"]) as body:
            raw = body.read()
        if hashlib.sha256(raw).hexdigest() != state["manifest_sha256"]:
            raise StorageError("S3 artifact manifest checksum mismatch")
        value = json.loads(raw)
        if value["id"] != id or value["format"] != 1:
            raise StorageError("Wrong artifact manifest")
        return value

    def _walk(self, root):
        if root.is_file():
            yield root, root.name
            return
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in sorted(dirs + files):
                path = Path(directory) / name
                yield path, path.relative_to(root).as_posix()

    def _private(self, name):
        # Credentials remain application runtime state. Kiro databases receive
        # special handling to archive conversations without authentication data.
        path = PurePosixPath(name)
        parts = set(path.parts)
        return (bool(parts & {".aws", ".midway", ".ssh", ".toolbox", "credential-templates"})
                or path.name in {"auth.json", ".credentials.json", "credentials",
                                 ".claude.json", ".env", ".env.local", ".npmrc", ".netrc"}
                or ("home" in parts and path.name in {"settings.json", "config.toml"})
                or any(path.name == PurePosixPath(p).name for h in self.config.harnesses.values()
                       for p in h.refresh_files))

    def _runtime_source(self, id, name):
        source = self.control / "runtime-vault" / id / name
        if source.exists() or source.is_symlink():
            return source
        parts = PurePosixPath(name).parts
        if len(parts) > 4 and parts[0] == "runs" and parts[2] == "home":
            try:
                run = self.store.get("run", parts[1])
                template = self.config.harnesses[run["harness"]].home_template
            except KeyError:
                return None
            if template:
                source = Path(template).expanduser().joinpath(*parts[3:])
                # Native macOS runs can originate from a Linux Kiro template.
                if not source.exists() and parts[3:6] == ("Library", "Application Support", "kiro-cli"):
                    source = Path(template).expanduser().joinpath(".local/share/kiro-cli", *parts[6:])
                if source.exists():
                    return source
        return None

    def _vault(self, id, path, name):
        target = self.control / "runtime-vault" / id / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.is_symlink() or target.is_file():
            target.unlink()
        if path.is_symlink():
            target.symlink_to(os.readlink(path))
        else:
            shutil.copyfile(path, target)
            target.chmod(0o600)

    def archive(self, id, root=None):
        """Caller holds the exclusive comparison lease and has stopped writers."""
        if not self.enabled:
            return
        # A killed SQLite sanitization may leave a disposable copy. The original
        # run or its committed archive remains authoritative.
        for stale in self.control.glob(f"session-export-{id}-*"):
            remove_tree(stale)
        root = root or self.root(id)
        if root.is_symlink():
            raise StorageError("Artifact root must not be a symlink")
        state = self.state(id)
        staging = root.parent / (".restore-" + id)
        if state["status"] in ("restoring", "restore_failed"):
            # Hydration is disposable until promoted. A killed download must
            # never replace the complete remote generation with a partial tree.
            self._verify_remote(state)
            remove_tree(staging)
            remove_tree(root)
            return self._save(id, status="archived", local_present=False, error=None)
        # Cleanup may have crashed after the remote commit. Never upload a
        # partially deleted tree over its complete previous archive.
        if state["status"] == "evicting":
            self._verify_remote(state)
            if not root.exists() or not state.get("root_identity") or identity(root)[:2] == state["root_identity"]:
                remove_tree(root)
                remove_tree(staging)
                return self._save(id, status="archived", local_present=False, error=None)
            # An external artifact producer recreated this pathname after the old
            # tree was evicted. Archive that new inode instead of deleting it.
            state = self._save(id, status="local", local_present=True)
        if not root.exists():
            if state.get("manifest_key"):
                self._verify_remote(state)
                return self._save(id, status="archived", local_present=False, error=None)
            raise StorageError("Local artifacts and committed S3 archive are both missing")
        if state.get("upload_id"):
            try:
                self.client.abort_multipart_upload(
                    Bucket=state.get("upload_bucket", state.get("bucket")),
                    Key=state["upload_key"], UploadId=state["upload_id"])
            except Exception as exc:
                if getattr(exc, "response", {}).get("Error", {}).get("Code") != "NoSuchUpload":
                    raise
        generation = uuid.uuid4().hex
        category = state.get("category", "comparisons")
        key = f"{self.prefix}/{category}/{id}/{generation}/artifacts.tar"
        root_identity = identity(root)[:2]
        self._save(id, status="uploading", local_present=True, error=None,
                   uploaded_bytes=0, uploaded_parts=0, transfer_phase="uploading")
        writer = None
        try:
            writer = MultipartWriter(self.client, self.bucket, key,
                lambda upload_id: self._save(id, upload_id=upload_id, upload_key=key,
                                            upload_bucket=self.bucket),
                lambda size, parts: self._save(id, uploaded_bytes=size, uploaded_parts=parts))
            entries, fingerprints = [], {}
            with tempfile.TemporaryDirectory(prefix=f"session-export-{id}-", dir=self.control) as temporary, \
                    tarfile.open(fileobj=writer, mode="w|", format=tarfile.PAX_FORMAT,
                              dereference=False) as archive:
                for path, name in self._walk(root):
                    before = identity(path)
                    fingerprints[name] = before
                    mode = before[-1]
                    if stat.S_ISDIR(mode):
                        info = archive.gettarinfo(str(path), arcname=name)
                    elif path.is_symlink():
                        target = os.readlink(path)
                        resolved = (path.parent / target).resolve()
                        if not resolved.is_relative_to(root.resolve()):
                            self._vault(id, path, name)
                            entries.append({"name": name, "type": "runtime", "mode": mode & 0o777})
                            continue
                        # Normalize internal links so archives never restore an
                        # absolute link or a link outside the hydrated root.
                        info = archive.gettarinfo(str(path), arcname=name)
                        info.linkname = os.path.relpath(resolved, path.parent)
                    elif stat.S_ISREG(mode):
                        if ("kiro-cli" in PurePosixPath(name).parts
                                and path.name.endswith((".sqlite3-wal", ".sqlite3-shm", ".db-wal", ".db-shm"))):
                            entries.append({"name": name, "type": "ephemeral"})
                            continue
                        source = path
                        session_database = is_session_database(name)
                        if session_database:
                            source = Path(temporary) / "sessions.sqlite3"
                            split_database(path, source, self.control / "runtime-vault" / id / name)
                        if not session_database and self._private(name):
                            self._vault(id, path, name)
                            entries.append({"name": name, "type": "runtime", "mode": mode & 0o777})
                            continue
                        info = archive.gettarinfo(str(source), arcname=name)
                        # Archive hardlinks as independent files for range reads.
                        info.type, info.linkname, info.size = tarfile.REGTYPE, "", source.stat().st_size
                    else:
                        # Socket/FIFO contents are not durable artifacts.
                        entries.append({"name": name, "type": "ephemeral"})
                        continue
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    offset = archive.offset + len(info.tobuf(format=tarfile.PAX_FORMAT))
                    entry = {"name": name, "mode": info.mode & 0o777,
                             "type": "file" if info.isfile() else "dir" if info.isdir() else "link",
                             "size": info.size, "offset": offset, "link": info.linkname}
                    if info.isfile():
                        if session_database:
                            entry["session_database"] = True
                        with source.open("rb") as file:
                            reader = HashReader(file)
                            archive.addfile(info, reader)
                            entry["sha256"] = reader.hash.hexdigest()
                    else:
                        archive.addfile(info)
                    if identity(path) != before:
                        raise StorageError("Artifact changed during upload: " + name)
                    entries.append(entry)
            self._save(id, transfer_phase="verifying")
            remote = writer.finish()
            # Detect additions/removals/edits before committing local eviction.
            if fingerprints != {name: identity(p) for p, name in self._walk(root)}:
                raise StorageError("Artifacts changed during upload; local files retained")
            manifest = {"format": 1, "id": id, "created_at": now(), "archive": remote,
                        "source_path": str(root), "entries": entries}
            raw = json.dumps(manifest, separators=(",", ":")).encode()
            manifest_key = key.rsplit("/", 1)[0] + "/manifest.json"
            result = self.client.put_object(Bucket=self.bucket, Key=manifest_key, Body=raw,
                ContentType="application/json", ChecksumSHA256=digest64(raw))
            if result.get("ChecksumSHA256") != digest64(raw):
                raise StorageError("S3 did not confirm the manifest checksum")
            # Record the commit BEFORE deletion. Recovery finishes deletion from
            # this state; it never treats the remaining subset as a new archive.
            commit = dict(archive=remote, bucket=self.bucket, category=category, root_identity=root_identity,
                manifest_key=manifest_key, manifest_sha256=hashlib.sha256(raw).hexdigest(),
                manifest_version=result.get("VersionId"), upload_id=None,
                uri=f"s3://{self.bucket}/{manifest_key}", archived_at=now())
            pointer = json.dumps({"id": id, **commit}, separators=(",", ":")).encode()
            pointer_result = self.client.put_object(
                Bucket=self.bucket, Key=f"{self.prefix}/{category}/{id}/latest.json",
                Body=pointer, ContentType="application/json", ChecksumSHA256=digest64(pointer))
            if pointer_result.get("ChecksumSHA256") != digest64(pointer):
                raise StorageError("S3 did not confirm the archive index checksum")
            if root_identity != identity(root)[:2] or fingerprints != {
                    name: identity(p) for p, name in self._walk(root)}:
                raise StorageError("Artifacts changed before eviction; local files retained")
            state = self._save(id, status="evicting", transfer_phase="cleanup", **commit)
            remove_tree(root)
            return self._save(id, status="archived", local_present=False, error=None)
        except BaseException as exc:
            # A failure after the commit must keep the evicting recovery state.
            status = "evicting" if self.state(id)["status"] == "evicting" else "upload_failed"
            self._save(id, status=status, error=str(exc), local_present=root.exists())
            raise

    def _verify_remote(self, state):
        self.manifest(state["id"])
        remote = state["archive"]
        head = self.client.head_object(**self._object_args(state, remote["key"], remote.get("version_id")),
                                       ChecksumMode="ENABLED")
        if head["ContentLength"] != remote["bytes"] or head.get("ChecksumSHA256") != remote["checksum"]:
            raise StorageError("Committed archive failed checksum/length verification")

    def restore(self, id, root=None):
        """Stage, validate, then rename. A failed download is never used by a job."""
        if not self.enabled:
            return
        root, state = root or self.root(id), self.state(id)
        if state["status"] == "evicting":
            self.archive(id, root)
            state = self.state(id)
        if root.exists() and state["status"] != "restoring":
            return
        if not state.get("manifest_key"):
            raise StorageError("No saved artifacts to restore")
        manifest = self.manifest(id)
        self._save(id, status="restoring", error=None)
        staging = root.parent / (".restore-" + id)
        remove_tree(staging)
        staging.mkdir(parents=True, mode=0o700)
        entries = {e["name"]: e for e in manifest["entries"]}
        remote = manifest["archive"]
        result = self.client.get_object(**self._object_args(state, remote["key"], remote.get("version_id")))
        try:
            with closing(result["Body"]) as body:
                reader = HashReader(body)
                seen, links, directories = set(), [], []
                with tarfile.open(fileobj=reader, mode="r|") as archive:
                    for member in archive:
                        safe_name(member.name)
                        entry = entries.get(member.name)
                        if not entry or member.name in seen:
                            raise StorageError("Unexpected or duplicate archive entry")
                        seen.add(member.name)
                        destination = staging / member.name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if member.isdir() and entry["type"] == "dir":
                            destination.mkdir(exist_ok=True)
                            directories.append((destination, entry["mode"]))
                        elif member.isfile() and entry["type"] == "file" and member.size == entry["size"]:
                            with archive.extractfile(member) as file, destination.open("xb") as output:
                                checked = HashReader(file)
                                shutil.copyfileobj(checked, output)
                            if checked.hash.hexdigest() != entry["sha256"]:
                                raise StorageError("Restored file checksum mismatch: " + member.name)
                            destination.chmod(entry["mode"] & 0o777)
                        elif member.issym() and entry["type"] == "link" and member.linkname == entry["link"]:
                            links.append((destination, member.linkname))
                        else:
                            raise StorageError("Unsupported or mismatched archive entry")
                # tarfile stops at the end marker, before all trailing padding.
                while reader.read(1024 * 1024):
                    pass
                if reader.hash.hexdigest() != remote["sha256"]:
                    raise StorageError("Restored archive checksum mismatch")
                if seen != {e["name"] for e in entries.values() if e["type"] in ("dir", "file", "link")}:
                    raise StorageError("Archive is missing files")
                # Links are installed only after file extraction, preventing
                # writes through archive-supplied symlinks.
                for destination, target in links:
                    if Path(target).is_absolute() or not (destination.parent / target).resolve().is_relative_to(staging.resolve()):
                        raise StorageError("Archive symlink escapes the temporary directory")
                    destination.symlink_to(target)
                for entry in entries.values():
                    if entry.get("session_database"):
                        source = self._runtime_source(id, entry["name"])
                        if source:
                            restore_runtime(staging / entry["name"], source)
                    if entry["type"] != "runtime":
                        continue
                    safe_name(entry["name"])
                    source = self._runtime_source(id, entry["name"])
                    destination = staging / entry["name"]
                    if source is None:
                        # Evaluation needs no credentials. Native startup applies
                        # the configured host setup; absent login is an auth gap.
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if source.is_symlink():
                        destination.symlink_to(os.readlink(source))
                    else:
                        shutil.copyfile(source, destination)
                        destination.chmod(entry["mode"])
                for directory, mode in reversed(directories):
                    directory.chmod(mode)
            if root.exists():
                # A crash after rename needs no second copy, but verify via the
                # staging download before replacing that interrupted hydration.
                remove_tree(root)
            staging.rename(root)
            self._save(id, status="local", local_present=True, error=None)
        except BaseException as exc:
            remove_tree(staging)
            self._save(id, status="restore_failed", error=str(exc))
            raise

    def read_file(self, id, path):
        root = self.root(id)
        path = Path(path)
        try:
            name = path.relative_to(root).as_posix()
        except ValueError:
            raise StorageError("Artifact path is outside this comparison")
        safe_name(name)
        with self.lock(id, shared=True):
            if self.state(id)["status"] not in ("evicting", "restoring") and path.exists():
                if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
                    raise StorageError("Invalid artifact file")
                return path.read_bytes()
            state, manifest = self.state(id), self.manifest(id)
            entry = next((e for e in manifest["entries"] if e["name"] == name and e["type"] == "file"), None)
            if entry is None:
                raise FileNotFoundError(name)
            if entry["size"] == 0:
                value = b""
            else:
                remote = manifest["archive"]
                result = self.client.get_object(**self._object_args(state, remote["key"], remote.get("version_id")),
                    Range=f"bytes={entry['offset']}-{entry['offset'] + entry['size'] - 1}")
                with closing(result["Body"]) as body:
                    value = body.read()
            if len(value) != entry["size"] or hashlib.sha256(value).hexdigest() != entry["sha256"]:
                raise StorageError("Downloaded artifact checksum mismatch")
            return value

    def files(self, id, path):
        root = self.root(id)
        directory = Path(path)
        prefix = directory.relative_to(root).as_posix() + "/"
        with self.lock(id, shared=True):
            if directory.exists() and self.state(id)["status"] not in ("evicting", "restoring"):
                return sorted(str(p.relative_to(directory)) for p in directory.rglob("*")
                              if p.is_file() and p.resolve().is_relative_to(root.resolve()))
            return sorted(e["name"][len(prefix):] for e in self.manifest(id)["entries"]
                          if e["type"] == "file" and e["name"].startswith(prefix))

    def archive_path(self, path):
        """Archive an explicitly owned non-application path, with retryable eviction."""
        path = Path(path).absolute()
        if path.is_symlink():
            raise StorageError("Artifact source must not be a symlink: " + str(path))
        id = "extra-" + hashlib.sha256(str(path).encode()).hexdigest()[:32]
        with self.lock(id):
            state = self.state(id)
            if not path.exists() and not state.get("manifest_key"):
                return None
            self._save(id, category="artifacts", source_path=str(path))
            return self.archive(id, path)

    def save_checkpoint(self, id):
        """The remote archive contains enough queue metadata to rebuild its index."""
        root = self.root(id)
        comparison = self.store.detail(id)
        comparison["status"] = comparison.get("artifact_final_status") or comparison["status"]
        comparison["artifact_final_status"] = None
        atomic_json(root / "_state" / "comparison.json",
                    {"root": str(root), "comparison": comparison})
        with (root / "_state" / "events.jsonl").open("w") as output:
            cursor = 0
            while events := self.store.events(id, cursor):
                for event in events:
                    output.write(json.dumps(event) + "\n")
                cursor = events[-1]["seq"]

    def load_index(self, id, category="comparisons"):
        self.root(id)
        if category not in ("comparisons", "artifacts"):
            raise StorageError("Invalid artifact category")
        try:
            result = self.client.get_object(
                Bucket=self.bucket, Key=f"{self.prefix}/{category}/{id}/latest.json")
        except Exception as exc:
            if getattr(exc, "response", {}).get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise KeyError(id) from exc
            raise
        with closing(result["Body"]) as body:
            state = json.loads(body.read())
        if (state.get("id") != id or state.get("bucket") != self.bucket
                or not state.get("manifest_key", "").startswith(f"{self.prefix}/{category}/{id}/")):
            raise StorageError("Invalid S3 artifact index")
        state = self._save(id, **{k: v for k, v in state.items() if k != "id"},
                          status="archived", local_present=False)
        self._verify_remote(state)
        return state

    def restore_scratch(self, id, path):
        with self.lock(id):
            if not self.state(id).get("manifest_key"):
                self.load_index(id, "artifacts")
            self._save(id, source_path=str(path))
            self.restore(id, path)

    def import_comparison(self, id):
        """Recover an S3 comparison index without hydrating its workspace."""
        self.root(id)  # Validate IDs before constructing S3 keys.
        with self.lock(id + ".execution"):
            try:
                return self.store.detail(id)
            except KeyError:
                pass
            self.load_index(id)
            checkpoint = json.loads(self.read_file(id, self.root(id) / "_state/comparison.json"))
            old_root, new_root = checkpoint["root"], str(self.root(id))

            def relocate(value):
                if isinstance(value, dict):
                    return {k: relocate(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [relocate(v) for v in value]
                if isinstance(value, str) and (value == old_root or value.startswith(old_root + "/")):
                    return new_root + value[len(old_root):]
                return value

            comparison = relocate(checkpoint["comparison"])
            if comparison["id"] != id:
                raise StorageError("Wrong comparison checkpoint")
            runs, questions = comparison.pop("runs"), comparison.pop("questions")
            events = self.read_file(id, self.root(id) / "_state/events.jsonl").decode().splitlines()
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                records = [("comparison", id, json.dumps(comparison))]
                for kind, values in (("run", runs), ("question", questions)):
                    for value in values:
                        if value["comparison_id"] != id:
                            raise StorageError("Checkpoint record belongs to another comparison")
                        records.append((kind, value["id"], json.dumps(value)))
                db.executemany("INSERT INTO objects VALUES(?,?,?)", records)
                for line in events:
                    event = relocate(json.loads(line))
                    db.execute("INSERT INTO events(comparison_id,run_id,at,type,data) VALUES(?,?,?,?,?)",
                               (id, event["run_id"], event["at"], event["type"], json.dumps(event["data"])))
            return self.store.detail(id)
