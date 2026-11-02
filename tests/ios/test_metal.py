"""Real host-Metal pixel tests; opt in after build_metal_test.py.

These execute the same C++ encoder and MSL shipped for device. They do not
substitute a simulator for the separate ARM64 iPhoneOS compile/link check.
"""
import os
import importlib.util
from pathlib import Path
from meltygui.platforms.ios import build_directory
import struct
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2] / "meltygui/platforms/ios"


@unittest.skipUnless(os.environ.get("MELTY_METAL_TEST") == "1", "requires compiled host Metal test extension")
class MetalPixels(unittest.TestCase):
    def setUp(self):
        extension, = (build_directory() / 'metal-test').glob('_melty_metal*.so')
        spec = importlib.util.spec_from_file_location('_melty_metal', extension)
        native = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(native)
        import meltygui_imgui as imgui
        from meltygui.core.graphics.metal_renderer import MetalRenderer
        native._test_initialize(str(build_directory() / "metal-test/Melty.metallib"),str(build_directory() / "metal-test/metal-programs.json"))
        self.native=native
        self.imgui=imgui
        self.context=imgui.create_context()
        io=imgui.get_io()
        io.ini_file_name=None
        io.display_size=(64,48)
        io.display_fb_scale=(1,1)
        io.delta_time=1/60
        self.renderer=MetalRenderer(native)
        self.renderer.refresh_font_texture()

    def tearDown(self):
        self.renderer.shutdown()
        self.imgui.destroy_context(self.context)

    def pixels(self, texture):
        self.native._test_flush()
        w,h,format=self.renderer._textures[texture]
        data=self.native._test_pixels(texture)
        code,channels={"rgba32f":("f",4),"rgba16f":("e",4),"r16":("H",1),"rgba8":("B",4)}[format]
        values=struct.unpack(f"{w*h*channels}{code}",data)
        return [[values[(y*w+x)*channels:(y*w+x+1)*channels] for x in range(w)] for y in range(h)]

    def test_quad_bounds_and_y_up_coordinates(self):
        texture=self.renderer.create_texture(8,8,"rgba32f")
        self.renderer.clear_rect(texture,(2,1,3,2),(0.25,0.5,1,1))
        image=self.pixels(texture)
        for y,row in enumerate(image):
            for x,pixel in enumerate(row):
                self.assertEqual(pixel,(0.25,0.5,1,1) if 2<=x<5 and 5<=y<7 else (0,0,0,0))

    def test_adjacent_draws_share_pass_and_preserve_blending_and_scissors(self):
        r, native = self.renderer, self.native
        native._test_flush()
        target = r.create_texture(8, 8, "rgba32f")
        native._test_flush()
        native.clear_texture(target, (0, 0, 0, 0))
        for _ in range(32):
            r.quad(target, "solid_fragment", struct.pack("4f", 1, .5, .25, .5),
                   rect=(0, 0, 8, 8), blend="over", clip=(2, 1, 3, 2))
        # A clear and all 32 overlapping rectangles are one attachment pass.
        self.assertEqual(native._test_flush(), 1)
        image = self.pixels(target)
        for y, row in enumerate(image):
            for x, pixel in enumerate(row):
                expected = (1, .5, .25, 1) if 2 <= x < 5 and 5 <= y < 7 else (0, 0, 0, 0)
                for actual, value in zip(pixel, expected):
                    self.assertAlmostEqual(actual, value, places=6)

    def test_pass_batching_preserves_target_clear_upload_and_frame_boundaries(self):
        r, native = self.renderer, self.native
        native._test_flush()
        a = r.create_texture(2, 2, "rgba32f")
        b = r.create_texture(2, 2, "rgba32f")
        native._test_flush()
        native.clear_texture(a, (1, 0, 0, 1))
        r.copy(a, b)
        native.clear_texture(a, (0, 1, 0, 1))
        r.copy(b, a, target_rect=(0, 0, 1, 2))
        self.assertEqual(native._test_flush(), 3)
        image = self.pixels(a)
        self.assertEqual(image[0], [(1, 0, 0, 1), (0, 1, 0, 1)])
        # An upload must finish the pass that sampled the old source first.
        r.copy(a, b)
        native.upload_texture(a, 2, 2, struct.pack("16f", *((0, 0, 1, 1) * 4)))
        r.copy(a, b, target_rect=(1, 0, 1, 2))
        self.assertEqual(native._test_flush(), 2)
        self.assertEqual(self.pixels(b)[0], [(1, 0, 0, 1), (0, 0, 1, 1)])
        # The same target on the next frame must get a fresh encoder.
        r.clear_rect(b, (0, 0, 2, 2), (.5, .5, .5, 1))
        self.assertEqual(native._test_flush(), 1)
        self.assertEqual(self.pixels(b)[0], [(.5, .5, .5, 1)] * 2)

    def test_present_keeps_pixels_at_top_left_across_keyboard_and_drawable_resizes(self):
        # Distinct pixels expose stretching, Y flips and old exposed pixels.
        # Exercise both directions, including a drawable acquired before a
        # simultaneous size change, through the real final presentation pass.
        r=self.renderer
        for sw,sh,dw,dh in ((12,8,12,20),(12,20,12,8),(12,8,20,16),(20,16,12,8),(12,8,12,8)):
            with self.subTest(scene=(sw,sh),drawable=(dw,dh)):
                values=[(x/32,y/32,.25,1) for y in range(sh) for x in range(sw)]
                scene=r.create_texture(sw,sh,"rgba16f",struct.pack(f"{sw*sh*4}e",*(c for p in values for c in p)))
                target=r.create_texture(dw,dh,"rgba16f")
                self.native.clear_texture(target,(1,0,1,1))
                self.native.set_scene(scene)
                self.native._test_present(target)
                for y,row in enumerate(self.pixels(target)):
                    for x,pixel in enumerate(row):
                        self.assertEqual(pixel,values[y*sw+x] if x<sw and y<sh else (0,0,0,1))
                r.delete_texture(scene)
                r.delete_texture(target)

    def test_rank_mask_copy_excludes_other_views(self):
        r=self.renderer
        source=r.create_texture(8,8,"rgba32f")
        r.native.clear_texture(source,(.2,.4,.8,.3))
        top=r.create_texture(8,8,"r16")
        sub=r.create_texture(8,8,"r16")
        tile=r.create_texture(8,8,"rgba32f")
        for target,rect,rank in ((top,(0,0,8,8),2/65535),(top,(0,0,4,8),1/65535),(sub,(0,0,4,8),1/65535)):
            r.draw("mask",target,rect=rect,uRankNorm=rank)
        r.draw("tile_copy",tile,uSrc=source,uTopMask=top,uSubMask=sub,
               uFBSize=(8,8),uSrcRectPx=(0,0,8,8),uDebugScale=1,uCopyDebugMode=0,uTint=(1,1,1,1))
        image=self.pixels(tile)
        for row in image:
            for x,pixel in enumerate(row):
                if x<4:
                    self.assertAlmostEqual(pixel[0],.2,places=5)
                    self.assertEqual(pixel[3],1)
                else:
                    self.assertEqual(pixel,(0,0,0,0))

    def test_resize_preserves_top_anchor_and_clears_new_bands(self):
        from types import SimpleNamespace
        from meltygui.core.graphics.metal_tiles import MetalTileBackend
        backend=MetalTileBackend(self.renderer)
        ds=SimpleNamespace(freeze_resize=False)
        tile=backend.ensure_tile(None,10,10,1,ds,(1,1))
        self.renderer.clear_rect(tile.tex,(0,self.renderer._textures[tile.tex][1]-10,10,10),(1,0,0,1))
        same=backend.ensure_tile(tile,12,12,2,ds,(1,1))
        self.assertIs(same,tile)
        image=self.pixels(tile.tex)
        self.assertEqual(image[0][0],(1,0,0,1))
        self.assertEqual(image[11][11],(0,0,0,0))
        grown=backend.ensure_tile(tile,34,34,3,ds,(1,1))
        image=self.pixels(grown.tex)
        self.assertEqual(image[0][0],(1,0,0,1))
        self.assertEqual(image[33][33],(0,0,0,0))
        backend.delete_tile(grown)

    def test_fractional_sizes_and_capture_phase_keep_resize_edges_clean(self):
        from types import SimpleNamespace
        from meltygui.core.graphics.metal_tiles import MetalTileBackend
        backend = MetalTileBackend(self.renderer)
        ds = SimpleNamespace(freeze_resize=False)
        tiny = backend.ensure_tile(None, .25, .375, 1, ds, (3, 3))
        try:
            self.assertEqual(tiny.size, (.25, .375))
            self.assertGreater(self.renderer._textures[tiny.tex][0], 0)
        finally:
            backend.delete_tile(tiny)
        tile = backend.ensure_tile(None, 10.125, 10.125, 1, ds, (3, 3))
        try:
            tile.capture_offset = (.375, .75)
            self.native.clear_texture(tile.tex, (1, 1, 1, 1))
            same = backend.ensure_tile(tile, 10.25, 10.25, 2, ds, (3, 3))
            self.assertIs(same, tile)
            self.assertEqual(tile.size, (10.25, 10.25))
            image = self.pixels(tile.tex)
            for row in image[:32]:
                self.assertTrue(all(pixel == (1, 1, 1, 1) for pixel in row[:31]))
                self.assertEqual(row[31], (0, 0, 0, 0))
        finally:
            backend.delete_tile(tile)

    def test_cached_images_move_by_quarter_physical_pixels(self):
        from meltygui.core.cache.tile_cache import _tile_image_uv
        from meltygui.core.graphics.metal_tiles import MetalTileBackend
        r, imgui = self.renderer, self.imgui
        backend = MetalTileBackend(r)
        for scale in (1, 2, 3):
            with self.subTest(scale=scale):
                tile = backend.ensure_tile(None, 8, 8, 1, None, (scale, scale))
                tile.capture_offset = (.375, .75)
                aw, ah, _ = r._textures[tile.tex]
                values = [v for y in range(ah) for x in range(aw) for v in (x % 2, 0, 0, 1)]
                r.redefine_texture(tile.tex, aw, ah, 'rgba32f', struct.pack(f'{len(values)}f', *values))
                io = imgui.get_io()
                io.display_size, io.display_fb_scale = (48, 48), (scale, scale)
                previous = None
                for offset in (0, .25, .5, .75, 1):
                    x, y = 16 + (.375 + offset) / scale, 16 + .75 / scale
                    r.begin_scene(48 * scale, 48 * scale, (0, 0, 0, 0))
                    r.draw_backgrounds([], (0, 0, 0, 0))
                    imgui.new_frame()
                    uv_a, uv_b = _tile_image_uv(tile, (x, y), align_to_pixels=False)
                    imgui.get_background_draw_list().add_image(tile.tex, (x, y), (x+8, y+8),
                                                               uv_a, uv_b)
                    r.begin_frame_split()
                    imgui.render()
                    r.render_except_overlay(imgui.get_draw_data())
                    r.end_scene()
                    pixels = self.pixels(r.scene_framebuffer)
                    row = []
                    for screen_x in range(16 * scale + 2, 24 * scale - 2):
                        sample = screen_x - 16 * scale - offset
                        base = int(sample)
                        fraction = sample - base
                        expected = (base % 2) * (1-fraction) + ((base+1) % 2) * fraction
                        value = pixels[20 * scale][screen_x][0]
                        self.assertAlmostEqual(value, expected, delta=.002)
                        row.append(value)
                    if previous is not None:
                        self.assertNotEqual(row, previous)
                    previous = row
                backend.delete_tile(tile)

    def test_shadow_strip_and_glow_execute(self):
        r=self.renderer
        mask=r.create_texture(8,8,"r16")
        win=r.create_texture(8,8,"r16")
        vertices=struct.pack("9f",1,1,.25,6,1,.25,1,6,.25)
        r.native.shape(mask,vertices,win,1,"max",None)
        image=self.pixels(mask)
        self.assertGreater(image[5][2][0],15000)
        self.assertEqual(image[0][7][0],0)
        from types import SimpleNamespace
        from meltygui.core.graphics.metal_tiles import MetalTileBackend
        from meltygui.core.cache.tile_cache import TileCacheMasked
        cache=TileCacheMasked(gpu=MetalTileBackend(r))
        cache.gpu.begin_masks(cache,8,8)
        cache._win_mask_tex=win
        mark=(2,2,2,2,(1,.5,.25),.5,0,0,3,1,0,None)
        cache.gpu.stamp_glows(cache,[(mark,(0,0),1,0,None,1)],0,0,1,1,8,8)
        glow=self.pixels(cache.glow_tex)
        self.assertGreater(max(p[0] for row in glow for p in row),0)
        cache.cleanup()

    def test_resources_are_owned_by_render_thread_and_indices_are_bounded(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.native.create_texture,1,1,"rgba8",None)
            with self.assertRaisesRegex(RuntimeError,"render thread"):
                future.result()
        target=self.renderer.create_texture(8,8)
        vertex=struct.pack("4fI",0,0,0,0,0xffffffff)
        params=struct.pack("12f",8,8,1,1,1,1,1,0,0,1,1,-1)
        tex=self.renderer._font_texture
        with self.assertRaisesRegex(ValueError,"vertex buffer"):
            self.native.mesh(target,vertex,struct.pack("I",1),[(tex,0,1,0,0,8,8,-1)],params,tex,target,target)
        with self.assertRaisesRegex(ValueError,"index buffer"):
            self.native.mesh(target,vertex,struct.pack("I",0),[(tex,sys.maxsize,sys.maxsize,0,0,8,8,-1)],params,tex,target,target)

    def test_cleanup_then_reuse_does_not_bind_retired_window_textures(self):
        from meltygui.core.graphics.metal_tiles import MetalTileBackend
        from meltygui.core.cache.tile_cache import TileCacheMasked
        r=self.renderer
        r.begin_scene(8,8,(0,0,0,1))
        cache=TileCacheMasked(gpu=MetalTileBackend(r))
        cache.gpu.begin_masks(cache,8,8)
        cache.gpu.build_windows(cache,(0,0,1,1,8,8))
        self.native._test_flush()
        cache.cleanup()
        cache.gpu.begin_masks(cache,8,8)
        r.compose_scene(cache)
        self.pixels(r.scene_framebuffer)
        self.assertIsNone(cache._win_rects_tex)
        cache.cleanup()

    def test_real_imgui_buffers_text_and_scene_filters(self):
        from meltygui.hdr_color import pack_color
        from meltygui.core.runtime.toggles import Toggles
        from meltygui.core.graphics.metal_tiles import MetalTileBackend
        from meltygui.core.cache.tile_cache import TileCacheMasked
        r=self.renderer
        r.begin_scene(64,48,(.05,.05,.05,1))
        r.draw_backgrounds([], (.05,.05,.05,1))
        self.imgui.new_frame()
        self.imgui.get_background_draw_list().add_text(2,2,pack_color(1,1,1,1),"Metal")
        r.begin_frame_split()
        self.imgui.render()
        r.render_except_overlay(self.imgui.get_draw_data())
        cache=TileCacheMasked(gpu=MetalTileBackend(r))
        cache.gpu.begin_masks(cache,64,48)
        r.compose_scene(cache)
        r.end_scene()
        image=self.pixels(r.scene_framebuffer)
        self.assertGreater(max(p[0] for row in image for p in row),.5)
        cache.cleanup()

    def test_hdr_png_icon_retains_radiance_gamut_and_alpha_through_metal(self):
        import tempfile
        import numpy as np
        from types import SimpleNamespace
        from meltygui.image_load import PRIMARIES_TO_SRGB
        from meltygui.model.folder_icon_model import load_folder_icon, FolderIconTexture
        from meltygui.platforms.ios.app_icon import write_hdr_icon

        nits = np.array([[[1000., 0, 0], [0, 1000, 0]],
                         [[0, 0, 1000], [1000, 1000, 1000]]])
        alpha = np.array([[65535, 32768], [65535, 65535]], dtype=np.uint16)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_hdr_icon(root/'hdr.png', nits, 2, bytes([9, 16, 0, 1]), alpha)
            (root/'app.desktop').write_text('[Desktop Entry]\nType=Application\nIcon=./hdr.png\n')
            pixels, _ = load_folder_icon(root)
        self.assertEqual(pixels.format, 'rgba16f')
        r = self.renderer
        state = SimpleNamespace(get=lambda key, create, delete: create())
        handle = FolderIconTexture(pixels)._upload_native(state, r).texture_id
        r.set_texture_sampling(handle, True, True)
        r.begin_scene(64, 48, (0, 0, 0, 0))
        r.draw_backgrounds([], (0, 0, 0, 0))
        self.imgui.new_frame()
        self.imgui.get_background_draw_list().add_image(handle, (8, 8), (24, 24))
        r.begin_frame_split()
        self.imgui.render()
        r.render_except_overlay(self.imgui.get_draw_data())
        image = self.pixels(r.scene_framebuffer)
        expected = (nits @ PRIMARIES_TO_SRGB[9].T / 203) * (alpha[..., None] / 65535)
        for y in range(2):
            for x in range(2):
                actual = image[12 + 8*y][12 + 8*x]
                np.testing.assert_allclose(actual[:3], expected[y, x], rtol=.003, atol=.003)
                self.assertAlmostEqual(actual[3], alpha[y, x]/65535, delta=.001)
        self.assertGreater(image[12][12][0], 5)
        self.assertLess(image[12][20][0], 0)
        drawable = r.create_texture(64, 48, 'rgba16f')
        self.native.set_scene(r.scene_framebuffer)
        self.native._test_present(drawable)
        presented = self.pixels(drawable)
        for y, x in ((12, 12), (12, 20), (20, 12), (20, 20)):
            self.assertEqual(presented[y][x], image[y][x])
        r.delete_texture(drawable)
        r.delete_texture(handle)

    def test_cpu_icon_rows_and_srgb_colors_survive_native_upload(self):
        from types import SimpleNamespace
        from meltygui.model.folder_icon_model import FolderIconTexture, IconPixels
        from meltygui.model.texture_model import ImageTexture
        # Asymmetric rows expose a Y flip; midtones expose missing/double sRGB
        # decoding. Use the two real CPU-image upload paths, not cached tiles.
        pixels = bytes((255, 0, 0, 255, 128, 128, 128, 255,
                        0, 0, 255, 255, 0, 255, 0, 128))
        state = SimpleNamespace(get=lambda key, create, delete: create())
        icons = (FolderIconTexture(IconPixels(Path('icon.png'), 2, 2, pixels)),
                 ImageTexture('image', 2, 2, 0x1908, pixels))
        r = self.renderer
        for icon in icons:
            with self.subTest(kind=type(icon).__name__):
                handle = icon._upload_native(state, r).texture_id
                r.set_texture_sampling(handle, True, True)
                r.begin_scene(64, 48, (0, 0, 0, 0))
                r.draw_backgrounds([], (0, 0, 0, 0))
                self.imgui.new_frame()
                self.imgui.get_background_draw_list().add_image(handle, (8, 8), (24, 24))
                r.begin_frame_split()
                self.imgui.render()
                r.render_except_overlay(self.imgui.get_draw_data())
                image = self.pixels(r.scene_framebuffer)
                for actual, expected in ((image[12][12], (1, 0, 0, 1)),
                                         (image[12][20], (.21586, .21586, .21586, 1)),
                                         (image[20][12], (0, 0, 1, 1)),
                                         (image[20][20], (0, 128/255, 0, 128/255))):
                    for got, want in zip(actual, expected):
                        self.assertAlmostEqual(got, want, delta=.002)
                r.delete_texture(handle)


if __name__=="__main__":
    unittest.main()
