import os
import time
from pathlib import Path

import pytest

from claude_code_auth_switch.role_prompt_cache import (
    RETENTION_SECONDS,
    STALE_TEMP_SECONDS,
    cache_dir,
    joined_prompt_file,
    remove_stale,
)


@pytest.fixture
def sources(tmp_path):
    first = tmp_path / "src/dispatcher.md"
    first.parent.mkdir()
    first.write_bytes(b"You dispatch.\n")
    second = tmp_path / "src/sdlc.md"
    second.write_bytes(b"Use sdlc.")
    return [str(first), str(second)]


def test_joins_the_sources_in_order_with_a_blank_line_and_a_final_newline(tmp_path, sources):
    joined = Path(joined_prompt_file("dispatcher", sources, tmp_path / "cache"))

    assert joined.read_bytes() == b"You dispatch.\n\nUse sdlc.\n"
    assert joined.parent == tmp_path / "cache" and joined.name.startswith("dispatcher-") and joined.suffix == ".md"


def test_the_file_and_a_new_directory_are_private(tmp_path, sources):
    joined = Path(joined_prompt_file("dispatcher", sources, tmp_path / "cache"))

    assert (joined.stat().st_mode & 0o777, joined.parent.stat().st_mode & 0o777) == (0o600, 0o700)


def test_an_existing_directory_keeps_its_mode(tmp_path, sources):
    (tmp_path / "cache").mkdir(mode=0o755)
    (tmp_path / "cache").chmod(0o755)

    joined_prompt_file("dispatcher", sources, tmp_path / "cache")

    assert (tmp_path / "cache").stat().st_mode & 0o777 == 0o755


def test_the_same_content_reuses_the_file_and_refreshes_its_mtime(tmp_path, sources):
    first = Path(joined_prompt_file("dispatcher", sources, tmp_path / "cache"))
    first.chmod(0o400)
    os.utime(first, (1000, 1000))

    again = Path(joined_prompt_file("dispatcher", sources, tmp_path / "cache"))

    assert again == first and first.stat().st_mtime > 1000
    assert first.stat().st_mode & 0o777 == 0o400  # not rewritten
    assert [path.name for path in (tmp_path / "cache").iterdir()] == [first.name]


def test_a_changed_source_gives_a_new_file_and_keeps_the_old_one(tmp_path, sources):
    old = Path(joined_prompt_file("dispatcher", sources, tmp_path / "cache"))
    Path(sources[1]).write_bytes(b"Use sdlc, always.\n")

    new = Path(joined_prompt_file("dispatcher", sources, tmp_path / "cache"))

    assert new != old and old.exists()
    assert new.read_bytes() == b"You dispatch.\n\nUse sdlc, always.\n"


def test_a_role_name_with_a_slash_stays_in_the_directory(tmp_path, sources):
    joined = Path(joined_prompt_file("../team/lead", sources, tmp_path / "cache"))

    assert joined.parent == tmp_path / "cache" and joined.name.startswith("-team-lead-")


def test_cache_dir_honours_xdg_cache_home_and_falls_back_to_home():
    assert cache_dir({"XDG_CACHE_HOME": "/xdg", "HOME": "/home/me"}) == Path("/xdg/cc-auth-switch/role-prompts")
    assert cache_dir({"HOME": "/home/me"}) == Path("/home/me/.cache/cc-auth-switch/role-prompts")
    assert cache_dir({"XDG_CACHE_HOME": "relative", "HOME": "/home/me"}) == Path(
        "/home/me/.cache/cc-auth-switch/role-prompts"
    )


def test_cleanup_removes_old_joined_files_and_stale_temporary_files_only(tmp_path):
    now = time.time()
    files = {
        "old-0123.md": now - RETENTION_SECONDS - 60,
        "recent-0123.md": now - RETENTION_SECONDS + 60,
        ".old-0123.md.abcd.tmp": now - STALE_TEMP_SECONDS - 60,
        ".fresh-0123.md.abcd.tmp": now - 60,
        "notes.txt": now - RETENTION_SECONDS - 60,
    }
    for name, mtime in files.items():
        (tmp_path / name).write_text("x")
        os.utime(tmp_path / name, (mtime, mtime))

    remove_stale(tmp_path, now)

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        ".fresh-0123.md.abcd.tmp",
        "notes.txt",
        "recent-0123.md",
    ]


def test_a_write_runs_the_cleanup(tmp_path, sources):
    cache = tmp_path / "cache"
    cache.mkdir()
    stale = cache / "old-0123.md"
    stale.write_text("x")
    os.utime(stale, (1000, 1000))

    joined_prompt_file("dispatcher", sources, cache)

    assert not stale.exists()


def test_cleanup_of_a_missing_directory_does_nothing(tmp_path):
    remove_stale(tmp_path / "missing", time.time())


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a directory without write permission")
def test_an_unwritable_directory_raises_and_leaves_no_temporary_file(tmp_path, sources):
    cache = tmp_path / "cache"
    cache.mkdir()
    cache.chmod(0o500)

    try:
        with pytest.raises(PermissionError):
            joined_prompt_file("dispatcher", sources, cache)
    finally:
        cache.chmod(0o700)
    assert list(cache.iterdir()) == []
