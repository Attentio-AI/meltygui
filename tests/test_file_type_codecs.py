"""File-type codec routing for the folder-tree windows: extension registry
normalization, the codec_for_path content sniff, ImageCodec's off-thread
decode, the binary fallback summary, and the gentler write gate for files
outside the project tree.

Run from repo root with the project venv:
    venv/bin/python -m pytest tests/test_file_type_codecs.py -s
"""
import sys
import os


from pathlib import Path

from PIL import Image

from meltygui.code.fileref import Address
from meltygui.code.fileref import is_editable_source
from meltygui.code.fileref import is_writable_file
from meltygui.code.new_codecs import extension_to_codec
from meltygui.code.new_codecs import codec_for_path
from meltygui.code.new_codecs import ImageCodec
from meltygui.code.new_codecs import BinaryFileCodec
from meltygui.code.new_codecs import TextFileCodec
from meltygui.graphics.texture_manager import PendingTexture


def test_extension_registry_is_normalized():
    # the old ext ".png" registered 'p', 'n', 'g' char by char
    assert "p" not in extension_to_codec
    assert extension_to_codec[".png"] is ImageCodec
    assert extension_to_codec[".jpeg"] is ImageCodec
    assert extension_to_codec[".py"] is TextFileCodec


def test_file_icons_are_distinct_supported_and_do_not_read_files(monkeypatch):
    from meltygui.code.codec_registry import file_icon_for_path
    from meltygui.model.icon_model import FA_ICONS

    def unexpected_io(*args, **kwargs):
        raise AssertionError("Icon lookup must not inspect files")

    monkeypatch.setattr(Path, "stat", unexpected_io)
    monkeypatch.setattr(Path, "open", unexpected_io)
    icons = [file_icon_for_path(f"/missing/file.{ext}")
             for ext in ("png", "jpeg", "txt", "py", "md")]
    assert len(set(icons)) == 5
    supported = {glyph for group in FA_ICONS.values() for glyph in group.values()}
    assert all(icon in supported for icon in icons)
    assert file_icon_for_path("PHOTO.JPG") == icons[1]
    assert file_icon_for_path("PHOTO.PNG") == icons[0]
    assert file_icon_for_path("unknown.whatever") is None


def test_file_rows_preserve_custom_icons_and_folder_fallback():
    from meltygui.files.fast_file_explorer import row_icon
    path = Path("photo.png")
    assert row_icon(path, False, None, "folder", "file") == ImageCodec.icon_for_path(path)
    assert row_icon(path, False, {"icon": "custom"}, "folder", "file") == "custom"
    assert row_icon(path, True, None, "folder", "file") == "folder"
    assert row_icon(Path("unknown.bin"), False, None, "folder", "file") == "file"


def test_codec_badges_keep_extension_and_accent_separate_from_file_tint(monkeypatch):
    from meltygui.code.codec_registry import file_badge_for_path

    def unexpected_io(*args, **kwargs):
        raise AssertionError("Badge lookup must not inspect files")

    monkeypatch.setattr(Path, "stat", unexpected_io)
    monkeypatch.setattr(Path, "open", unexpected_io)
    badges = {ext: file_badge_for_path(f"file.{ext.upper()}")
              for ext in ("png", "jpeg", "jpg", "txt", "md", "py")}
    assert all(badge[0] == ext.upper() for ext, badge in badges.items())
    assert badges["jpeg"][1] == badges["jpg"][1]
    assert badges["png"][1][1] > badges["png"][1][0]
    assert badges["jpeg"][1][0] > badges["jpeg"][1][1]
    assert badges["md"][1][0] < badges["jpeg"][1][0]
    assert badges["txt"][1][0] > badges["txt"][1][2]
    assert badges["py"][2] == "python"
    assert file_badge_for_path("unknown.bin") is None


def test_codec_for_path_extension_case_insensitive(tmp_path):
    p = tmp_path / "PHOTO.PNG"
    Image.new("RGB", (2, 2)).save(p, format="PNG")
    assert codec_for_path(p) is ImageCodec


def test_image_codec_vetoes_empty_file(tmp_path):
    # old startup crash: a 0-byte "image" routed to ImageCodec made every
    # watch-triggered load throw UnidentifiedImageError. Empty bytes decode
    # as utf-8, so the file lands on text.
    p = tmp_path / "test (4th copy).png"
    p.write_bytes(b"")
    assert codec_for_path(p) is TextFileCodec


def test_image_codec_vetoes_non_image_bytes(tmp_path):
    p = tmp_path / "garbage.png"
    p.write_bytes(b"\x00\xff" * 300)              # not a PNG, not utf-8
    assert codec_for_path(p) is BinaryFileCodec
    p.write_text("actually notes someone misnamed")
    assert codec_for_path(p) is TextFileCodec     # claims re-probes on change


def test_image_codec_claims_missing_file(tmp_path):
    # existence is resolve_address's job, not the content veto's
    assert codec_for_path(tmp_path / "nope.png") is ImageCodec


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
    from meltygui.code.new_codecs import TypeCodec
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


# ── code_file_io's view resolution + the read-only contract ─────────────────
#
# "The codec decides the data type; the type decides the view." A new file
# codec is ONE codec: load() returns a value whose type has a default renderer
# (is_default_for) and code_file_io replaces it to draw_any. Tests pin the
# resolution order in _codec_view and the high-level flags the editor and
# code_file_io read (editable / icon).

def test_codec_view_keeps_render_host_capture():
    # The host's capture view_func materializes the value into host["value"]
    # (what draw_code_editor reads). Overriding capture with the codec's view
    # skipped materialization - an image host stuck on "Loading..." forever.
    from meltygui.code.new_converters import _codec_view
    from meltygui.code.new_converters import code_file_io
    from meltygui.core.conversion.render_host import RenderHost
    host = RenderHost(io_function=code_file_io, input_value=Path("/tmp/x.png"),
                      name="##test_codec_view_host", evictable=True)
    try:
        pending = PendingTexture(name="x", tex_width=1, tex_height=1, gl_format=0, data=b"")
        capture = host._internal_view_func   # bound once; each access binds anew
        assert _codec_view(ImageCodec, pending, capture) is capture
        assert _codec_view(TextFileCodec, "text", capture) is capture
    finally:
        host.remove()


def test_codec_view_routes_by_type():
    from meltygui.code.new_converters import _codec_view
    from meltygui.core.rendering.render_dispatch import draw_any

    def text_view(**kw):
        return False, None

    # str → whatever text view the caller wired (mode-pinned editor)
    assert _codec_view(TextFileCodec, "source", text_view) is text_view
    assert _codec_view(BinaryFileCodec, "hexdump", text_view) is text_view
    # anything else → draw_any (is_default_for func); ImageCodec declares
    # no view_func because PendingTexture already has a default renderer
    pending = PendingTexture(name="x", tex_width=1, tex_height=1, gl_format=0, data=b"")
    assert ImageCodec.view_func is None
    assert _codec_view(ImageCodec, pending, text_view) is draw_any


def test_codec_view_explicit_override_wins():
    from meltygui.code.new_converters import _codec_view
    from meltygui.code.new_codecs import Codec
    from meltygui.code.new_codecs import register_codec

    def pinned(**kw):
        return False, None

    def text_view(**kw):
        return False, None

    class PinnedCodec(Codec):
        view_func = staticmethod(pinned)

    assert _codec_view(PinnedCodec, "even a str", text_view) is pinned
    assert _codec_view(PinnedCodec, object(), text_view) is pinned


def test_read_only_codecs_declare_it():
    # code_file_io reads Codec.editable: False = ignore view edits, never arm
    # a save, reload external writes outright (no merge conflict).
    from meltygui.code.new_codecs import Codec
    assert Codec.editable is True
    assert ImageCodec.editable is False
    assert BinaryFileCodec.editable is False
    assert TextFileCodec.editable is True
    assert ImageCodec.save(None, None) is False




# ── the search Code tab: asset files ride the codec registry ─────────────

def test_asset_extensions_are_the_non_text_codecs():
    from meltygui.code.new_codecs import asset_extensions
    exts = asset_extensions()
    assert ".png" in exts and ".jpg" in exts
    assert ".py" not in exts and ".md" not in exts     # TextFileCodec's


