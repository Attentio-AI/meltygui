"""
Built-in shader types and common shaders.

These are automatically registered when importing melty.
"""

from src.shader_library.shader_manager.registry import register_shader_type, register_shader
from src.shader_library.shader_manager.base import GLType


@register_shader
class ShadowCast:
    """
    Shadow cast using light transmission model.
    Multiple shadows combine correctly via multiplication.
    """
    shader_type = 'standard'
    uniforms = {
        'light_dir': (GLType.VEC2, (-1.0, -1.0)),
        'height_scale': (GLType.FLOAT, 0.02),
        'blur_scale': (GLType.FLOAT, 0.05),
        'max_steps': (GLType.INT, 64),
        'blur_samples': (GLType.INT, 6),
        'depth_bias': (GLType.FLOAT, 0.005),
        'surface_threshold': (GLType.FLOAT, 0.01),
        'min_height_diff': (GLType.FLOAT, 0.0),
        'shadow_strength': (GLType.FLOAT, 0.7),
        'texture_size': (GLType.VEC2, None),
    }
    fragment_code = """
void main() {
    vec2 uv = v_texcoord;
    float receiver_depth = texture(u_texture, uv).r;

    if (receiver_depth < surface_threshold) {
        fragColor = vec4(0.0, 0.0, 0.0, 0.0);
        return;
    }

    vec2 light_normalized = normalize(light_dir);

    float min_offset = (1.0 - receiver_depth) * height_scale;
    float depth_step = 1.0 / float(max_steps);

    // Start with full light, each caster blocks some
    float light = 1.0;
    float max_height_diff = 0.0;

    for (int i = 1; i <= max_steps; i++) {
        float test_caster_depth = receiver_depth + float(i) * depth_step;

        if (test_caster_depth > 1.0) break;

        float height_diff = test_caster_depth - receiver_depth;

        if (height_diff < min_height_diff) {
            continue;
        }

        float shadow_offset = max(height_diff * height_scale, min_offset);
        vec2 base_sample_pos = uv + light_normalized * shadow_offset;

        float blur_radius = height_diff * blur_scale;

        float hits = 0.0;
        float total_samples = 0.0;

        for (int s = 0; s < blur_samples; s++) {
            float angle = float(s) * 6.28318 / float(blur_samples);

            for (int r = 0; r <= 2; r++) {
                float radius = blur_radius * float(r) / 2.0;
                vec2 offset = vec2(cos(angle), sin(angle)) * radius;
                vec2 sample_pos = base_sample_pos + offset;

                if (sample_pos.x < 0.0 || sample_pos.x > 1.0 || 
                    sample_pos.y < 0.0 || sample_pos.y > 1.0) {
                    total_samples += 1.0;
                    continue;
                }

                float scene_depth = texture(u_texture, sample_pos).r;

                if (scene_depth >= test_caster_depth - depth_bias) {
                    hits += 1.0;
                }
                total_samples += 1.0;
            }
        }

        float coverage = hits / total_samples;

        if (coverage > 0.0) {
            // Multiply remaining light by (1 - blockage)
            // This correctly combines overlapping soft shadows
            light *= (1.0 - coverage);
            max_height_diff = max(max_height_diff, height_diff);
        }
    }

    // Shadow is inverse of remaining light
    float shadow_intensity = (1.0 - light) * shadow_strength;

    fragColor = vec4(shadow_intensity, max_height_diff, 0.0, receiver_depth);
}
"""

@register_shader
class ShadowBlur:
    """
    Pass 2: Distance-dependent blur of the shadow map.

    Shadows that traveled farther get blurred more, creating soft penumbra.

    Input: Output from ShadowCast
    Output: Blurred shadow map (same format)
    """
    shader_type = 'standard'
    uniforms = {
        'blur_scale': (GLType.FLOAT, 20.0),  # Max blur radius in pixels
        'min_blur': (GLType.FLOAT, 1.0),  # Minimum blur (keeps some softness)
        'texture_size': (GLType.VEC2, None),
    }
    fragment_code = """
void main() {
    vec2 uv = v_texcoord;
    vec2 texel = 1.0 / texture_size;

    vec4 center = texture(u_texture, uv);
    float travel_dist = center.g;  // How far the shadow traveled
    float receiver_depth = center.a;

    // Blur radius scales with travel distance
    float blur_radius = mix(min_blur, blur_scale, travel_dist);
    int r = int(ceil(blur_radius));

    if (r <= 0) {
        fragColor = center;
        return;
    }

    // Gaussian blur with variable radius
    vec4 sum = vec4(0.0);
    float total_weight = 0.0;
    float sigma = blur_radius / 3.0;
    float sigma2 = sigma * sigma;

    for (int x = -r; x <= r; x++) {
        for (int y = -r; y <= r; y++) {
            vec2 offset = vec2(float(x), float(y));
            vec2 sample_uv = uv + offset * texel;

            vec4 sample_val = texture(u_texture, sample_uv);

            // Only blur shadows that are on surfaces at similar depth
            // This prevents shadow bleeding across depth discontinuities
            float depth_diff = abs(sample_val.a - receiver_depth);
            float depth_weight = exp(-depth_diff * 50.0);

            // Gaussian weight
            float dist2 = float(x*x + y*y);
            float gauss_weight = exp(-dist2 / (2.0 * sigma2));

            float weight = gauss_weight * depth_weight;
            sum += sample_val * weight;
            total_weight += weight;
        }
    }

    fragColor = sum / total_weight;
}
"""


@register_shader
class ShadowComposite:
    """
    Pass 3: Composite shadows onto the original image.

    Input uniforms:
        u_texture: Original color texture
        shadow_map: Blurred shadow map from Pass 2
        depth_map: Original depth map

    Output: Final composited image with shadows
    """
    shader_type = 'standard'
    uniforms = {
        'shadow_map': (GLType.SAMPLER2D, None),
        'depth_map': (GLType.SAMPLER2D, None),
        'shadow_opacity': (GLType.FLOAT, 1.0),  # How dark shadows get
        'shadow_color': (GLType.VEC3, (0.0, 0.0, 0.0)),  # Shadow tint
        'texture_size': (GLType.VEC2, None),
    }
    fragment_code = """
void main() {
    vec2 uv = v_texcoord;

    vec4 color = texture(u_texture, uv);
    vec4 shadow = texture(shadow_map, uv);
    float depth = texture(depth_map, uv).r;

    float shadow_intensity = shadow.r;

    // Apply shadow by darkening toward shadow_color
    vec3 shadowed = mix(color.rgb, shadow_color, shadow_intensity * shadow_opacity);

    fragColor = vec4(vec3(shadowed), color.a);
}
"""


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
class Jet:
    """
    Apply jet colormap to the red channel of a texture.

    Maps input red values [0, 1] to the jet color scale
    (blue -> cyan -> green -> yellow -> red).

    Input uniforms:
        u_texture: Input texture (uses red channel)

    Output: RGB jet-colored representation of input intensity
    """
    shader_type = 'standard'
    uniforms = {
        'offset': (GLType.FLOAT, 0.0),  # -1.0 to 1.0
    }
    fragment_code = """
vec3 jet(float t) {
    // Clamp input to [0, 1]
    t += offset;
    t = clamp(t, 0.0, 1.0);

    // Compute RGB components using piecewise linear approximation
    // Each channel is a trapezoid function centered at different positions
    float r = clamp(1.5 - abs(t - 0.75) * 4.0, 0.0, 1.0);
    float g = clamp(1.5 - abs(t - 0.5) * 4.0, 0.0, 1.0);
    float b = clamp(1.5 - abs(t - 0.25) * 4.0, 0.0, 1.0);

    return vec3(r, g, b);
}

void main() {
    float intensity = texture(u_texture, v_texcoord).r;
    fragColor = vec4(jet(intensity), 1.0);
}
"""

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


# =============================================================================
# NORMALIZATION
# =============================================================================

@register_shader
class MinMaxReduction:
    """
    Reduction shader for finding min/max values in a texture.

    Each output pixel represents the min/max of a 2x2 block from the input.
    This is used internally by the normalize() method for parallel reduction.

    Output format: R=min_value, G=max_value (across all RGB channels)
    """
    shader_type = 'standard'
    uniforms = {
        'texture_size': (GLType.VEC2, None),
    }
    fragment_code = """
void main() {
    vec2 texel = 1.0 / texture_size;

    // Sample 2x2 block
    vec3 s00 = texture(u_texture, v_texcoord + vec2(0.0, 0.0) * texel).rgb;
    vec3 s10 = texture(u_texture, v_texcoord + vec2(1.0, 0.0) * texel).rgb;
    vec3 s01 = texture(u_texture, v_texcoord + vec2(0.0, 1.0) * texel).rgb;
    vec3 s11 = texture(u_texture, v_texcoord + vec2(1.0, 1.0) * texel).rgb;

    // Find min and max across all channels in the 2x2 block
    float min_val = min(min(min(s00.r, s00.g), min(s00.b, s10.r)),
                       min(min(s10.g, s10.b), min(s01.r, s01.g)));
    min_val = min(min_val, min(min(s01.b, s11.r), min(s11.g, s11.b)));

    float max_val = max(max(max(s00.r, s00.g), max(s00.b, s10.r)),
                       max(max(s10.g, s10.b), max(s01.r, s01.g)));
    max_val = max(max_val, max(max(s01.b, s11.r), max(s11.g, s11.b)));

    // Store min in R, max in G
    fragColor = vec4(min_val, max_val, 0.0, 1.0);
}
"""


@register_shader
class NormalizeRemap:
    """
    Remaps texture values from [min, max] to [0, 1].

    Used internally by the normalize() method after min/max calculation.
    """
    shader_type = 'standard'
    uniforms = {
        'min_value': (GLType.FLOAT, 0.0),
        'max_value': (GLType.FLOAT, 1.0),
    }
    fragment_code = """
void main() {
    vec4 color = texture(u_texture, v_texcoord);

    // Avoid division by zero
    float range = max(max_value - min_value, 0.0001);

    // Remap from [min, max] to [0, 1]
    color.rgb = (color.rgb - min_value) / range;
    color.rgb = color.rgb;

    fragColor = color;
}
"""
