"""
Built-in shader types and common shaders.

These are automatically registered when importing melty.
"""

from .shader_manager.registry import register_shader_type, register_shader
from .shader_manager.base import GLType


# =============================================================================
# SHADER TYPES
# =============================================================================

@register_shader_type
class Standard:
    """
    Standard shader type for fullscreen texture processing.
    
    Uses a simple quad with position and texture coordinates.
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


@register_shader_type
class Fullscreen:
    """
    Fullscreen shader type - generates texture coordinates from position.
    
    Useful when you don't have explicit texture coordinates.
    """
    vertex_in = {
        'position': GLType.VEC3,
    }
    vertex_out = {
        'v_texcoord': GLType.VEC2,
    }
    vertex_code = """
void main() {
    v_texcoord = position.xy * 0.5 + 0.5;
    gl_Position = vec4(position, 1.0);
}
"""



@register_shader_type
class Transformed:
    """
    Shader type with model-view-projection transformation.
    
    Useful for 3D scenes or when you need transforms.
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
    gl_Position = u_projection * u_modelview * vec4(position, 1.0);
}
"""


# =============================================================================
# BASIC FILTERS
# =============================================================================

@register_shader
class Passthrough:
    """Simple passthrough - copies input to output unchanged."""
    shader_type = 'standard'
    uniforms = {}
    fragment_code = """
void main() {
    fragColor = texture(u_texture, v_texcoord);
}
"""


@register_shader
class BrightnessContrast:
    """Adjust brightness and contrast of an image."""
    shader_type = 'standard'
    uniforms = {
        'brightness': (GLType.FLOAT, 0.0),  # -1.0 to 1.0
        'contrast': (GLType.FLOAT, 1.0),    # 0.0 to 2.0+
    }
    fragment_code = """
void main() {
    vec4 color = texture(u_texture, v_texcoord);
    // Apply contrast around midpoint, then add brightness
    color.rgb = (color.rgb - 0.5) * contrast + 0.5 + brightness;
    color.rgb = clamp(color.rgb, 0.0, 1.0);
    fragColor = color;
}
"""


@register_shader
class HueSaturation:
    """Adjust hue, saturation, and lightness."""
    shader_type = 'standard'
    uniforms = {
        'hue_shift': (GLType.FLOAT, 0.0),      # -1.0 to 1.0 (maps to -180 to 180 degrees)
        'saturation': (GLType.FLOAT, 1.0),     # 0.0 to 2.0+
        'lightness': (GLType.FLOAT, 0.0),      # -1.0 to 1.0
    }
    fragment_code = """
vec3 rgb2hsl(vec3 c) {
    float maxc = max(max(c.r, c.g), c.b);
    float minc = min(min(c.r, c.g), c.b);
    float l = (maxc + minc) / 2.0;
    
    if (maxc == minc) {
        return vec3(0.0, 0.0, l);
    }
    
    float d = maxc - minc;
    float s = l > 0.5 ? d / (2.0 - maxc - minc) : d / (maxc + minc);
    
    float h;
    if (maxc == c.r) {
        h = (c.g - c.b) / d + (c.g < c.b ? 6.0 : 0.0);
    } else if (maxc == c.g) {
        h = (c.b - c.r) / d + 2.0;
    } else {
        h = (c.r - c.g) / d + 4.0;
    }
    h /= 6.0;
    
    return vec3(h, s, l);
}

float hue2rgb(float p, float q, float t) {
    if (t < 0.0) t += 1.0;
    if (t > 1.0) t -= 1.0;
    if (t < 1.0/6.0) return p + (q - p) * 6.0 * t;
    if (t < 1.0/2.0) return q;
    if (t < 2.0/3.0) return p + (q - p) * (2.0/3.0 - t) * 6.0;
    return p;
}

vec3 hsl2rgb(vec3 c) {
    if (c.y == 0.0) {
        return vec3(c.z);
    }
    
    float q = c.z < 0.5 ? c.z * (1.0 + c.y) : c.z + c.y - c.z * c.y;
    float p = 2.0 * c.z - q;
    
    return vec3(
        hue2rgb(p, q, c.x + 1.0/3.0),
        hue2rgb(p, q, c.x),
        hue2rgb(p, q, c.x - 1.0/3.0)
    );
}

void main() {
    vec4 color = texture(u_texture, v_texcoord);
    vec3 hsl = rgb2hsl(color.rgb);
    
    // Apply adjustments
    hsl.x = fract(hsl.x + hue_shift * 0.5);  // Hue shift
    hsl.y = clamp(hsl.y * saturation, 0.0, 1.0);  // Saturation
    hsl.z = clamp(hsl.z + lightness, 0.0, 1.0);   // Lightness
    
    fragColor = vec4(hsl2rgb(hsl), color.a);
}
"""


@register_shader
class Grayscale:
    """Convert to grayscale with adjustable method."""
    shader_type = 'standard'
    uniforms = {
        'method': (GLType.INT, 0),  # 0=luminance, 1=average, 2=lightness
    }
    fragment_code = """
void main() {
    vec4 color = texture(u_texture, v_texcoord);
    float gray;
    
    if (method == 0) {
        // Luminance (perceptual)
        gray = dot(color.rgb, vec3(0.2126, 0.7152, 0.0722));
    } else if (method == 1) {
        // Average
        gray = (color.r + color.g + color.b) / 3.0;
    } else {
        // Lightness
        gray = (max(max(color.r, color.g), color.b) + min(min(color.r, color.g), color.b)) / 2.0;
    }
    
    fragColor = vec4(vec3(gray), color.a);
}
"""


@register_shader
class Invert:
    """Invert colors."""
    shader_type = 'standard'
    uniforms = {
        'amount': (GLType.FLOAT, 1.0),  # 0.0 to 1.0
    }
    fragment_code = """
void main() {
    vec4 color = texture(u_texture, v_texcoord);
    vec3 inverted = 1.0 - color.rgb;
    fragColor = vec4(mix(color.rgb, inverted, amount), color.a);
}
"""


@register_shader  
class Gamma:
    """Apply gamma correction."""
    shader_type = 'standard'
    uniforms = {
        'gamma': (GLType.FLOAT, 1.0),  # 0.1 to 3.0+
    }
    fragment_code = """
void main() {
    vec4 color = texture(u_texture, v_texcoord);
    color.rgb = pow(color.rgb, vec3(1.0 / gamma));
    fragColor = color;
}
"""


@register_shader
class Threshold:
    """Apply threshold to create binary image."""
    shader_type = 'standard'
    uniforms = {
        'threshold': (GLType.FLOAT, 0.5),  # 0.0 to 1.0
        'smoothness': (GLType.FLOAT, 0.0),  # 0.0 = hard, higher = softer
    }
    fragment_code = """
void main() {
    vec4 color = texture(u_texture, v_texcoord);
    float lum = dot(color.rgb, vec3(0.2126, 0.7152, 0.0722));
    float value = smoothstep(threshold - smoothness, threshold + smoothness, lum);
    fragColor = vec4(vec3(value), color.a);
}
"""


# =============================================================================
# BLUR FILTERS
# =============================================================================

@register_shader
class BoxBlur:
    """Simple box blur (uniform weighted average)."""
    shader_type = 'standard'
    uniforms = {
        'radius': (GLType.FLOAT, 1.0),
        'texture_size': (GLType.VEC2, (512.0, 512.0)),
    }
    fragment_code = """
void main() {
    vec2 texel = 1.0 / texture_size;
    vec4 sum = vec4(0.0);
    int r = int(radius);
    float count = 0.0;
    
    for (int x = -r; x <= r; x++) {
        for (int y = -r; y <= r; y++) {
            sum += texture(u_texture, v_texcoord + vec2(float(x), float(y)) * texel);
            count += 1.0;
        }
    }
    
    fragColor = sum / count;
}
"""


@register_shader
class GaussianBlurH:
    """Horizontal pass of separable Gaussian blur."""
    shader_type = 'standard'
    uniforms = {
        'radius': (GLType.FLOAT, 4.0),
        'texture_size': (GLType.VEC2, (512.0, 512.0)),
    }
    fragment_code = """
void main() {
    vec2 texel = vec2(1.0 / texture_size.x, 0.0);
    
    // Pre-computed Gaussian weights for sigma = radius/3
    float weights[9];
    weights[0] = 0.0625; weights[1] = 0.125; weights[2] = 0.1875;
    weights[3] = 0.25; weights[4] = 0.1875; weights[5] = 0.125;
    weights[6] = 0.0625; weights[7] = 0.0; weights[8] = 0.0;
    
    vec4 sum = vec4(0.0);
    float total = 0.0;
    int r = int(radius);
    
    for (int i = -r; i <= r; i++) {
        float w = exp(-float(i*i) / (2.0 * radius * radius / 9.0));
        sum += texture(u_texture, v_texcoord + float(i) * texel) * w;
        total += w;
    }
    
    fragColor = sum / total;
}
"""


@register_shader
class GaussianBlurV:
    """Vertical pass of separable Gaussian blur."""
    shader_type = 'standard'
    uniforms = {
        'radius': (GLType.FLOAT, 4.0),
        'texture_size': (GLType.VEC2, (512.0, 512.0)),
    }
    fragment_code = """
void main() {
    vec2 texel = vec2(0.0, 1.0 / texture_size.y);
    
    vec4 sum = vec4(0.0);
    float total = 0.0;
    int r = int(radius);
    
    for (int i = -r; i <= r; i++) {
        float w = exp(-float(i*i) / (2.0 * radius * radius / 9.0));
        sum += texture(u_texture, v_texcoord + float(i) * texel) * w;
        total += w;
    }
    
    fragColor = sum / total;
}
"""


@register_shader
class GaussianBlur:
    """Combined Gaussian blur (both horizontal and vertical in one pass)."""
    shader_type = 'standard'
    uniforms = {
        'radius': (GLType.FLOAT, 4.0),
        'texture_size': (GLType.VEC2, None),
    }
    fragment_code = """
void main() {
    vec2 texel = 1.0 / texture_size;

    vec4 sum = vec4(0.0);
    float total = 0.0;
    int r = int(radius);

    // 2D Gaussian blur - sample in both directions
    for (int x = -r; x <= r; x++) {
        for (int y = -r; y <= r; y++) {
            float dist = float(x*x + y*y);
            float w = exp(-dist / (2.0 * radius * radius / 9.0));
            sum += texture(u_texture, v_texcoord + vec2(float(x), float(y)) * texel) * w;
            total += w;
        }
    }

    fragColor = sum / total;
}
"""


# =============================================================================
# EDGE DETECTION
# =============================================================================

@register_shader
class Sobel:
    """Sobel edge detection."""
    shader_type = 'standard'
    uniforms = {
        'texture_size': (GLType.VEC2, (512.0, 512.0)),
        'strength': (GLType.FLOAT, 1.0),
    }
    fragment_code = """
void main() {
    vec2 texel = 1.0 / texture_size;
    
    // Sample 3x3 neighborhood
    float tl = dot(texture(u_texture, v_texcoord + vec2(-1, -1) * texel).rgb, vec3(0.299, 0.587, 0.114));
    float t  = dot(texture(u_texture, v_texcoord + vec2( 0, -1) * texel).rgb, vec3(0.299, 0.587, 0.114));
    float tr = dot(texture(u_texture, v_texcoord + vec2( 1, -1) * texel).rgb, vec3(0.299, 0.587, 0.114));
    float l  = dot(texture(u_texture, v_texcoord + vec2(-1,  0) * texel).rgb, vec3(0.299, 0.587, 0.114));
    float r  = dot(texture(u_texture, v_texcoord + vec2( 1,  0) * texel).rgb, vec3(0.299, 0.587, 0.114));
    float bl = dot(texture(u_texture, v_texcoord + vec2(-1,  1) * texel).rgb, vec3(0.299, 0.587, 0.114));
    float b  = dot(texture(u_texture, v_texcoord + vec2( 0,  1) * texel).rgb, vec3(0.299, 0.587, 0.114));
    float br = dot(texture(u_texture, v_texcoord + vec2( 1,  1) * texel).rgb, vec3(0.299, 0.587, 0.114));
    
    // Sobel kernels
    float gx = -tl - 2.0*l - bl + tr + 2.0*r + br;
    float gy = -tl - 2.0*t - tr + bl + 2.0*b + br;
    
    float edge = sqrt(gx*gx + gy*gy) * strength;
    fragColor = vec4(vec3(edge), 1.0);
}
"""


@register_shader
class Laplacian:
    """Laplacian edge detection."""
    shader_type = 'standard'
    uniforms = {
        'texture_size': (GLType.VEC2, (512.0, 512.0)),
        'strength': (GLType.FLOAT, 1.0),
    }
    fragment_code = """
void main() {
    vec2 texel = 1.0 / texture_size;
    
    vec4 center = texture(u_texture, v_texcoord);
    vec4 t = texture(u_texture, v_texcoord + vec2(0, -1) * texel);
    vec4 b = texture(u_texture, v_texcoord + vec2(0,  1) * texel);
    vec4 l = texture(u_texture, v_texcoord + vec2(-1, 0) * texel);
    vec4 r = texture(u_texture, v_texcoord + vec2( 1, 0) * texel);
    
    vec4 edge = (t + b + l + r - 4.0 * center) * strength;
    fragColor = vec4(abs(edge.rgb), center.a);
}
"""


# =============================================================================
# COLOR EFFECTS
# =============================================================================

@register_shader
class Sepia:
    """Apply sepia tone effect."""
    shader_type = 'standard'
    uniforms = {
        'amount': (GLType.FLOAT, 1.0),
    }
    fragment_code = """
void main() {
    vec4 color = texture(u_texture, v_texcoord);
    
    vec3 sepia;
    sepia.r = dot(color.rgb, vec3(0.393, 0.769, 0.189));
    sepia.g = dot(color.rgb, vec3(0.349, 0.686, 0.168));
    sepia.b = dot(color.rgb, vec3(0.272, 0.534, 0.131));
    
    fragColor = vec4(mix(color.rgb, sepia, amount), color.a);
}
"""


@register_shader
class Vignette:
    """Apply vignette effect (darkened edges)."""
    shader_type = 'standard'
    uniforms = {
        'radius': (GLType.FLOAT, 0.75),    # Inner radius where vignette starts
        'softness': (GLType.FLOAT, 0.45),  # Softness of the vignette
        'amount': (GLType.FLOAT, 0.5),     # Darkness amount
    }
    fragment_code = """
void main() {
    vec4 color = texture(u_texture, v_texcoord);
    
    vec2 center = v_texcoord - 0.5;
    float dist = length(center) * 1.414;  // Normalize to corners = 1
    
    float vignette = smoothstep(radius, radius - softness, dist);
    color.rgb = mix(color.rgb * (1.0 - amount), color.rgb, vignette);
    
    fragColor = color;
}
"""


@register_shader
class ChromaticAberration:
    """Simulate chromatic aberration (color fringing)."""
    shader_type = 'standard'
    uniforms = {
        'amount': (GLType.FLOAT, 0.005),
    }
    fragment_code = """
void main() {
    vec2 direction = v_texcoord - 0.5;
    
    float r = texture(u_texture, v_texcoord + direction * amount).r;
    float g = texture(u_texture, v_texcoord).g;
    float b = texture(u_texture, v_texcoord - direction * amount).b;
    float a = texture(u_texture, v_texcoord).a;
    
    fragColor = vec4(r, g, b, a);
}
"""


@register_shader
class Posterize:
    """Reduce color levels for a poster effect."""
    shader_type = 'standard'
    uniforms = {
        'levels': (GLType.FLOAT, 8.0),  # Number of levels per channel
    }
    fragment_code = """
void main() {
    vec4 color = texture(u_texture, v_texcoord);
    color.rgb = floor(color.rgb * levels + 0.5) / levels;
    fragColor = color;
}
"""


# =============================================================================
# DISTORTION
# =============================================================================

@register_shader
class Pixelate:
    """Pixelate effect."""
    shader_type = 'standard'
    uniforms = {
        'pixel_size': (GLType.FLOAT, 8.0),
        'texture_size': (GLType.VEC2, (512.0, 512.0)),
    }
    fragment_code = """
void main() {
    vec2 size = pixel_size / texture_size;
    vec2 coord = floor(v_texcoord / size) * size + size * 0.5;
    fragColor = texture(u_texture, coord);
}
"""


@register_shader
class Swirl:
    """Swirl distortion effect."""
    shader_type = 'standard'
    uniforms = {
        'angle': (GLType.FLOAT, 3.0),    # Swirl angle
        'radius': (GLType.FLOAT, 0.5),   # Effect radius
        'center': (GLType.VEC2, (0.5, 0.5)),
    }
    fragment_code = """
void main() {
    vec2 coord = v_texcoord - center;
    float dist = length(coord);
    
    if (dist < radius) {
        float percent = (radius - dist) / radius;
        float theta = percent * percent * angle;
        float s = sin(theta);
        float c = cos(theta);
        coord = vec2(
            coord.x * c - coord.y * s,
            coord.x * s + coord.y * c
        );
    }
    
    fragColor = texture(u_texture, coord + center);
}
"""


@register_shader
class Bulge:
    """Bulge/pinch distortion effect."""
    shader_type = 'standard'
    uniforms = {
        'strength': (GLType.FLOAT, 0.5),  # Positive = bulge, negative = pinch
        'radius': (GLType.FLOAT, 0.5),
        'center': (GLType.VEC2, (0.5, 0.5)),
    }
    fragment_code = """
void main() {
    vec2 coord = v_texcoord - center;
    float dist = length(coord);
    
    if (dist < radius) {
        float percent = dist / radius;
        float distortion = pow(percent, 1.0 - strength);
        coord = coord * distortion;
    }
    
    fragColor = texture(u_texture, coord + center);
}
"""
