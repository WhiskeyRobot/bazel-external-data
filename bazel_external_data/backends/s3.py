from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from bazel_external_data import util
from bazel_external_data.core import Backend


_ACCESS_DENIED_ERROR_CODES = {"403", "AccessDenied", "Forbidden"}
_CREDENTIAL_ERROR_FRAGMENTS = {
    "could not be found",
    "Unable to locate credentials",
    "NoCredentialsError",
}
# An expired (or missing) SSO token is a distinct, common failure with a
# specific fix -- re-running `aws sso login` -- so detect it separately and
# point the user at that command rather than the generic credential guidance.
_SSO_EXPIRED_ERROR_FRAGMENTS = {
    "Token has expired and refresh failed",
    "Error loading SSO Token",
    "session associated with this profile has expired",
    "UnauthorizedSSOTokenError",
    "retrieving token from sso",
}
_MISSING_ERROR_CODES = {"404", "NoSuchKey", "NotFound"}


class S3Backend(Backend):
    """An S3-backed content-addressed store using the AWS CLI.

    Objects are addressed by digest. For SHA512, the default key is exactly the
    digest value, matching the HTTP backend's SHA512 path convention. Other
    hash algorithms, if added later, use an algorithm subdirectory.

    Configuration keys (from the remote's entry in `.external_data.yml`):
      bucket (required): S3 bucket name (must not contain "/").
      prefix: optional key prefix within the bucket.
      profile: AWS named profile, passed as `--profile`.
      region: AWS region, passed as `--region`.
      endpoint_url: custom S3 endpoint (`--endpoint-url`), e.g. for an
        S3-compatible store.
      aws_cli: AWS CLI executable name/path (default "aws").
      disable_upload: if true, `upload_file` raises (default false).
      verbose: if true, log AWS commands and progress (default false).

    Operations use the `s3api` subcommands rather than the higher-level `s3`
    commands because the existence/dedup check needs `head-object`'s
    structured result to tell missing (404) apart from access-denied (403),
    which `s3 ls` does not surface cleanly. The other operations use `s3api`
    too, for consistency.
    """

    def __init__(self, config, project_root, user):
        super().__init__(config, project_root, user)
        self._aws_cli = config.get("aws_cli", "aws")

        # The bucket is a name, not a path; reject an embedded "/" (a common
        # mistake when a prefix is meant) but tolerate surrounding slashes.
        bucket = config["bucket"].strip("/")
        if "/" in bucket:
            raise ValueError(
                "S3 backend 'bucket' must be a bucket name without '/': "
                f"{config['bucket']!r}")
        self._bucket = bucket

        self._prefix = config.get("prefix", "").strip("/")
        self._disable_upload = config.get("disable_upload", False)
        self._verbose = config.get("verbose", False)
        self._project_root = project_root

        self._profile = config.get("profile")
        self._region = config.get("region")
        self._endpoint_url = config.get("endpoint_url")

        # TODO: Support AWS authentication configuration via the `user` input
        # channel (see RobotLocomotion's docs/config/external_data.user.yml)
        # in addition to the ambient AWS credential chain.

    def _verbose_print(self, text):
        if self._verbose:
            print(text)

    def _s3_uri(self, key):
        return f"s3://{self._bucket}/{key}"

    def _object_key(self, hash):
        hash_path = ("" if hash.get_algo() == "sha512"
                     else f"{hash.get_algo()}/")
        key = f"{hash_path}{hash.get_value()}"
        if self._prefix:
            key = f"{self._prefix}/{key}"
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
        # Disable the AWS CLI v2 pager so output is never piped through an
        # interactive pager (e.g. less), which can hang or garble the captured
        # output in a non-interactive subprocess.
        env["AWS_PAGER"] = ""
        return env

    def _run_aws(self, args):
        cmd = self._aws_base_args() + args
        self._verbose_print(" ".join(cmd))
        try:
            return subprocess.run(
                cmd,
                # Merge stderr into stdout so a single stream carries all
                # output for error reporting.
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=self._aws_env(),
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                "The S3 backend requires the AWS CLI. Install aws or use a "
                "non-S3 backend."
            ) from e

    def _has_error_code(self, output, codes):
        return any(code in output for code in codes)

    def _is_access_denied_error(self, output):
        return self._has_error_code(output, _ACCESS_DENIED_ERROR_CODES)

    def _is_credential_error(self, output):
        return self._has_error_code(output, _CREDENTIAL_ERROR_FRAGMENTS)

    def _is_sso_expired_error(self, output):
        return self._has_error_code(output, _SSO_EXPIRED_ERROR_FRAGMENTS)

    def _is_missing_result(self, output):
        return self._has_error_code(output, _MISSING_ERROR_CODES)

    def _handle_aws_error(self, result, operation, key):
        # stderr is merged into stdout (see _run_aws); always include the full
        # output so that all information is available when debugging.
        output = result.stdout
        if self._is_access_denied_error(output):
            raise RuntimeError(
                f"S3 {operation} denied for {self._s3_uri(key)}. Check AWS "
                f"credentials and bucket permissions.\n{output}")
        if self._is_sso_expired_error(output):
            profile = self._profile or os.environ.get("AWS_PROFILE")
            login_cmd = f"{self._aws_cli} sso login"
            if profile:
                login_cmd += f" --profile={profile}"
            raise RuntimeError(
                f"AWS SSO credentials for S3 {operation} of "
                f"{self._s3_uri(key)} have expired. Refresh them and re-run:\n"
                f"    {login_cmd}\n"
                "(over SSH without a browser, add --use-device-code)\n"
                f"{output}")
        if self._is_credential_error(output):
            raise RuntimeError(
                f"AWS credentials are not available for S3 {operation} of "
                f"{self._s3_uri(key)}. Configure standard AWS authentication "
                "such as AWS_PROFILE, environment variables, or an instance "
                f"role.\n{output}")
        raise RuntimeError(
            f"S3 {operation} failed for {self._s3_uri(key)}: {output}")

    def _git_value(self, args):
        """Returns stripped `git` output, or None on any failure (quiet)."""
        try:
            output = subprocess.check_output(
                ["git", "-C", self._project_root] + args,
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            return output or None
        except (OSError, subprocess.CalledProcessError):
            return None

    def _best_effort_git_value(self, args):
        value = self._git_value(args)
        if value is None:
            print(
                f"warning: git {' '.join(args)} failed; using 'unknown' for "
                "upload metadata",
                file=sys.stderr)
            return "unknown"
        return value

    def _git_remote_url(self):
        # Prefer the remote that the current branch tracks; fall back to
        # origin, then to any configured remote, then to "unknown".
        candidates = []
        upstream = self._git_value(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        if upstream and "/" in upstream:
            candidates.append(upstream.split("/", 1)[0])
        candidates.append("origin")
        remotes = self._git_value(["remote"])
        if remotes:
            candidates.extend(remotes.split())

        seen = set()
        for remote in candidates:
            if remote in seen:
                continue
            seen.add(remote)
            url = self._git_value(["remote", "get-url", remote])
            if url:
                return url
        return "unknown"

    def _compute_metadata(self, project_relpath, filepath):
        now = datetime.now(timezone.utc)
        timestamp = now.isoformat().replace("+00:00", "Z")
        uploaded_by = self._best_effort_git_value(["config", "user.email"])
        git_remote = self._git_remote_url()
        git_commit = self._best_effort_git_value(["rev-parse", "HEAD"])
        return {
            "original-path": project_relpath,
            "original-name": Path(filepath).name,
            "original-time": timestamp,
            "uploaded-by": uploaded_by,
            "git-remote-best-effort": git_remote,
            "git-commit-best-effort": git_commit,
        }

    def check_file(self, hash, project_relpath):
        key = self._object_key(hash)
        self._verbose_print(f"head {self._s3_uri(key)}")
        result = self._run_aws([
            "s3api",
            "head-object",
            "--bucket", self._bucket,
            "--key", key,
        ])
        if result.returncode == 0:
            return True
        if self._is_missing_result(result.stdout):
            return False
        self._handle_aws_error(result, "HEAD", key)

    def download_file(self, hash, project_relpath, output_file):
        key = self._object_key(hash)
        if not self.check_file(hash, project_relpath):
            raise util.DownloadError(
                f"File not available '{project_relpath}' "
                f"(hash: {hash.get_value()})")
        self._verbose_print(f"get {self._s3_uri(key)}")
        result = self._run_aws([
            "s3api",
            "get-object",
            "--bucket", self._bucket,
            "--key", key,
            output_file,
        ])
        if result.returncode != 0:
            self._handle_aws_error(result, "GET", key)
        self._verbose_print("File downloaded successfully!")

    def upload_file(self, hash, project_relpath, filepath):
        if self._disable_upload:
            raise RuntimeError("Upload disabled")
        key = self._object_key(hash)
        if self.check_file(hash, project_relpath):
            print("File already uploaded")
            return
        self._verbose_print(f"put {self._s3_uri(key)}")
        result = self._run_aws([
            "s3api",
            "put-object",
            "--bucket", self._bucket,
            "--key", key,
            "--body", filepath,
            "--metadata", json.dumps(
                self._compute_metadata(project_relpath, filepath),
                sort_keys=True,
            ),
        ])
        if result.returncode != 0:
            self._handle_aws_error(result, "PUT", key)
        print("File uploaded successfully!")
