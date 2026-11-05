"""Content-addressed immutable blob storage for server peers."""
import hashlib
import os
import shutil

from cluster_peer import PeerError


MAX_BLOB_BYTES = 512 * 1024 * 1024
MIN_FREE_BYTES = 300 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


def valid_sha256(value):
    return (isinstance(value, str) and len(value) == 64 and
            all(character in "0123456789abcdef" for character in value))


def hash_file(path):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class BlobStore:
    def __init__(self, root, reserve_bytes=MIN_FREE_BYTES):
        self.root = os.path.abspath(root)
        if os.path.islink(self.root):
            raise PeerError(500, "каталог blob не должен быть ссылкой")
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        if type(reserve_bytes) is not int or reserve_bytes < 0:
            raise PeerError(400, "некорректный резерв места blob")
        self.reserve_bytes = reserve_bytes

    def path(self, sha256):
        if not valid_sha256(sha256):
            raise PeerError(400, "некорректный sha256 blob")
        return os.path.join(self.root, sha256[:2], sha256)

    def info(self, sha256, verify=False):
        path = self.path(sha256)
        try:
            size = os.path.getsize(path)
        except OSError:
            raise PeerError(404, "blob не найден на этом узле")
        if verify:
            actual, checked_size = hash_file(path)
            if (actual, checked_size) != (sha256, size):
                raise PeerError(409, "локальный blob повреждён")
        return {"sha256": sha256, "size": size, "path": path}

    def has(self, sha256, size):
        try:
            return self.info(sha256)["size"] == size
        except PeerError as error:
            if error.code == 404:
                return False
            raise

    def _prepare(self, sha256, size):
        if not valid_sha256(sha256) or type(size) is not int or not 0 <= size <= MAX_BLOB_BYTES:
            raise PeerError(400, "некорректный манифест blob")
        destination = self.path(sha256)
        os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)
        if os.path.exists(destination):
            info = self.info(sha256, verify=True)
            if info["size"] != size:
                raise PeerError(409, "размер существующего blob не совпадает")
            return destination, None
        return destination, destination + ".tmp-" + os.urandom(8).hex()

    def adopt(self, source, sha256, size):
        """Verify a completed upload and retain it without duplicating blocks when possible."""
        source = os.path.abspath(source)
        actual, actual_size = hash_file(source)
        if (actual, actual_size) != (sha256, size):
            raise PeerError(409, "контрольная сумма исходного файла не совпадает")
        destination, temporary = self._prepare(sha256, size)
        if temporary is None:
            return destination
        try:
            try:
                os.link(source, temporary)
            except OSError:
                if shutil.disk_usage(self.root).free < self.reserve_bytes + size:
                    raise PeerError(507, "недостаточно места для копии blob")
                with open(source, "rb") as reader, open(temporary, "xb") as writer:
                    shutil.copyfileobj(reader, writer, CHUNK_BYTES)
                    writer.flush()
                    os.fsync(writer.fileno())
            copied_hash, copied_size = hash_file(temporary)
            if (copied_hash, copied_size) != (sha256, size):
                raise PeerError(409, "blob изменился во время сохранения")
            os.replace(temporary, destination)
            try:
                os.chmod(destination, 0o600)
            except OSError:
                pass
            return destination
        finally:
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass

    def receive(self, stream, sha256, size):
        destination, temporary = self._prepare(sha256, size)
        if temporary is None:
            return destination
        if shutil.disk_usage(self.root).free < self.reserve_bytes + size:
            raise PeerError(507, "недостаточно места для принятия blob")
        digest = hashlib.sha256()
        received = 0
        try:
            with open(temporary, "xb") as writer:
                while received < size:
                    chunk = stream.read(min(CHUNK_BYTES, size - received))
                    if not chunk:
                        raise PeerError(503, "передача blob оборвалась")
                    writer.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                if stream.read(1):
                    raise PeerError(409, "узел прислал blob длиннее манифеста")
                writer.flush()
                os.fsync(writer.fileno())
            if digest.hexdigest() != sha256:
                raise PeerError(409, "sha256 принятого blob не совпадает")
            os.replace(temporary, destination)
            try:
                os.chmod(destination, 0o600)
            except OSError:
                pass
            return destination
        finally:
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass

    def materialize(self, sha256, size, destination):
        """Expose a verified blob at the application's upload path."""
        source = self.info(sha256, verify=True)
        if source["size"] != size:
            raise PeerError(409, "размер blob не совпадает с вложением")
        destination = os.path.abspath(destination)
        os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)
        if os.path.exists(destination):
            actual, actual_size = hash_file(destination)
            if (actual, actual_size) != (sha256, size):
                raise PeerError(409, "путь загрузки уже занят другим файлом")
            return destination
        temporary = destination + ".tmp-" + os.urandom(8).hex()
        try:
            try:
                os.link(source["path"], temporary)
            except OSError:
                if shutil.disk_usage(os.path.dirname(destination)).free < self.reserve_bytes + size:
                    raise PeerError(507, "недостаточно места для файла приложения")
                shutil.copyfile(source["path"], temporary)
            os.replace(temporary, destination)
            return destination
        finally:
            try:
                if os.path.exists(temporary):
                    os.remove(temporary)
            except OSError:
                pass
