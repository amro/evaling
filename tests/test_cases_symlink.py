"""Absolute attachment paths normalize (symlinks resolved) like relative ones."""

from evaling.config.cases import load_cases

from .test_cases import config_with


def test_absolute_files_are_symlink_resolved(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "dog.jpg").write_bytes(b"x")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    cfg = config_with([{"files": {"photo": str(link / "dog.jpg")}}], tmp_path)
    [case] = load_cases(cfg)
    assert case.files["photo"] == str((real / "dog.jpg").resolve())
