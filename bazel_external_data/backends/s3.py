from datetime import datetime, timezone
import os
import subprocess

from bazel_external_data import util
from bazel_external_data.core import Backend


_MISSING_ERROR_CODES = {"404", "NoSuchKey", "NotFound"}


class S3Backend(Backend):
    """An S3-backed content-addressed store.

    Objects are addressed by digest. For SHA512, the default key is exactly the
    digest value, matching the HTTP backend's SHA512 path convention. Other
    hash algorithms, if added later, use an algorithm subdirectory.
    """

    def __init__(self, config, project_root, user):
        Backend.__init__(self, config, project_root, user)
        self._bucket = config["bucket"]
        self._prefix = config.get("prefix", "").strip("/")
        self._disable_upload = config.get("disable_upload", False)
        self._verbose = config.get("verbose", False)
        self._project_root = project_root
        self._botocore_exceptions = None
        self._client = self._make_client(config)

    def _make_client(self, config):
        try:
            import boto3
            import botocore.config
            import botocore.exceptions
        except ImportError as e:
            raise RuntimeError(
                "The S3 backend requires boto3 and botocore. Install boto3 "
                "or use a non-S3 backend."
            ) from e
        self._botocore_exceptions = botocore.exceptions

        session_kwargs = {}
        if "profile_name" in config:
            session_kwargs["profile_name"] = config["profile_name"]
        if "region_name" in config:
            session_kwargs["region_name"] = config["region_name"]
        try:
            session = boto3.session.Session(**session_kwargs)

            client_kwargs = {}
            if "endpoint_url" in config:
                client_kwargs["endpoint_url"] = config["endpoint_url"]
            client_kwargs["config"] = botocore.config.Config(
                retries={
                    "max_attempts": config.get("max_attempts", 10),
                    "mode": config.get("retry_mode", "standard"),
                },
            )
            return session.client("s3", **client_kwargs)
        except Exception as e:
            self._handle_boto_error(e, "CONFIGURE", self._prefix)
            raise

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

    def _handle_client_error(self, e, operation, key):
        code = e.response.get("Error", {}).get("Code")
        if code in ["403", "AccessDenied"]:
            raise RuntimeError(
                "S3 {} denied for {}. Check AWS credentials and bucket "
                "permissions.".format(operation, self._s3_uri(key))
            ) from e
        raise RuntimeError(
            "S3 {} failed for {}: {}".format(
                operation, self._s3_uri(key), e)
        ) from e

    def _handle_credential_error(self, e, operation, key):
        raise RuntimeError(
            "AWS credentials are not available for S3 {} of {}. "
            "Configure standard AWS authentication such as AWS_PROFILE, "
            "environment variables, or an instance role.".format(
                operation, self._s3_uri(key))
        ) from e

    def _client_error_code(self, e):
        return e.response.get("Error", {}).get("Code")

    def _is_missing_error(self, e):
        exceptions = self._botocore_exceptions
        if isinstance(e, exceptions.ClientError):
            return self._client_error_code(e) in _MISSING_ERROR_CODES
        return False

    def _handle_boto_error(self, e, operation, key):
        exceptions = self._botocore_exceptions
        if isinstance(e, exceptions.ClientError):
            self._handle_client_error(e, operation, key)
        elif isinstance(e, (
                exceptions.NoCredentialsError,
                exceptions.ProfileNotFound)):
            self._handle_credential_error(e, operation, key)
        elif isinstance(e, exceptions.BotoCoreError):
            raise RuntimeError(
                "S3 {} failed for {}: {}".format(
                    operation, self._s3_uri(key), e)
            ) from e

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
        self._verbose_print("head s3://{}/{}".format(self._bucket, key))
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception as e:
            if self._is_missing_error(e):
                return False
            self._handle_boto_error(e, "HEAD", key)
            raise

    def download_file(self, hash, project_relpath, output_file):
        key = self._object_key(hash)
        if not self.check_file(hash, project_relpath):
            raise util.DownloadError(
                "File not available '{}' (hash: {})".format(
                    project_relpath, hash.get_value()))
        self._verbose_print("get s3://{}/{}".format(self._bucket, key))
        try:
            self._client.download_file(self._bucket, key, output_file)
        except Exception as e:
            self._handle_boto_error(e, "GET", key)
            raise

    def upload_file(self, hash, project_relpath, filepath):
        if self._disable_upload:
            raise RuntimeError("Upload disabled")
        key = self._object_key(hash)
        if self.check_file(hash, project_relpath):
            print("File already uploaded")
            return
        self._verbose_print("put s3://{}/{}".format(self._bucket, key))
        extra_args = {
            "Metadata": self._upload_metadata(project_relpath, filepath),
        }
        try:
            self._client.upload_file(
                filepath, self._bucket, key, ExtraArgs=extra_args)
        except Exception as e:
            self._handle_boto_error(e, "PUT", key)
            raise
        print("File uploaded successfully!")
