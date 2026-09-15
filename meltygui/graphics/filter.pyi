"""
Type stub file for Filter class.

This file is auto-generated to provide IDE autocomplete for
dynamically created shader methods.

To regenerate: python -m shader_manager.stub_generator
"""

from typing import Optional, Dict, Any, Tuple, List
from contextlib import AbstractContextManager
from .compiler import CompiledProgram
from .registry import ShaderRegistry


class Filter(AbstractContextManager):
    """
    Main interface for shader-based texture filtering.
    
    All registered shaders are available as methods for IDE autocomplete.
    """
    
    def __init__(self, auto_compile: bool = True) -> None: ...
    
    def box_blur(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        radius: float = 1.0,
        texture_size: Tuple[float, float] = (512.0, 512.0),
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply box_blur shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            radius: Shader uniform (float)
            texture_size: Shader uniform (Tuple[float, float])
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def brightness_contrast(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        brightness: float = 0.0,
        contrast: float = 1.0,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply brightness_contrast shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            brightness: Shader uniform (float)
            contrast: Shader uniform (float)
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def bulge(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        center: Tuple[float, float] = (0.5, 0.5),
        radius: float = 0.5,
        strength: float = 0.5,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply bulge shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            center: Shader uniform (Tuple[float, float])
            radius: Shader uniform (float)
            strength: Shader uniform (float)
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def chromatic_aberration(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        amount: float = 0.005,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply chromatic_aberration shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            amount: Shader uniform (float)
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def gamma(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        gamma: float = 1.0,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply gamma shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            gamma: Shader uniform (float)
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def gaussian_blur(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        radius: float = 4.0,
        texture_size: Tuple[float, float] = None,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply gaussian_blur shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            radius: Shader uniform (float)
            texture_size: Shader uniform (Tuple[float, float])
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def gaussian_blur_h(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        radius: float = 4.0,
        texture_size: Tuple[float, float] = (512.0, 512.0),
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply gaussian_blur_h shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            radius: Shader uniform (float)
            texture_size: Shader uniform (Tuple[float, float])
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def gaussian_blur_v(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        radius: float = 4.0,
        texture_size: Tuple[float, float] = (512.0, 512.0),
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply gaussian_blur_v shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            radius: Shader uniform (float)
            texture_size: Shader uniform (Tuple[float, float])
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def grayscale(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        method: int = 0,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply grayscale shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            method: Shader uniform (int)
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def hue_saturation(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        hue_shift: float = 0.0,
        lightness: float = 0.0,
        saturation: float = 1.0,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply hue_saturation shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            hue_shift: Shader uniform (float)
            lightness: Shader uniform (float)
            saturation: Shader uniform (float)
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def invert(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        amount: float = 1.0,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply invert shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            amount: Shader uniform (float)
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def jet(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        offset: float = 0.0,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply jet shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            offset: Shader uniform (float)
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def laplacian(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        strength: float = 1.0,
        texture_size: Tuple[float, float] = (512.0, 512.0),
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply laplacian shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            strength: Shader uniform (float)
            texture_size: Shader uniform (Tuple[float, float])
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def min_max_reduction(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        texture_size: Tuple[float, float] = None,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply min_max_reduction shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            texture_size: Shader uniform (Tuple[float, float])
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def normalize_remap(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        min_max_tex: Any = None,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply normalize_remap shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            min_max_tex: Shader uniform (Any)
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def passthrough(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply passthrough shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def pixelate(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        pixel_size: float = 8.0,
        texture_size: Tuple[float, float] = (512.0, 512.0),
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply pixelate shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            pixel_size: Shader uniform (float)
            texture_size: Shader uniform (Tuple[float, float])
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def posterize(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        levels: float = 8.0,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply posterize shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            levels: Shader uniform (float)
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def sepia(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        amount: float = 1.0,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply sepia shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            amount: Shader uniform (float)
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def shadow_blur(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        blur_scale: float = 20.0,
        min_blur: float = 1.0,
        texture_size: Tuple[float, float] = None,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply shadow_blur shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            blur_scale: Shader uniform (float)
            min_blur: Shader uniform (float)
            texture_size: Shader uniform (Tuple[float, float])
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def shadow_cast(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        blur_exponent: float = 1.0,
        blur_samples: int = 8,
        blur_scale: float = 0.3,
        depth_bias: float = 0.0,
        depth_scale: float = 1.0,
        height_scale: float = 0.5,
        hit_falloff: float = 14.0,
        hit_strength: float = 0.7,
        light_dir: Tuple[float, float] = (-0.02, 0.064),
        max_steps: int = 64,
        min_height_diff: float = 0.0,
        shadow_strength: float = 0.4,
        surface_threshold: float = -10.0,
        texture_size: Tuple[float, float] = None,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply shadow_cast shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            blur_exponent: Shader uniform (float)
            blur_samples: Shader uniform (int)
            blur_scale: Shader uniform (float)
            depth_bias: Shader uniform (float)
            depth_scale: Shader uniform (float)
            height_scale: Shader uniform (float)
            hit_falloff: Shader uniform (float)
            hit_strength: Shader uniform (float)
            light_dir: Shader uniform (Tuple[float, float])
            max_steps: Shader uniform (int)
            min_height_diff: Shader uniform (float)
            shadow_strength: Shader uniform (float)
            surface_threshold: Shader uniform (float)
            texture_size: Shader uniform (Tuple[float, float])
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def shadow_composite(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        depth_map: Any = None,
        depth_scale: float = 1.0,
        depth_sharpness: float = 50.0,
        glow_map: Any = None,
        glow_shadow_cut: float = 1.0,
        glow_strength: float = 0.0,
        light_dir: Tuple[float, float] = (-0.5, 1.6),
        shadow_color: Tuple[float, float, float] = (0.0, 0.0, 0.0),
        shadow_map: Any = None,
        shadow_opacity: float = 1.0,
        shadow_size: Tuple[float, float] = None,
        specular_bevel: float = 0.0,
        specular_depth_eps: float = 0.001,
        specular_depth_falloff: float = 0.0,
        specular_fade: float = 300.0,
        specular_fade_rel: float = 0.6,
        specular_roughness: float = 0.4,
        specular_slope_tol: float = 0.0002,
        specular_strength: float = 1.0,
        texture_size: Tuple[float, float] = None,
        win_mask: Any = None,
        win_rects: Any = None,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply shadow_composite shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            depth_map: Shader uniform (Any)
            depth_scale: Shader uniform (float)
            depth_sharpness: Shader uniform (float)
            glow_map: Shader uniform (Any)
            glow_shadow_cut: Shader uniform (float)
            glow_strength: Shader uniform (float)
            light_dir: Shader uniform (Tuple[float, float])
            shadow_color: Shader uniform (Tuple[float, float, float])
            shadow_map: Shader uniform (Any)
            shadow_opacity: Shader uniform (float)
            shadow_size: Shader uniform (Tuple[float, float])
            specular_bevel: Shader uniform (float)
            specular_depth_eps: Shader uniform (float)
            specular_depth_falloff: Shader uniform (float)
            specular_fade: Shader uniform (float)
            specular_fade_rel: Shader uniform (float)
            specular_roughness: Shader uniform (float)
            specular_slope_tol: Shader uniform (float)
            specular_strength: Shader uniform (float)
            texture_size: Shader uniform (Tuple[float, float])
            win_mask: Shader uniform (Any)
            win_rects: Shader uniform (Any)
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def sobel(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        strength: float = 1.0,
        texture_size: Tuple[float, float] = (512.0, 512.0),
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply sobel shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            strength: Shader uniform (float)
            texture_size: Shader uniform (Tuple[float, float])
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def swirl(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        angle: float = 3.0,
        center: Tuple[float, float] = (0.5, 0.5),
        radius: float = 0.5,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply swirl shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            angle: Shader uniform (float)
            center: Shader uniform (Tuple[float, float])
            radius: Shader uniform (float)
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def threshold(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        smoothness: float = 0.0,
        threshold: float = 0.5,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply threshold shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            smoothness: Shader uniform (float)
            threshold: Shader uniform (float)
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    
    def vignette(
        self,
        texture_id: int = 0,
        in_place: bool = False,
        amount: float = 0.5,
        radius: float = 0.75,
        softness: float = 0.45,
        output_texture: Optional[int] = None,
        output_framebuffer: Optional[int] = None,
        input_framebuffer: Optional[int] = None,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> int:
        """
        Apply vignette shader to a texture or framebuffer.
        
        Args:
            texture_id: Input texture ID (ignored if input_framebuffer set)
            in_place: If True, modify the input texture directly
            amount: Shader uniform (float)
            radius: Shader uniform (float)
            softness: Shader uniform (float)
            output_texture: Optional specific output texture to render to
            output_framebuffer: Optional framebuffer to render to (e.g., 0 for main screen)
            input_framebuffer: Optional framebuffer to read from (e.g., 0 for main screen)
            output_size: Optional (width, height) render resolution; runs the shader
                at a lower resolution while sampling the full-res input
        
        Returns:
            Output texture ID (0 if rendering to framebuffer)
        """
        ...
    