import os
import shutil
import sys
import tempfile
import types
import unittest

from bazel_external_data import core, hashes


class ClientError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class BotoCoreError(Exception):
    pass


class NoCredentialsError(BotoCoreError):
    pass


class ProfileNotFound(BotoCoreError):
    pass


class FakeS3Client(object):
    objects = {}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError("404")

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
        with open(Filename, "rb") as f:
            body = f.read()
        self.objects[(Bucket, Key)] = {
            "body": body,
            "extra_args": ExtraArgs,
        }

    def download_file(self, Bucket, Key, Filename):
        with open(Filename, "wb") as f:
            f.write(self.objects[(Bucket, Key)]["body"])


class FakeSession(object):
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def client(self, name, **kwargs):
        assert name == "s3"
        return FakeS3Client()


class Config(object):
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class S3Test(unittest.TestCase):
    def setUp(self):
        self._old_modules = dict(sys.modules)
        FakeS3Client.objects = {}
        boto3 = types.ModuleType("boto3")
        boto3.session = types.SimpleNamespace(Session=FakeSession)
        botocore = types.ModuleType("botocore")
        botocore.config = types.SimpleNamespace(Config=Config)
        botocore.exceptions = types.SimpleNamespace(
            BotoCoreError=BotoCoreError,
            ClientError=ClientError,
            NoCredentialsError=NoCredentialsError,
            ProfileNotFound=ProfileNotFound,
        )
        sys.modules["boto3"] = boto3
        sys.modules["botocore"] = botocore
        sys.modules["botocore.config"] = botocore.config
        sys.modules["botocore.exceptions"] = botocore.exceptions

        from bazel_external_data.backends.s3 import S3Backend
        self._backend_cls = S3Backend
        self._test_dir = tempfile.mkdtemp(
            dir=os.environ.get("TEST_TEMPDIR", None))

    def tearDown(self):
        sys.modules.clear()
        sys.modules.update(self._old_modules)
        shutil.rmtree(self._test_dir)

    def _make_dut(self, prefix=""):
        config = {
            "backend": "s3",
            "bucket": "unit-test-bucket",
        }
        if prefix:
            config["prefix"] = prefix
        return self._backend_cls(
            config,
            self._test_dir,
            core.User({"core": {"cache_dir": self._test_dir}}))

    def _make_file(self):
        filepath = os.path.join(self._test_dir, "payload.txt")
        with open(filepath, "w") as f:
            f.write("payload\n")
        return filepath

    def test_object_key_matches_http_convention_for_sha512(self):
        dut = self._make_dut()
        filepath = self._make_file()
        hashsum = hashes.sha512.compute(filepath)
        self.assertEqual(hashsum.get_value(), dut._object_key(hashsum))

    def test_object_key_allows_prefix(self):
        dut = self._make_dut(prefix="/scratch/test/")
        filepath = self._make_file()
        hashsum = hashes.sha512.compute(filepath)
        self.assertEqual(
            "scratch/test/{}".format(hashsum.get_value()),
            dut._object_key(hashsum))

    def test_file_lifecycle_and_upload_metadata(self):
        dut = self._make_dut(prefix="scratch/test")
        filepath = self._make_file()
        hashsum = hashes.sha512.compute(filepath)
        project_relpath = "external_data/archives/payload.tar.gz"

        self.assertFalse(dut.check_file(hashsum, project_relpath))
        dut.upload_file(hashsum, project_relpath, filepath)
        self.assertTrue(dut.check_file(hashsum, project_relpath))

        key = dut._object_key(hashsum)
        metadata = FakeS3Client.objects[
            ("unit-test-bucket", key)
        ]["extra_args"]["Metadata"]
        self.assertEqual(project_relpath, metadata["original-path"])
        self.assertEqual("payload.txt", metadata["original-filename"])
        self.assertIn("git-commit-best-effort", metadata)

        os.remove(filepath)
        dut.download_file(hashsum, project_relpath, filepath)
        with open(filepath, "r") as f:
            self.assertEqual("payload\n", f.read())


if __name__ == "__main__":
    unittest.main()
