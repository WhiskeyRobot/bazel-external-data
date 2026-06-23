from datetime import datetime, timezone
import json
import os
import subprocess

from bazel_external_data import util
from bazel_external_data.core import Backend


_ACCESS_DENIED_ERROR_CODES = {"403", "AccessDenied", "Forbidden"}
_CREDENTIAL_ERROR_FRAGMENTS = {
    "could not be found",
    "Unable to locate credentials",
    "NoCredentialsError",
}
_MISSING_ERROR_CODES = {"404", "NoSuchKey", "NotFound"}


class S3Backend(Backend):
    """An S3-backed content-addressed store using the AWS CLI.

    Objects are addressed by digest. For SHA512, the default key is exactly the
    digest value, matching the HTTP backend's SHA512 path convention. Other
    hash algorithms, if added later, use an algorithm subdirectory.
    """

    def __init__(self, config, project_root, user):
        Backend.__init__(self, config, project_root, user)
        self._aws_cli = config.get("aws_cli", "aws")
        self._bucket = config["bucket"]
        self._prefix = config.get("prefix", "").strip("/")
        self._disable_upload = config.get("disable_upload", False)
        self._verbose = config.get("verbose", False)
        self._project_root = project_root

        self._profile = config.get("profile")
        self._region = config.get("region")
        self._endpoint_url = config.get("endpoint_url")
        self._max_attempts = config.get("max_attempts")
        self._retry_mode = config.get("retry_mode")

    def _verbose_print(self, text):
        if self._verbose:
            print(text)

    def _s3_uri(self, key=""):
        if key:
            return "s3://{}/{}".format(self._bucket, key)
        return "s3://{}".format(self._bucket)

    def _object_key(self, hash):
        hash_path = ("" if hash.get_algo() == "sha512"
                     else "{}/".format(hash.get_algo()))
        key = "{}{}".format(hash_path, hash.get_value())
        if self._prefix:
            key = "{}/{}".format(self._prefix, key)
        return key

    def _aws_base_args(self):
        args = [self._aws_cli]
        if self._profile:
            args += ["--profile", self._profile]
        if self._region:
            args += ["--region", self._region]
        if self._endpoint_url:
            args += ["--endpoint-url", self._endpoint_url]
        return args

    def _aws_env(self):
        env = os.environ.copy()
        env["AWS_PAGER"] = ""
        if self._max_attempts is not None:
            env["AWS_MAX_ATTEMPTS"] = str(self._max_attempts)
        if self._retry_mode is not None:
            env["AWS_RETRY_MODE"] = str(self._retry_mode)
        return env

    def _run_aws(self, args):
        cmd = self._aws_base_args() + args
        self._verbose_print(" ".join(cmd))
        try:
            return subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._aws_env(),
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "The S3 backend requires the AWS CLI. Install aws or use a "
                "non-S3 backend."
            ) from e

    def _aws_output(self, result):
        return "{}\n{}".format(result.stdout, result.stderr).strip()

    def _has_error_code(self, result, codes):
        output = self._aws_output(result)
        return any(code in output for code in codes)

    def _is_missing_result(self, result):
        return self._has_error_code(result, _MISSING_ERROR_CODES)

    def _handle_aws_error(self, result, operation, key):
        output = self._aws_output(result)
        if self._has_error_code(result, _ACCESS_DENIED_ERROR_CODES):
            raise RuntimeError(
                "S3 {} denied for {}. Check AWS credentials and bucket "
                "permissions.".format(operation, self._s3_uri(key))
            )
        if any(fragment in output for fragment in _CREDENTIAL_ERROR_FRAGMENTS):
            raise RuntimeError(
                "AWS credentials are not available for S3 {} of {}. "
                "Configure standard AWS authentication such as AWS_PROFILE, "
                "environment variables, or an instance role.".format(
                    operation, self._s3_uri(key))
            )
        raise RuntimeError(
            "S3 {} failed for {}: {}".format(
                operation, self._s3_uri(key), output)
        )

    def _best_effort_git_value(self, args):
        try:
            return subprocess.check_output(
                ["git", "-C", self._project_root] + args,
                stderr=subprocess.DEVNULL,
            ).decode("utf8").strip() or "unknown"
        except (OSError, subprocess.CalledProcessError):
            return "unknown"

    def _upload_metadata(self, project_relpath, filepath):
        return {
            "original-path": project_relpath,
            "original-name": os.path.basename(filepath),
            "original-time": (
                datetime.now(timezone.utc).isoformat()
                    .replace("+00:00", "Z")),
            "uploaded-by": self._best_effort_git_value(
                ["config", "user.email"]),
            "git-remote-best-effort": self._best_effort_git_value(
                ["config", "--get", "remote.origin.url"]),
            "git-commit-best-effort": self._best_effort_git_value(
                ["rev-parse", "HEAD"]),
        }

    def check_file(self, hash, project_relpath):
        key = self._object_key(hash)
        self._verbose_print("head {}".format(self._s3_uri(key)))
        result = self._run_aws([
            "s3api",
            "head-object",
            "--bucket", self._bucket,
            "--key", key,
        ])
        if result.returncode == 0:
            return True
        if self._is_missing_result(result):
            return False
        self._handle_aws_error(result, "HEAD", key)

    def download_file(self, hash, project_relpath, output_file):
        key = self._object_key(hash)
        if not self.check_file(hash, project_relpath):
            raise util.DownloadError(
                "File not available '{}' (hash: {})".format(
                    project_relpath, hash.get_value()))
        self._verbose_print("get {}".format(self._s3_uri(key)))
        result = self._run_aws([
            "s3api",
            "get-object",
            "--bucket", self._bucket,
            "--key", key,
            output_file,
        ])
        if result.returncode != 0:
            self._handle_aws_error(result, "GET", key)

    def upload_file(self, hash, project_relpath, filepath):
        if self._disable_upload:
            raise RuntimeError("Upload disabled")
        key = self._object_key(hash)
        if self.check_file(hash, project_relpath):
            print("File already uploaded")
            return
        self._verbose_print("put {}".format(self._s3_uri(key)))
        result = self._run_aws([
            "s3api",
            "put-object",
            "--bucket", self._bucket,
            "--key", key,
            "--body", filepath,
            "--metadata", json.dumps(
                self._upload_metadata(project_relpath, filepath),
                sort_keys=True,
            ),
        ])
        if result.returncode != 0:
            self._handle_aws_error(result, "PUT", key)
        print("File uploaded successfully!")
