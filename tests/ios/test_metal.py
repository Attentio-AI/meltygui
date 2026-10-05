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
        self.renderer.clear_rect(tile.tex,(0,22,10,10),(1,0,0,1))
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


if __name__=="__main__":
    unittest.main()
