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
    deny_head = False

    def head_object(self, Bucket, Key):
        if self.deny_head:
            raise ClientError("AccessDenied")
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
    sessions = []
    client_calls = []
    raise_on_init = False

    def __init__(self, **kwargs):
        if self.raise_on_init:
            raise ProfileNotFound()
        self.kwargs = kwargs
        self.sessions.append(self)

    def client(self, name, **kwargs):
        assert name == "s3"
        self.client_calls.append((name, kwargs))
        return FakeS3Client()


class Config(object):
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class S3Test(unittest.TestCase):
    def setUp(self):
        self._module_names = [
            "boto3",
            "botocore",
            "botocore.config",
            "botocore.exceptions",
        ]
        self._old_modules = {
            name: sys.modules.get(name) for name in self._module_names}
        FakeS3Client.objects = {}
        FakeS3Client.deny_head = False
        FakeSession.sessions = []
        FakeSession.client_calls = []
        FakeSession.raise_on_init = False
        boto3 = types.ModuleType("boto3")
        boto3.session = types.SimpleNamespace(Session=FakeSession)
        botocore = types.ModuleType("botocore")
        botocore_config = types.ModuleType("botocore.config")
        botocore_config.Config = Config
        botocore_exceptions = types.ModuleType("botocore.exceptions")
        botocore_exceptions.BotoCoreError = BotoCoreError
        botocore_exceptions.ClientError = ClientError
        botocore_exceptions.NoCredentialsError = NoCredentialsError
        botocore_exceptions.ProfileNotFound = ProfileNotFound
        botocore.config = botocore_config
        botocore.exceptions = botocore_exceptions
        sys.modules["boto3"] = boto3
        sys.modules["botocore"] = botocore
        sys.modules["botocore.config"] = botocore_config
        sys.modules["botocore.exceptions"] = botocore_exceptions

        from bazel_external_data.backends.s3 import S3Backend
        self._backend_cls = S3Backend
        self._test_dir = tempfile.mkdtemp(
            dir=os.environ.get("TEST_TEMPDIR", None))

    def tearDown(self):
        for name in self._module_names:
            old_module = self._old_modules[name]
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module
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

    def test_client_configuration(self):
        config = {
            "backend": "s3",
            "bucket": "unit-test-bucket",
            "endpoint_url": "https://example.invalid",
            "max_attempts": 3,
            "profile_name": "unit-test-profile",
            "region_name": "us-west-2",
            "retry_mode": "adaptive",
        }
        self._backend_cls(
            config,
            self._test_dir,
            core.User({"core": {"cache_dir": self._test_dir}}))

        self.assertEqual(
            {"profile_name": "unit-test-profile", "region_name": "us-west-2"},
            FakeSession.sessions[-1].kwargs)
        name, client_kwargs = FakeSession.client_calls[-1]
        self.assertEqual("s3", name)
        self.assertEqual(
            "https://example.invalid", client_kwargs["endpoint_url"])
        self.assertEqual(
            {"max_attempts": 3, "mode": "adaptive"},
            client_kwargs["config"].kwargs["retries"])

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
        self.assertEqual("payload.txt", metadata["original-name"])
        self.assertIn("git-commit-best-effort", metadata)

        os.remove(filepath)
        dut.download_file(hashsum, project_relpath, filepath)
        with open(filepath, "r") as f:
            self.assertEqual("payload\n", f.read())

    def test_access_denied_is_an_error(self):
        dut = self._make_dut()
        filepath = self._make_file()
        hashsum = hashes.sha512.compute(filepath)
        FakeS3Client.deny_head = True

        with self.assertRaisesRegex(RuntimeError, "S3 HEAD denied"):
            dut.check_file(hashsum, "external_data/archives/payload.tar.gz")

    def test_client_setup_errors_are_clear(self):
        FakeSession.raise_on_init = True

        with self.assertRaisesRegex(
                RuntimeError, "AWS credentials are not available"):
            self._make_dut(prefix="scratch/test")


if __name__ == "__main__":
    unittest.main()
