# Can't name it `bazel_external_data` due to Python package clashes.
# @ref https://github.com/bazelbuild/bazel/issues/3998
workspace(name = "bazel_external_data_pkg")

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")

http_archive(
    name = "rules_python",
    sha256 = "9f9f3b300a9264e4c77999312ce663be5dee9a56e361a1f6fe7ec60e1beef9a3",
    strip_prefix = "rules_python-1.4.1",
    url = "https://github.com/bazel-contrib/rules_python/releases/download/1.4.1/rules_python-1.4.1.tar.gz",
)

http_archive(
    name = "rules_shell",
    sha256 = "20721f63908879c083f94869e618ea8d4ff5edb91ff9a72a2ebee357fdbc352d",
    strip_prefix = "rules_shell-0.8.0",
    url = "https://github.com/bazel-contrib/rules_shell/releases/download/v0.8.0/rules_shell-v0.8.0.tar.gz",
)

register_toolchains("//:py_toolchain")

load("//test:external_data_workspace_test.bzl", "add_external_data_test_repositories")

add_external_data_test_repositories(__workspace_dir__)
