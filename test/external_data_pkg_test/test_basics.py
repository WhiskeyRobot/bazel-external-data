#!/usr/bin/env python3

import os
from os.path import basename
import subprocess
import unittest


def subshell(cmd):
    return subprocess.check_output(cmd, shell=True).decode("utf8").strip()


expected_files = {
    "master_files": [
        "basic.bin",
        "glob_1.bin",
        "glob_2.bin",
        "glob_3.bin",
    ],
    "extra_files": [
        "extra.bin",
    ],
}

archive_files = [
    "archive/a.bin",
    "archive/b.bin",
    "archive/subdir/c.bin",
]

data_dir = 'data'
mock_dir = 'mock'


def find_runfile(candidates):
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    search_roots = ["."]
    runfiles_dir = os.environ.get("RUNFILES_DIR")
    if runfiles_dir:
        search_roots.append(runfiles_dir)
    for search_root in search_roots:
        for root, _, filenames in os.walk(search_root):
            for filename in filenames:
                path = os.path.normpath(os.path.join(root, filename))
                if any(path.endswith(candidate) for candidate in candidates):
                    return path
    raise FileNotFoundError(candidates)


class TestBasics(unittest.TestCase):
    def test_files(self):
        # Go through each file and ensure that we have the desired contents.
        files = subshell("find data -name '*.bin' | sort")
        for file in files.split('\n'):
            with open(file) as f:
                contents = f.read()
            file_name = os.path.basename(file)

            mock_contents = None
            for mock_name, mock_file_names in expected_files.items():
                if file_name in mock_file_names:
                    mock_file = os.path.join(mock_dir, mock_name, file_name)
                    with open(mock_file) as f:
                        mock_contents = f.read()
                    break
            if mock_contents is None:
                for archive_file in archive_files:
                    if "data/" + archive_file == file:
                        mock_contents = "Contents of '{}'".format(file_name)
                        break
                else:
                    print("Skipping: {}".format(file))
            else:
                self.assertEqual(contents, mock_contents)

    def test_executable(self):
        output = subshell("data/executable")
        self.assertEqual(output, "Hello")

    def test_repository_rules(self):
        files = [
            "test_data/a.bin",
            "test_data/b.bin",
            "test_data/subdir/c.bin",
        ]
        repo_prefixes = [
            "external/repo_archive",
            "repo_archive",
        ]
        for file in files:
            candidates = [
                os.path.join(prefix, file) for prefix in repo_prefixes]
            file = find_runfile(candidates)
            with open(file) as f:
                c = f.read()
                expected = "Content for '{}'\n".format(basename(file))
                self.assertEqual(c, expected)


if __name__ == '__main__':
    unittest.main()
