#!/bin/bash
set -eu -o pipefail

# Copy necessary srcs to create a (set of) workspace(s) from an existing Bazel
# workspace. For testing Bazel workflows.

if [[ ! $(basename $(dirname ${PWD})) =~ .*\.runfiles ]]; then
    echo "Must be run from within Bazel"
    exit 1
fi

# Handle case when running with `bazel run`.
if [[ -z "${TEST_TMPDIR:-}" ]]; then
    tmp_base=/tmp/bazel_workspace_test
    mkdir -p "${tmp_base}"
    export TEST_TMPDIR=$(mktemp -d -p "${tmp_base}")
fi

# Declare the new workspace directory (do not use the root, as that will
# confuse Bazel with infinite symlinks).
workspace_dir="${TEST_TMPDIR}/workspace"
mkdir -p "${workspace_dir}"
copy_tree() {
    local src_root="${1}"
    local dst_root="${2}"
    local src
    (
        cd "${src_root}"
        find . -type f -o -type l
    ) | while IFS= read -r src; do
        local subdir
        subdir=$(dirname "${src}")
        mkdir -p "${dst_root}/${subdir}"
        cp "${src_root}/${src}" "${dst_root}/${subdir}"
    done
}

# Copy the main workspace into the new workspace root.
copy_tree "${PWD}" "${workspace_dir}"

# Also copy sibling external repositories from runfiles. Newer Bazel runfiles
# layouts do not always place external repositories under ./external.
runfiles_dir=$(dirname "${PWD}")
main_workspace=$(basename "${PWD}")
for repo_dir in "${runfiles_dir}"/*; do
    repo_name=$(basename "${repo_dir}")
    if [[ "${repo_name}" == "${main_workspace}" ]]; then
        continue
    fi
    if [[ ! -d "${repo_dir}" ]]; then
        continue
    fi
    copy_tree "${repo_dir}" "${workspace_dir}/${repo_name}"
done

# Execute command.
cd "${workspace_dir}"
exec "$@"
