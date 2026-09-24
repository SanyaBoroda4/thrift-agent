"""pipeline.ready_folders: the prod entry point. A share is picked up only once the Shortcut's marker is there,
iCloud has nothing left to download, and the folder has been quiet for the settle window."""
import os
import time

from PIL import Image

from thrift_agent import pipeline
from thrift_agent.config import Settings, settings

OLD = time.time() - 3600


def _settings(tmp_path):
    base = settings().data
    data = {**base, "paths": {k: str(tmp_path / k) for k in base["paths"]}}
    data["paths"]["db"] = str(tmp_path / "state.db")
    s = Settings(data)
    s.ensure_dirs()
    return s


def _share(s, name, marker="_done.txt", photo=True, age=OLD):
    d = s.path("inbox") / name
    d.mkdir(parents=True)
    if photo:
        Image.new("RGB", (60, 80), "red").save(d / "IMG_1.jpg")
    if marker:
        (d / marker).touch()
    if age is not None:
        for f in d.iterdir():
            os.utime(f, (age, age))
    return d


def test_finished_share_is_ready(tmp_path):
    s = _settings(tmp_path)
    d = _share(s, "2026-09-21_1432")                              # "_done.txt": the marker's stem still matches
    assert pipeline.ready_folders(s) == [d]
    assert pipeline.ready_folders(_settings(tmp_path / "empty")) == []


def test_share_still_downloading_from_icloud_waits(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    d = _share(s, "2026-09-21_1432")
    (d / ".IMG_2.jpg.icloud").touch()                             # not yet evicted from the cloud
    runs = []
    monkeypatch.setattr(pipeline.subprocess, "run", lambda *a, **k: runs.append(a[0]))

    monkeypatch.setattr(pipeline.platform, "system", lambda: "Windows")
    assert pipeline.ready_folders(s) == [] and runs == []         # dev never shells out to brctl

    monkeypatch.setattr(pipeline.platform, "system", lambda: "Darwin")
    assert pipeline.ready_folders(s) == []
    assert runs == [["brctl", "download", str(d)]]                # the Mac asks iCloud for the rest

    (d / ".IMG_2.jpg.icloud").unlink()
    Image.new("RGB", (60, 80), "blue").save(d / "IMG_2.jpg")
    os.utime(d / "IMG_2.jpg", (OLD, OLD))
    assert pipeline.ready_folders(s) == [d]


def test_share_without_marker_waits(tmp_path):
    s = _settings(tmp_path)
    _share(s, "2026-09-21_1432", marker=None)
    assert pipeline.ready_folders(s) == []


def test_share_still_being_written_waits(tmp_path):
    s = _settings(tmp_path)
    d = _share(s, "2026-09-21_1432", age=None)                    # files just written
    assert s["inbox"]["settle_seconds"] > 0 and pipeline.ready_folders(s) == []
    for f in d.iterdir():                                          # ...then quiet for longer than the settle window
        os.utime(f, (OLD, OLD))                                    # (a 0 s settle is flaky: mtime can lead time.time())
    assert pipeline.ready_folders(s) == [d]


def test_hidden_dirs_and_loose_files_are_ignored(tmp_path):
    s = _settings(tmp_path)
    _share(s, ".hidden")                                          # complete, but a dotfolder (iCloud/Finder junk)
    (s.path("inbox") / "stray.jpg").touch()
    assert pipeline.ready_folders(s) == []
