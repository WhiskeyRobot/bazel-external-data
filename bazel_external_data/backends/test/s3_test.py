import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from bazel_external_data import core, hashes
from bazel_external_data.backends.s3 import S3Backend


def _completed_process(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class FakeAws(object):
    def __init__(self):
        self.calls = []
        self.objects = {}
        self.deny_head = False
        self.fail_download = False

    def run(self, cmd, stdout, stderr, text, env):
        self.calls.append({
            "cmd": cmd,
            "stdout": stdout,
            "stderr": stderr,
            "text": text,
            "env": env,
        })
        self.assert_run_kwargs(stdout, stderr, text)

        s3api_index = cmd.index("s3api")
        operation = cmd[s3api_index + 1]
        bucket = self._flag(cmd, "--bucket")
        key = self._flag(cmd, "--key")

        if operation == "head-object":
            return self._head_object(cmd, bucket, key)
        if operation == "put-object":
            return self._put_object(cmd, bucket, key)
        if operation == "get-object":
            return self._get_object(cmd, bucket, key)
        raise AssertionError("Unsupported operation: {}".format(operation))

    def assert_run_kwargs(self, stdout, stderr, text):
        if stdout != subprocess.PIPE:
            raise AssertionError(stdout)
        if stderr != subprocess.PIPE:
            raise AssertionError(stderr)
        if text is not True:
            raise AssertionError(text)

    def _flag(self, cmd, name):
        return cmd[cmd.index(name) + 1]

    def _head_object(self, cmd, bucket, key):
        if self.deny_head:
            return _completed_process(
                cmd,
                returncode=255,
                stderr=(
                    "An error occurred (AccessDenied) when calling the "
                    "HeadObject operation: Access Denied"
                ),
            )
        if (bucket, key) not in self.objects:
            return _completed_process(
                cmd,
                returncode=255,
                stderr=(
                    "An error occurred (404) when calling the HeadObject "
                    "operation: Not Found"
                ),
            )
        return _completed_process(cmd, stdout="{}\n")

    def _put_object(self, cmd, bucket, key):
        filepath = self._flag(cmd, "--body")
        with open(filepath, "rb") as f:
            body = f.read()
        self.objects[(bucket, key)] = {
            "body": body,
            "metadata": json.loads(self._flag(cmd, "--metadata")),
        }
        return _completed_process(cmd, stdout='{"ETag": "etag"}\n')

    def _get_object(self, cmd, bucket, key):
        if self.fail_download:
            return _completed_process(
                cmd,
                returncode=255,
                stderr=(
                    "An error occurred (RequestTimeout) when calling the "
                    "GetObject operation: network unavailable"
                ),
            )
        output_file = cmd[-1]
        with open(output_file, "wb") as f:
            f.write(self.objects[(bucket, key)]["body"])
        return _completed_process(cmd, stdout='{"ContentLength": 8}\n')


class S3Test(unittest.TestCase):
    def setUp(self):
        self._test_dir = tempfile.mkdtemp(
            dir=os.environ.get("TEST_TEMPDIR", None))
        self._fake_aws = FakeAws()
        self._run_patch = mock.patch(
            "subprocess.run",
            side_effect=self._fake_aws.run,
        )
        self._check_output_patch = mock.patch(
            "subprocess.check_output",
            side_effect=subprocess.CalledProcessError(1, "git"),
        )
        self._run_patch.start()
        self._check_output_patch.start()

    def tearDown(self):
        self._check_output_patch.stop()
        self._run_patch.stop()
        shutil.rmtree(self._test_dir)

    def _make_dut(self, prefix="", **kwargs):
        config = {
            "backend": "s3",
            "bucket": "unit-test-bucket",
        }
        if prefix:
            config["prefix"] = prefix
        config.update(kwargs)
        return S3Backend(
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

    def test_aws_cli_configuration(self):
        dut = self._make_dut(
            aws_cli="aws-test",
            endpoint_url="https://example.invalid",
            max_attempts=3,
            profile_name="unit-test-profile",
            region_name="us-west-2",
            retry_mode="adaptive",
        )
        filepath = self._make_file()
        hashsum = hashes.sha512.compute(filepath)

        self.assertFalse(
            dut.check_file(hashsum, "external_data/archives/payload.tar.gz"))

        call = self._fake_aws.calls[-1]
        self.assertEqual(
            [
                "aws-test",
                "--profile", "unit-test-profile",
                "--region", "us-west-2",
                "--endpoint-url", "https://example.invalid",
                "s3api",
                "head-object",
                "--bucket", "unit-test-bucket",
                "--key", hashsum.get_value(),
            ],
            call["cmd"],
        )
        self.assertEqual("", call["env"]["AWS_PAGER"])
        self.assertEqual("3", call["env"]["AWS_MAX_ATTEMPTS"])
        self.assertEqual("adaptive", call["env"]["AWS_RETRY_MODE"])

    def test_file_lifecycle_and_upload_metadata(self):
        dut = self._make_dut(prefix="scratch/test")
        filepath = self._make_file()
        hashsum = hashes.sha512.compute(filepath)
        project_relpath = "external_data/archives/payload.tar.gz"

        self.assertFalse(dut.check_file(hashsum, project_relpath))
        dut.upload_file(hashsum, project_relpath, filepath)
        self.assertTrue(dut.check_file(hashsum, project_relpath))

        key = dut._object_key(hashsum)
        metadata = self._fake_aws.objects[
            ("unit-test-bucket", key)
        ]["metadata"]
        self.assertEqual(project_relpath, metadata["original-path"])
        self.assertEqual("payload.txt", metadata["original-name"])
        self.assertIn("git-commit-best-effort", metadata)

        os.remove(filepath)
        dut.download_file(hashsum, project_relpath, filepath)
        with open(filepath, "r") as f:
            self.assertEqual("payload\n", f.read())

    def test_upload_can_be_disabled(self):
        dut = self._make_dut(disable_upload=True)
        filepath = self._make_file()
        hashsum = hashes.sha512.compute(filepath)

        with self.assertRaisesRegex(RuntimeError, "Upload disabled"):
            dut.upload_file(
                hashsum,
                "external_data/archives/payload.tar.gz",
                filepath)

    def test_access_denied_is_an_error(self):
        dut = self._make_dut()
        filepath = self._make_file()
        hashsum = hashes.sha512.compute(filepath)
        self._fake_aws.deny_head = True

        with self.assertRaisesRegex(RuntimeError, "S3 HEAD denied"):
            dut.check_file(hashsum, "external_data/archives/payload.tar.gz")

    def test_missing_aws_cli_error_is_clear(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            dut = self._make_dut()
            filepath = self._make_file()
            hashsum = hashes.sha512.compute(filepath)
            with self.assertRaisesRegex(RuntimeError, "requires the AWS CLI"):
                dut.check_file(
                    hashsum,
                    "external_data/archives/payload.tar.gz")

    def test_credential_errors_are_clear(self):
        def missing_credentials(cmd, stdout, stderr, text, env):
            return _completed_process(
                cmd,
                returncode=255,
                stderr="Unable to locate credentials",
            )

        with mock.patch("subprocess.run", side_effect=missing_credentials):
            dut = self._make_dut()
            filepath = self._make_file()
            hashsum = hashes.sha512.compute(filepath)
            with self.assertRaisesRegex(
                    RuntimeError, "AWS credentials are not available"):
                dut.check_file(
                    hashsum,
                    "external_data/archives/payload.tar.gz")

    def test_generic_aws_errors_are_not_credential_errors(self):
        dut = self._make_dut()
        filepath = self._make_file()
        hashsum = hashes.sha512.compute(filepath)
        project_relpath = "external_data/archives/payload.tar.gz"
        dut.upload_file(hashsum, project_relpath, filepath)
        self._fake_aws.fail_download = True

        with self.assertRaisesRegex(RuntimeError, "S3 GET failed"):
            dut.download_file(hashsum, project_relpath, filepath)


if __name__ == "__main__":
    unittest.main()
