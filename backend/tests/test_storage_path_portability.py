"""A Windows-shaped path must mean the same thing on every platform.

These guards read a database value that an older row, an operator import, or a
tampered API payload could control, so they must refuse anything that means
"absolute" on EITHER operating system. ``C:/escape/x.png`` is absolute on Windows
and an ordinary relative directory on POSIX: a guard written against the host's
own ``pathlib`` therefore rejects it in local development and accepts it on the
Linux runner. That is not a cosmetic difference -- the same stored row is
readable on one platform and refused on the other.

The rule encoded here: a storage key or mock reference containing a Windows
drive component (``C:``) is refused everywhere. Such a key is never a legitimate
workspace-relative name, and treating it as a plain directory name is exactly the
ambiguity that made the two platforms disagree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import storage as st


@pytest.fixture()
def storage_root(tmp_path, monkeypatch):
    root = tmp_path / "videos"
    root.mkdir()
    monkeypatch.setattr(st, "STORAGE_ROOT", root)
    return root


@pytest.mark.parametrize("key,expected", [
    ("C:/abs/ws/x.png", True),
    ("c:/abs/ws/x.png", True),
    ("D:\\abs\\ws\\x.png", True),
    ("masks/C:/abs/x.png", True),
    ("masks/run-1/frame.png", False),
    ("videos/clip.mp4", False),
])
def test_drive_component_detection_is_platform_independent(key: str, expected: bool):
    # A workspace-RELATIVE key may not carry a drive anywhere.
    """The drive check itself must not depend on the host's pathlib.

    ``pathlib`` on Windows already refuses ``C:/...`` because resolving it changes
    drive; on POSIX the same string is an ordinary relative directory. Asserting
    the predicate directly is what makes the gap observable on a Windows
    developer machine instead of only on the Linux runner.
    """
    assert st.has_drive_component(key) is expected


@pytest.mark.parametrize("value,expected", [
    # An absolute path may legitimately START with a Windows drive ...
    ("C:/data/videos/ws-1/clip.mp4", False),
    ("C:\\data\\videos\\ws-1\\clip.mp4", False),
    # ... but a drive embedded mid-path is never honest.
    ("masks/C:/abs/x.png", True),
    ("masks/run-1/C:/x.png", True),
])
def test_leading_drive_is_allowed_only_where_an_absolute_path_is_expected(
    value: str, expected: bool
):
    assert st.has_drive_component(value, allow_leading=True) is expected


@pytest.mark.parametrize("key", [
    "C:/abs/ws/x.png",
    "c:/abs/ws/x.png",
    "D:\\abs\\ws\\x.png",
    "masks/C:/abs/x.png",
])
def test_validate_storage_key_refuses_a_windows_drive_component(key: str):
    """A drive component is an escape attempt, not a directory name."""
    assert st.validate_storage_key("ws-1", key) is None


def test_validate_storage_key_still_accepts_ordinary_keys():
    assert st.validate_storage_key("ws-1", "masks/run-1/frame.png") == "masks/run-1/frame.png"
    assert st.validate_storage_key("ws-1", "videos/clip.mp4") == "videos/clip.mp4"


def test_managed_path_refuses_an_embedded_drive_component(storage_root, monkeypatch):
    monkeypatch.chdir(storage_root)
    assert st.managed_path("ws-1", "masks/C:/abs/ws-1/clip.mp4") is None


def test_managed_path_still_resolves_a_real_absolute_windows_file(storage_root):
    """A leading drive is the ordinary spelling of an absolute file, not an escape."""
    inside = storage_root / "ws-1" / "clip.mp4"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"x")
    assert st.managed_path("ws-1", str(inside)) == inside.resolve()


def test_mock_render_spec_refuses_a_windows_drive_component(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "MOCK_ROOT", tmp_path / "mock")
    assert st.mock_render_spec_path("mock:abs/C:/evil/final-1.mp4") is None
    # The legitimate form still resolves.
    assert st.mock_render_spec_path("mock:abc/final-1.mp4") is not None


def test_sqlite_dsn_keeps_its_absolute_posix_path():
    """A ``sqlite:////abs/path`` DSN names an absolute file, not a relative one.

    ``urlsplit`` reports ``//abs/path``; stripping every leading slash turns a
    rooted file into one relative to the cwd, and the backup tool then reports a
    missing database for a file that exists.
    """
    from app.scripts.backup_restore import sqlite_file_path

    assert sqlite_file_path("sqlite:////tmp/ymoney/source.sqlite3") == "/tmp/ymoney/source.sqlite3"
    assert sqlite_file_path("sqlite:///tmp/ymoney/source.sqlite3") == "/tmp/ymoney/source.sqlite3"
    # Windows spelling: the authority separator plus the drive root.
    assert sqlite_file_path("sqlite:///C:/data/ymoney.sqlite3") == "C:/data/ymoney.sqlite3"
    assert sqlite_file_path("sqlite://") == ""


def test_sqlite_file_path_agrees_with_pathlib_for_the_paths_it_must_open(tmp_path):
    from app.scripts.backup_restore import sqlite_file_path

    for name in ("source.sqlite3", "nested/inner.sqlite3"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")
        dsn = f"sqlite:///{target.as_posix()}"
        resolved = Path(sqlite_file_path(dsn))
        assert resolved.exists(), f"{dsn} must resolve to an existing file"
        assert resolved == target