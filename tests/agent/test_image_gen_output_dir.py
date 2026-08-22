"""Tests for the ``image_gen.output_dir`` save-directory override.

``save_b64_image`` (and therefore every image_gen provider) writes to
``$HERMES_HOME/cache/images/`` by default; profiles can redirect generated
assets into a working directory via ``image_gen.output_dir`` in config.yaml.
"""

from __future__ import annotations

import base64

import pytest

from agent import image_gen_provider


# 1×1 transparent PNG — valid bytes for save_b64_image()
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6300010000000500010d0a2db40000000049454e44"
    "ae426082"
)


@pytest.fixture(autouse=True)
def _tmp_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


def _b64() -> str:
    return base64.b64encode(_PNG).decode()


def test_default_save_dir_without_override(tmp_path):
    saved = image_gen_provider.save_b64_image(_b64(), prefix="t")
    assert saved.parent == tmp_path / "cache" / "images"
    assert saved.read_bytes() == _PNG


def test_output_dir_override_redirects_saves(tmp_path):
    target = tmp_path / "workdir" / "assets" / "generated"
    (tmp_path / "config.yaml").write_text(
        f"image_gen:\n  output_dir: {target}\n"
    )
    saved = image_gen_provider.save_b64_image(_b64(), prefix="t")
    assert saved.parent == target
    assert saved.read_bytes() == _PNG


def test_blank_output_dir_falls_back_to_default(tmp_path):
    (tmp_path / "config.yaml").write_text('image_gen:\n  output_dir: "  "\n')
    saved = image_gen_provider.save_b64_image(_b64(), prefix="t")
    assert saved.parent == tmp_path / "cache" / "images"
