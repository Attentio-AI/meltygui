"""File-type codec routing for the folder-tree windows: extension registry
normalization, the codec_for_path content sniff, ImageCodec's off-thread
decode, the binary fallback summary, and the gentler write gate for files
outside the project tree.

Run from repo root with the project venv:
    venv/bin/python -m pytest tests/test_file_type_codecs.py -s
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path

from PIL import Image

from src.lsd.gl_gui.view.core_conversion.address import (
    Address, is_editable_source, is_writable_file)
from src.lsd.gl_gui.view.core_conversion.new_codecs import (
    extension_to_codec, codec_for_path,
    ImageCodec, BinaryFileCodec, TextFileCodec)
from src.shader_library.shader_manager.texture_manager import PendingTexture


def test_extension_registry_is_normalized():
    # the old ext ".png" registered 'p', 'n', 'g' char by char
    assert "p" not in extension_to_codec
    assert extension_to_codec[".png"] is ImageCodec
    assert extension_to_codec[".jpeg"] is ImageCodec
    assert extension_to_codec[".py"] is TextFileCodec


def test_codec_for_path_extension_case_insensitive(tmp_path):
    p = tmp_path / "PHOTO.PNG"
    p.write_bytes(b"")
    assert codec_for_path(p) is ImageCodec


def test_codec_for_path_sniffs_text(tmp_path):
    p = tmp_path / "no_extension_notes"
    p.write_text("just some plain notes\nwith two lines")
    assert codec_for_path(p) is TextFileCodec


def test_codec_for_path_sniffs_binary(tmp_path):
    p = tmp_path / "8481f6"   # the stress-folder shape: hash name, no ext
    p.write_bytes(bytes(range(256)) * 4)
    assert codec_for_path(p) is BinaryFileCodec


def test_codec_for_path_unknown_ext_still_sniffs(tmp_path):
    p = tmp_path / "data.int"
    p.write_text("1 2 3 4")
    assert codec_for_path(p) is TextFileCodec


def test_image_codec_decodes_off_thread(tmp_path):
    p = tmp_path / "tiny.png"
    Image.new("RGBA", (4, 2), (255, 0, 0, 255)).save(p)
    address = Address(p)
    pending = ImageCodec.load(address)
    assert isinstance(pending, PendingTexture)
    assert (pending.tex_width, pending.tex_height) == (4, 2)
    assert pending.texture_id is None          # upload deferred to GL thread
    assert len(pending.data) == 4 * 2 * 4      # RGBA bytes decoded
    assert ImageCodec.save(pending, p) is False  # read-only


def test_binary_codec_summary(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"\x00\x01\x02ABC" + b"\xff" * 500)
    summary = BinaryFileCodec.load(Address(p))
    assert "blob.bin" in summary and "506 bytes" in summary
    assert "00 01 02 41 42 43" in summary      # hex row
    assert "ABC" in summary                     # ascii column
    assert "more bytes" in summary              # truncation message
    assert BinaryFileCodec.save(summary, p) is False


def test_code_buttons_only_for_python(tmp_path):
    py, txt = tmp_path / "mod.py", tmp_path / "notes.txt"
    py.write_text("x = 1"), txt.write_text("notes")
    assert TextFileCodec.show_code_buttons(Address(py))
    assert not TextFileCodec.show_code_buttons(Address(txt))
    assert not ImageCodec.show_code_buttons(Address(txt))
    assert not BinaryFileCodec.show_code_buttons(Address(txt))
    assert not TextFileCodec.show_code_buttons(None)  # unresolved → no buttons
    from src.lsd.gl_gui.view.core_conversion.new_codecs import TypeCodec
    assert TypeCodec.show_code_buttons(Address(py))   # live-Python codecs keep them


def test_writable_gate_outside_project():
    home = Path.home()
    assert is_writable_file(home / "test_folder" / "notes.txt")
    assert not is_editable_source(home / "test_folder" / "notes.txt")  # code buttons more strict
    assert not is_writable_file(home / "venv" / "site-packages" / "x.py")
    assert not is_writable_file("/etc/passwd")


def test_text_codec_resolves_external_file(tmp_path, monkeypatch):
    # tmp_path is usually under /tmp (outside project) - point home at it so the
    # gate logic, not the test environment, is what's exercised.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    p = tmp_path / "external.txt"
    p.write_text("editable")
    address = TextFileCodec.resolve_address(p)
    assert address is not None
    assert address._allow_write          # write guard opt-in for non-project files
    assert TextFileCodec.load(address) == "editable"


if __name__ == "__main__":
    print("run via pytest (uses tmp_path/monkeypatch fixtures)")
