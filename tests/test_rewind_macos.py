"""Rewind footprint controls: size-cap backstop in _prune.

The main footprint lever (downscale via `sips`) is measured empirically, not
unit-tested here (it shells out to a macOS-only binary). What we *can* and must
test in isolation is the size-cap prune logic: given a rewind dir whose frames
exceed MAX_TOTAL_MB, _prune must delete the oldest frames (files + DB rows)
until back under the cap, and never touch newer frames it doesn't need to.
"""

from __future__ import annotations

import time

import pytest

from sunday.devices import rewind_macos as rw


def _seed_frames(conn, day_dir, n, size_bytes, start_ts):
    """Create n fake frames: a real on-disk .jpg of `size_bytes` and a matching
    DB row, timestamped start_ts, start_ts+1, ... (oldest first)."""
    day_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(n):
        ts = start_ts + i
        p = day_dir / f"{i:06d}.jpg"
        p.write_bytes(b"\x00" * size_bytes)
        conn.execute(
            "INSERT INTO frames (ts, image_path, content_hash, ocr_text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, str(p), f"hash{i}", f"text {i}", ts),
        )
        paths.append(p)
    conn.commit()
    return paths


@pytest.mark.asyncio
async def test_prune_size_cap_removes_oldest_until_under(tmp_path, monkeypatch):
    # Tiny cap so we can reason in whole MB. 5 frames @ 1 MB = 5 MB total,
    # cap = 3 MB → must drop the 2 oldest to land at 3 MB.
    monkeypatch.setattr(rw, "REWIND_DIR", tmp_path)
    monkeypatch.setattr(rw, "REWIND_DB", tmp_path / "rewind.db")
    monkeypatch.setattr(rw, "MAX_TOTAL_MB", 3)
    # Keep the time-based prune from interfering: large retention.
    monkeypatch.setattr(rw, "RETENTION_DAYS", 3650)

    conn = rw._connect()
    one_mb = 1024 * 1024
    now = time.time()
    paths = _seed_frames(conn, tmp_path / "2026-06-21", 5, one_mb, start_ts=now)

    assert rw._dir_total_bytes() == 5 * one_mb

    rw._prune(conn)

    # Back under the 3 MB cap.
    assert rw._dir_total_bytes() <= 3 * one_mb
    # The two OLDEST files are gone; the three newest survive.
    assert not paths[0].exists()
    assert not paths[1].exists()
    assert paths[2].exists()
    assert paths[3].exists()
    assert paths[4].exists()

    # DB rows for the deleted frames are gone; survivors remain.
    remaining = {r[0] for r in conn.execute("SELECT image_path FROM frames").fetchall()}
    assert str(paths[0]) not in remaining
    assert str(paths[1]) not in remaining
    assert str(paths[2]) in remaining
    assert str(paths[4]) in remaining
    assert conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_prune_size_cap_noop_when_under(tmp_path, monkeypatch):
    monkeypatch.setattr(rw, "REWIND_DIR", tmp_path)
    monkeypatch.setattr(rw, "REWIND_DB", tmp_path / "rewind.db")
    monkeypatch.setattr(rw, "MAX_TOTAL_MB", 400)
    monkeypatch.setattr(rw, "RETENTION_DAYS", 3650)

    conn = rw._connect()
    one_mb = 1024 * 1024
    now = time.time()
    paths = _seed_frames(conn, tmp_path / "2026-06-21", 3, one_mb, start_ts=now)

    rw._prune(conn)

    # Nothing removed — well under the 400 MB cap.
    assert all(p.exists() for p in paths)
    assert conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 3


@pytest.mark.asyncio
async def test_prune_size_cap_sweeps_orphan_day_folders(tmp_path, monkeypatch):
    # Frames captured but never indexed (no DB rows) must still be reclaimable
    # by the cap — otherwise an OCR-failure streak could pin the footprint high.
    monkeypatch.setattr(rw, "REWIND_DIR", tmp_path)
    monkeypatch.setattr(rw, "REWIND_DB", tmp_path / "rewind.db")
    monkeypatch.setattr(rw, "MAX_TOTAL_MB", 3)
    monkeypatch.setattr(rw, "RETENTION_DAYS", 3650)

    conn = rw._connect()
    one_mb = 1024 * 1024
    # Two day-folders of orphan frames (no DB rows), 3 MB each = 6 MB.
    for day in ("2026-06-20", "2026-06-21"):
        d = tmp_path / day
        d.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (d / f"{i:06d}.jpg").write_bytes(b"\x00" * one_mb)

    assert rw._dir_total_bytes() == 6 * one_mb

    rw._prune(conn)

    # Cap enforced even with zero indexed frames: oldest day-folder swept first.
    assert rw._dir_total_bytes() <= 3 * one_mb
    assert not (tmp_path / "2026-06-20").exists()
    assert (tmp_path / "2026-06-21").exists()
