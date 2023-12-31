# Copyright (c) 2022 Graphcore Ltd. All rights reserved.
import pathlib
from shutil import copy

import pytest
from filelock import FileLock

# NOTE: The import below is dependent on 'pytest.ini' in the root of
# the repository
import tutorials_tests.testing_util as testing_util

working_path = pathlib.Path(__file__).parent.absolute()


@pytest.fixture(scope="session")
def make_executable():
    with FileLock(__file__ + ".lock"):
        testing_util.run_command_fail_explicitly(["make", "all"], working_path)


def launch(tmp_path, executable):
    copy(working_path / executable, tmp_path / executable)
    testing_util.run_command_fail_explicitly([executable], tmp_path)


@pytest.mark.category1  # category1: test < 5 minutes
@pytest.mark.ipus(2)
def test_example_1(tmp_path, make_executable):
    """
    Usage of the Block program
    """
    launch(tmp_path, "./example_1")


@pytest.mark.category1  # category1: test < 5 minutes
@pytest.mark.ipus(1)
def test_example_2(tmp_path, make_executable):
    """
    Profiling individual programs with implicit Blocks
    """
    launch(tmp_path, "./example_2")


@pytest.mark.category1  # category1: test < 5 minutes
@pytest.mark.ipus(1)
def test_example_3(tmp_path, make_executable):
    """
    Profiling I/O operations
    """
    launch(tmp_path, "./example_3")


@pytest.mark.category1  # category1: test < 5 minutes
@pytest.mark.ipus(1)
def test_example_3_a(tmp_path, make_executable):
    """
    Profiling I/O operations generated by the profiling itself
    """
    launch(tmp_path, "./example_3_a")
