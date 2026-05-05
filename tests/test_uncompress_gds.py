"""Regression: uncompress_gds must surface real make failures, not swallow them.

The previous implementation captured stderr/stdout into PIPE and threw them
away, used ``subprocess.run`` without ``check=True`` (so its
``except CalledProcessError`` block was dead code), and returned silently on
ANY make failure. That meant Fargate-only failures (e.g. disk pressure during
gunzip on a heavy repo) bubbled up as the misleading downstream error
"A single valid GDS was not found" with no information about why. See
chipfoundry/cf-cli#20.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from cf_precheck.config import uncompress_gds


def _completed(returncode: int, stderr: bytes = b"", stdout: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args="make ... uncompress",
        returncode=returncode,
        stderr=stderr,
        stdout=stdout,
    )


def test_success_when_gds_files_present_regardless_of_exit_code(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Caravel-lite Makefile legitimately exits 2 on the mag/ step after the
    gds/ step succeeds. As long as gds/*.gds is on disk, treat as success."""
    (tmp_path / "gds").mkdir()
    (tmp_path / "gds" / "user_project_wrapper.gds").write_bytes(b"GDSII")
    (tmp_path / "gds" / "other.gds").write_bytes(b"GDSII")

    with patch.object(
        subprocess, "run",
        return_value=_completed(2, stderr=b"gzip: mag/user_project_wrapper.mag already exists"),
    ), caplog.at_level(logging.INFO):
        uncompress_gds(tmp_path, Path("/opt/caravel"))

    info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert any("produced 2 gds/*.gds file" in m for m in info_msgs), info_msgs
    assert any("user_project_wrapper.gds" in m for m in info_msgs), info_msgs
    assert any("other.gds" in m for m in info_msgs), info_msgs
    assert any("make exit 2" in m for m in info_msgs), info_msgs


def test_critical_with_stderr_when_make_fails_and_no_gds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Real make failure (e.g. disk full, missing tool) on a repo with no
    pre-existing gds/*.gds — surface stderr + returncode, then exit 252."""
    (tmp_path / "gds").mkdir()
    stderr = b"gunzip: write error: No space left on device\n"

    with patch.object(
        subprocess, "run", return_value=_completed(2, stderr=stderr),
    ), caplog.at_level(logging.CRITICAL):
        with pytest.raises(SystemExit) as excinfo:
            uncompress_gds(tmp_path, Path("/opt/caravel"))

    assert excinfo.value.code == 252
    crit_msgs = [r.message for r in caplog.records if r.levelno == logging.CRITICAL]
    assert any("make uncompress failed" in m for m in crit_msgs), crit_msgs
    assert any("No space left on device" in m for m in crit_msgs), crit_msgs
    assert any("exit 2" in m for m in crit_msgs), crit_msgs


def test_critical_when_make_succeeds_but_no_gds_produced(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """make exits 0 (e.g. no .gds.gz inputs to operate on) but no .gds files
    appear: cf-precheck cannot proceed; surface a clear "produced no gds"
    message instead of the misleading downstream "no valid GDS" error."""
    (tmp_path / "gds").mkdir()

    with patch.object(
        subprocess, "run", return_value=_completed(0, stdout=b"Nothing to do for 'uncompress'."),
    ), caplog.at_level(logging.CRITICAL):
        with pytest.raises(SystemExit) as excinfo:
            uncompress_gds(tmp_path, Path("/opt/caravel"))

    assert excinfo.value.code == 252
    crit_msgs = [r.message for r in caplog.records if r.levelno == logging.CRITICAL]
    assert any("reported success" in m for m in crit_msgs), crit_msgs
    assert any("produced no" in m for m in crit_msgs), crit_msgs


def test_missing_gds_dir_treated_as_no_gds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If gds/ doesn't exist at all (malformed repo), still surface a clear
    error rather than silently passing or KeyError'ing later."""
    with patch.object(
        subprocess, "run",
        return_value=_completed(0, stdout=b"make: *** No rule to make target 'uncompress'."),
    ), caplog.at_level(logging.CRITICAL):
        with pytest.raises(SystemExit) as excinfo:
            uncompress_gds(tmp_path, Path("/opt/caravel"))

    assert excinfo.value.code == 252
