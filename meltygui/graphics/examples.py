"""
Example usage of the Melty shader library.

This demonstrates:
1. Basic filter application
2. Creating custom shaders
3. Filter chaining
4. Custom shader types
"""
from lsd.gl_gui.melty import Melty
from lsd.gl_gui.shader_library import register_shader, GLType, register_shader_type, Filter
from lsd.gl_gui.shader_library.shader_manager.registry import get_registry


# =============================================================================
# CUSTOM SHADER EXAMPLE
# =============================================================================

@register_shader
class ColorTint:
    """Apply a color tint to the image."""
    shader_type = 'standard'
    uniforms = {
        'tint_color': (GLType.VEC3, (1.0, 0.8, 0.6)),
        'amount': (GLType.FLOAT, 0.3),
    }
    fragment_code = """
void main() {
    vec4 color = texture(u_texture, v_texcoord);
    color.rgb = mix(color.rgb, color.rgb * tint_color, amount);
    fragColor = color;
}
"""


@register_shader
class FilmGrain:
    """Add film grain noise effect."""
    shader_type = 'standard'
    uniforms = {
        'amount': (GLType.FLOAT, 0.1),
        'time': (GLType.FLOAT, 0.0),  # Animate with time for varying grain
    }
    fragment_code = """
// Simple hash function for noise
float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
}

void main() {
    vec4 color = texture(u_texture, v_texcoord);
    
    // Generate noise based on position and time
    float noise = hash(v_texcoord * 1000.0 + time) * 2.0 - 1.0;
    
    // Apply grain
    color.rgb += noise * amount;
    color.rgb = clamp(color.rgb, 0.0, 1.0);
    
    fragColor = color;
}
"""


@register_shader
class Sharpen:
    """Sharpening filter using unsharp mask."""
    shader_type = 'standard'
    uniforms = {
        'amount': (GLType.FLOAT, 1.0),
        'texture_size': (GLType.VEC2, (512.0, 512.0)),
    }
    fragment_code = """
void main() {
    vec2 texel = 1.0 / texture_size;
    
    // Sample center and neighbors
    vec4 center = texture(u_texture, v_texcoord);
    vec4 top = texture(u_texture, v_texcoord + vec2(0, -1) * texel);
    vec4 bottom = texture(u_texture, v_texcoord + vec2(0, 1) * texel);
    vec4 left = texture(u_texture, v_texcoord + vec2(-1, 0) * texel);
    vec4 right = texture(u_texture, v_texcoord + vec2(1, 0) * texel);
    
    // Sharpen kernel: -1, -1, -1 / -1, 9, -1 / -1, -1, -1 (simplified)
    vec4 sharpened = center * (1.0 + 4.0 * amount) - (top + bottom + left + right) * amount;
    
    fragColor = clamp(sharpened, 0.0, 1.0);
}
"""


# =============================================================================
# CUSTOM SHADER TYPE EXAMPLE
# =============================================================================

@register_shader_type
class MultiTexture:
    """
    Shader type that supports multiple texture inputs.
    Useful for blend modes, masks, etc.
    """
    vertex_in = {
        'position': GLType.VEC3,
        'texcoord': GLType.VEC2,
    }
    vertex_out = {
        'v_texcoord': GLType.VEC2,
    }
    vertex_code = """
void main() {
    v_texcoord = texcoord;
    gl_Position = vec4(position, 1.0);
}
"""


@register_shader(shader_type='multi_texture')
class BlendMultiply:
    """Multiply blend two textures."""
    uniforms = {
        'blend_texture': (GLType.SAMPLER2D, 0),
        'opacity': (GLType.FLOAT, 1.0),
    }
    fragment_code = """
uniform sampler2D blend_texture;

void main() {
    vec4 base = texture(u_texture, v_texcoord);
    vec4 blend = texture(blend_texture, v_texcoord);
    
    vec3 result = base.rgb * blend.rgb;
    fragColor = vec4(mix(base.rgb, result, opacity), base.a);
}
"""


# =============================================================================
# INTEGRATION EXAMPLE - How your Melty class would use Filter
# =============================================================================

# class Melty:
#     """Example of how your existing Melty class would integrate with Filter."""
#
#     def __init__(self):
#         self.filter = Filter()
#
#     def cleanup(self):
#         self.filter.cleanup()


# =============================================================================
# USAGE EXAMPLES
# =============================================================================

def basic_usage_example():
    """Demonstrate basic filter usage."""
    
    # Your Melty class holds the Filter instance

    # Assuming you have a texture_id defined somewhere
    texture_id = 1  # Placeholder
    
    # Apply a single filter
    result = Melty.filter.brightness_contrast(
        texture_id,
        brightness=0.2,
        contrast=1.3
    )
    
    # Apply in-place (modifies original texture)
    Melty.filter.grayscale(texture_id, in_place=True)
    
    # Apply multiple filters
    result = Melty.filter.hue_saturation(result, saturation=1.5)
    result = Melty.filter.vignette(result, radius=0.8, amount=0.3)
    
    # Clean up
    Melty.cleanup()


def chaining_example():
    """Demonstrate filter chaining."""

    texture_id = 1  # Placeholder

    # Create a filter chain
    # Note: texture_size is automatically detected - no need to pass it!
    result = (Melty.filter.chain()
        .brightness_contrast(brightness=0.1, contrast=1.2)
        .hue_saturation(saturation=1.2)
        .vignette(radius=0.7, softness=0.5, amount=0.4)
        .sharpen(amount=0.5)
        .apply(texture_id))
    
    # Apply chain in-place
    (Melty.filter.chain()
        .sepia(amount=0.8)
        .film_grain(amount=0.05)
        .apply(texture_id, in_place=True))

    Melty.cleanup()


def context_manager_example():
    """Using Filter as a context manager."""
    
    texture_id = 1  # Placeholder
    
    with Filter() as f:
        result = f.posterize(texture_id, levels=4)
        
    # Resources are automatically cleaned up


def listing_shaders_example():
    """List available shaders and their info."""

    f = Filter()

    # List all available shaders
    print("Available shaders:")
    for name in f.list_shaders():
        info = f.get_shader_info(name)
        print(f"  {name}:")
        print(f"    type: {info['shader_type']}")
        print(f"    uniforms: {list(info['uniforms'].keys())}")

    # List shader types
    print("\nAvailable shader types:")
    for name in f.list_shader_types():
        print(f"  {name}")

    f.cleanup()


def texture_caching_example():
    """Demonstrate automatic texture caching for efficient multi-frame processing."""

    texture_id = 1  # Placeholder for input texture

    f = Filter()

    # Process multiple frames efficiently
    # Output textures are automatically cached per input texture ID
    for frame in range(10):
        # Apply filters - same output texture is cached each frame!
        result1 = f.brightness_contrast(texture_id, brightness=0.1)
        result2 = f.vignette(result1, amount=0.3)

        # Use the final result...
        # (In a real app, you'd render it or save it)

        # No need to manually release - caching is automatic!

    # Check cache statistics
    stats = f.get_cache_stats()
    print("Texture cache stats:", stats['textures'])
    # Output: {1: (10, 512, 512), 10: (11, 512, 512)}
    # Maps input_texture_id -> (output_texture_id, width, height)

    # Clear cache when done to free GPU memory
    f.clear_texture_cache()

    f.cleanup()


def cache_benefits_example():
    """
    Demonstrate the benefits of automatic texture caching.

    With automatic caching:
        - Each input texture ID gets its own persistent output texture
        - Textures are automatically reused across frames
        - Zero manual management required
        - Optimal performance for multi-frame processing
    """

    texture_id = 1  # Placeholder
    width, height = 1920, 1080

    f = Filter()

    # Process 100 frames with filter chain
    for frame in range(100):
        # Apply multiple filters
        # Each intermediate result automatically gets its own cached texture
        temp1 = f.brightness_contrast(texture_id, brightness=0.1)
        # Note: texture_size is automatically detected from the texture!
        temp2 = f.gaussian_blur_h(temp1, radius=5.0)
        final = f.gaussian_blur_v(temp2, radius=5.0)

        # Use final result...
        # No manual release needed!

    # After 100 iterations, 3 textures exist (one per unique input texture ID)
    stats = f.get_cache_stats()
    print(f"Cached textures: {len(stats['textures'])}")
    # Output: 3 (one for texture_id=1, one for temp1, one for temp2)

    f.cleanup()


def multi_size_caching_example():
    """
    Demonstrate automatic FBO and texture caching for multiple texture sizes.

    This is the key use case: when processing textures of different sizes
    in the same session, FBOs and output textures are cached automatically.
    """

    f = Filter()

    # Simulate rendering two different sized textures each frame
    texture_a = 1  # 512x512 texture
    texture_b = 2  # 1920x1080 texture

    for frame in range(100):
        # Process texture A (512x512)
        # Frame 1: Creates FBO for 512x512 and output texture for texture_a
        # Frame 2+: Reuses both!
        result_a = f.brightness_contrast(texture_a, brightness=0.1)
        # Use result_a...

        # Process texture B (1920x1080)
        # Frame 1: Creates FBO for 1920x1080 and output texture for texture_b
        # Frame 2+: Reuses both!
        result_b = f.vignette(texture_b, amount=0.3)
        # Use result_b...

        # No manual management needed!

    # Check cache stats
    stats = f.get_cache_stats()
    print("Cache statistics:")
    print(f"  Output textures: {stats['textures']}")
    # e.g., {1: (10, 512, 512), 2: (11, 1920, 1080)}
    # Maps input_texture_id -> (output_texture_id, width, height)
    print(f"  FBOs cached: {list(stats['fbos'].keys())}")
    # e.g., [(512, 512), (1920, 1080)]
    print(f"  Temp textures: {list(stats['temp_textures'].keys())}")
    # e.g., [(512, 512), (1920, 1080)]

    # Each size has its own FBO and temp texture - no extra overhead!
    # Each input texture has its own output texture - automatic reuse!

    f.cleanup()


# =============================================================================
# PYGAME INTEGRATION EXAMPLE
# =============================================================================

# def pygame_integration_example():
#     """Example integration with Pygame."""
#     import pygame
#     from pygame.locals import *
#     from OpenGL.GL import *
#
#     # Initialize Pygame and OpenGL
#     pygame.init()
#     pygame.display.set_mode((800, 600), DOUBLEBUF | OPENGL)
#
#     # Initialize Melty class with Filter
#     melty = Melty()
#
#     # Load an image as texture (simplified)
#     def load_texture(path):
#         surface = pygame.image.load(path)
#         data = pygame.image.tostring(surface, "RGBA", True)
#         width, height = surface.get_size()
#
#         texture = glGenTextures(1)
#         glBindTexture(GL_TEXTURE_2D, texture)
#         glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, width, height, 0,
#                      GL_RGBA, GL_UNSIGNED_BYTE, data)
#         glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
#         glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
#
#         return texture, (width, height)
#
#     # Example usage:
#     # texture, size = load_texture("image.png")
#     # texture = melty.filter.brightness_contrast(texture, brightness=0.2)
#
#     melty.cleanup()
#     pygame.quit()


if __name__ == '__main__':
    # Just list available shaders (doesn't need OpenGL context)

    registry = get_registry()
    
    print("Melty Shader Library")
    print("=" * 40)
    
    print("\nRegistered Shader Types:")
    for name, shader_type in registry.shader_types.items():
        print(f"  @{name}")
        print(f"    inputs:  {list(shader_type.vertex_in.keys())}")
        print(f"    outputs: {list(shader_type.vertex_out.keys())}")
    
    print("\nRegistered Shaders:")
    for name, shader in registry.shaders.items():
        uniforms = ', '.join(f"{k}={v[1]}" for k, v in shader.uniforms.items())
        print(f"  {name}({uniforms})")
