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
    offset_factor = 0.04
    uniforms = {
        'light_dir': (GLType.VEC2, (-0.5 * offset_factor, 1.6 * offset_factor)),
        'height_scale': (GLType.FLOAT, 0.5),
        'blur_scale': (GLType.FLOAT, 0.3),
        # CONTACT-HARDENING curve: how fast the penumbra grows with the
        # fragment's distance from the caster's silhouette edge (measured
        # in-shader by bisecting along light_dir, normalized 0 at contact,
        # 1 at the shadow tip). blur_scale stays the magnitude at the far
        # end; this shapes the ramp: < 1 blooms the blur rapidly just past
        # the contact edge, 1 = linear growth, > 1 stays crisp for most of
        # the shadow and only softens the tip. 0 = the legacy behavior
        # (uniform blur across the whole shadow).
        'blur_exponent': (GLType.FLOAT, 1.0),
        'max_steps': (GLType.INT, 64),
        'blur_samples': (GLType.INT, 8),
        # Occlusion each caster hit contributes before the depth gap decay:
        # the base darkness of a shadow right at its caster (per-sample,
        # accumulated into coverage).
        'hit_strength': (GLType.FLOAT, 0.7),
        # How fast that contribution decays per unit of caster/receiver
        # depth gap: higher = deep stacks fade their shadows out sooner
        # (contribution clamps at 0, never lightens).
        'hit_falloff': (GLType.FLOAT, 14.0),
        'depth_bias': (GLType.FLOAT, 0.00),
        'surface_threshold': (GLType.FLOAT, -10.0),
        'min_height_diff': (GLType.FLOAT, 0.000),
        'shadow_strength': (GLType.FLOAT, 0.4),
        # Scale applied to raw depth-texture reads. Helps the caller bind the
        # UI's F16 rank mask directly instead of a pre-normalized RGBA8 copy:
        # 8-bit quantization there was ~6.6 depth slices per quantum, so
        # caster/receiver gaps rounded differently every time a window's
        # depth slot changed and shadow intensity visibly wandered.
        'depth_scale': (GLType.FLOAT, 1.0),
        'texture_size': (GLType.VEC2, None),
    }
    fragment_code = """
void main() {
    vec2 uv = v_texcoord;
    float receiver_depth = texture(u_texture, uv).r * depth_scale;
     
    if (receiver_depth < surface_threshold) {
        fragColor = vec4(0.0, 0.0, 0.0, 0.0);
        return;
    }

    vec2 light_normalized = normalize(light_dir);
    float min_offset = 0.0;

    // float min_offset = (0.35) * height_scale;
    float depth_step = 1.0 / float(max_steps);

    // Start with full light, each caster blocks some
    float light = 1.0;
    float max_height_diff = 0.0;

    for (float i = 0.5; i <= 16.0; i++) {
        float test_caster_depth = receiver_depth + float(i) * depth_step;

        float height_diff = test_caster_depth - receiver_depth;

        if (height_diff < min_height_diff) {
            continue;
        }

        float shadow_offset = max(height_diff * height_scale, min_offset);
        vec2 base_sample_pos = uv + light_normalized * shadow_offset;

        // Contact-hardening: the penumbra should grow with the fragment's
        // DISTANCE FROM THE CASTER EDGE along the shadow, not just the
        // depth gap (which is constant across a flat caster's whole
        // shadow, making the blur uniform from contact edge to tip). The
        // caster lies toward +light_dir from any shadowed fragment, so
        // bisect [0, shadow_offset] for the nearest sample inside the
        // caster: fragments hugging the silhouette find it almost
        // immediately (edge_dist ~ 0, crisp), fragments at the shadow tip
        // only at full offset (edge_dist ~ 1, full blur). Gated on the
        // base sample actually hitting the caster — penumbra-fringe
        // fragments whose center sample misses keep edge_dist = 1 (they
        // ARE the soft tail). Assumes a contiguous caster interval, true
        // for the UI's convex rects/strips.
        float edge_dist = 1.0;
        float d_at_offset = texture(u_texture, base_sample_pos).r * depth_scale;
        if (blur_exponent > 0.0
                && d_at_offset >= test_caster_depth - depth_bias) {
            float t_lo = 0.0;
            float t_hi = shadow_offset;
            for (int b = 0; b < 5; b++) {
                float mid = 0.5 * (t_lo + t_hi);
                float d_mid = texture(u_texture,
                                      uv + light_normalized * mid).r
                              * depth_scale;
                if (d_mid >= test_caster_depth - depth_bias) {
                    t_hi = mid;
                } else {
                    t_lo = mid;
                }
            }
            edge_dist = t_hi / max(shadow_offset, 1e-6);
        }
        // blur_exponent = 0 -> pow term is 1 -> legacy uniform blur.
        float blur_radius = height_diff * blur_scale
                            * pow(max(edge_dist, 1e-4), blur_exponent);

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

                float scene_depth = texture(u_texture, sample_pos).r * depth_scale;

                if (scene_depth >= test_caster_depth - depth_bias) {
                    // Clamped at 0 so a large falloff fades a slice's
                    // contribution out instead of going negative and
                    // eating other slices' coverage.
                    hits += max(0.0, hit_strength - height_diff * hit_falloff);
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
        # Resolution of shadow_map. May be lower than the screen when the shadow
        # map is downscaled for performance; used to drive the bilateral upsample.
        'shadow_size': (GLType.VEC2, None),
        # How aggressively the upsample snaps the shadow to depth edges. Higher =
        # crisper edges (less fringing), lower = softer. Depths are in [0, 1].
        'depth_sharpness': (GLType.FLOAT, 50.0),
        # Same sample depth scale as ShadowCast's depth_scale - depth_map is the
        # raw R16 depth mask, and s.a (receiver depth baked by ShadowCast) is
        # already scaled, so the bilateral compare needs both in the same units.
        'depth_scale': (GLType.FLOAT, 1.0),
        # Glow light buffer (TileCacheMasked add_glow marks, low-res RGBA16F):
        # rgb = accumulated emitted light, already depth-gated per emitter at
        # stamp time (the glow stamp shader samples the depth rank mask and
        # only lands light between the emitter's root-window surface and its
        # own depth). Composite-side the light cancels shadow where it falls
        # (glow_shadow_cut) then adds its emission on top (glow_strength).
        'glow_map': (GLType.SAMPLER2D, None),
        'glow_strength': (GLType.FLOAT, 0.0),   # 0 disables the whole path
        'glow_shadow_cut': (GLType.FLOAT, 1.0),
        # Specular rim on the LIT edge of raised surfaces - the edge facing
        # the light source (opposite side from the cast shadow). The shader
        # marches the full-res depth mask toward the light: a lower surface
        # within specular_bevel px marks the silhouette edge, and the pixel
        # is shaded as if the edge were a quarter-round bevel of that
        # radius. light_dir must match ShadowCast's so the highlight always
        # sits opposite the shadow.
        'light_dir': (GLType.VEC2, (-0.5, 1.6)),
        # Bevel radius in px = width of the highlight rim, the apparent
        # roundness of the edge. 0 disables the pass.
        'specular_bevel': (GLType.FLOAT, 0.0),
        # Microfacet-style roughness in (0, 1]: low = tight bright rim
        # hugging the edge profile, high = broad dim sheen across the bevel.
        'specular_roughness': (GLType.FLOAT, 0.4),
        # Peak highlight strength added to the frame (white is).
        'specular_strength': (GLType.FLOAT, 1.0),
        # Fade of the highlight ALONG perpendicular lit edges, in px: brightness
        # peaks at the owning window's lit corner (where the two lit edges
        # meet) and runs out over this distance scanning away from it. The
        # corner comes from CPU-side window geometry (win_mask/win_rects),
        # interpolated analytically per fragment - perfectly smooth and it
        # moves with the window. 0 = uniform rim.
        'specular_fade': (GLType.FLOAT, 300.0),
        # Adapts that fade length to the owning window's own size: per axis
        # the fade runs over min(specular_fade, rel * edge_length). Big
        # windows keep the fixed specular_fade look; small windows fade
        # out within their own edge instead of holding a uniform bright
        # rim. 0 disables the adaptation (pure fixed-f fade).
        'specular_fade_rel': (GLType.FLOAT, 0.6),
        # The frameless OS window's frame (titlebar/etc): its rect sits
        # at frame_origin in the framebuffer, frame_size wide, corners of
        # frame_radius, on a TRANSPARENT background. Inside that rounded rect the
        # composite is the usual darkening; outside it - the shadow margin
        # and the cut corners - there is nothing but the shadow, so the
        # output becomes the shadow itself with premultiplied alpha
        # (shadow_color·s, s). frame_size (0, 0) = no frame, the whole
        # framebuffer is drawn.
        'frame_origin': (GLType.VEC2, (0.0, 0.0)),
        'frame_radius': (GLType.FLOAT, 0.0),
        'frame_size': (GLType.VEC2, (0.0, 0.0)),
        # Window-occlusion mask (blit_offscreen._build_window_mask): R16,
        # each dispatched window's rounded rect stamped back-to-front at
        # rank (i+1)/1024. The specular pass decodes the rank at a
        # fragment to find its owning window...
        'win_mask': (GLType.SAMPLER2D, None),
        # ...and texelFetches that window's fb-space rect (x0, y0, x1, y1)
        # from this 256x1 RGBA32F table, filled in the same z order.
        'win_rects': (GLType.SAMPLER2D, None),
        # Depth falloff of the highlight: intensity decays as
        # exp(-receiver_depth * rate), so surfaces near the floor catch
        # the full highlight and high-stacked ones progressively lose it.
        # Depth is in depth_scale'd units (one layer slot ~ 0.3 at the
        # default 64/32 layer/depth ratio). 0 = depth-independent.
        'specular_depth_falloff': (GLType.FLOAT, 0.0),
        # Minimum depth drop (in depth_scale'd units) that counts as a
        # silhouette edge - rejects same-surface rasterization noise.
        'specular_depth_eps': (GLType.FLOAT, 0.001),
        # Per-px slope allowance added to that threshold as the march gets
        # farther: a sample only reads as an edge when it sits more than
        # eps + tol*distance below the start. Marks interpolate rank
        # across their quad (per-corner values), so a single background
        # can slope smoothly - this keeps a tilted surface from reading
        # as a phantom edge (only the short bevel march remains, so the
        # allowance rarely exceeds eps in practice).
        'specular_slope_tol': (GLType.FLOAT, 0.0002),
        'texture_size': (GLType.VEC2, None),
    }
    fragment_code = """
// Distance in px from `uv` to the nearest LOWER surface marching along
// `dir` (a unit, in pixels), up to max_d. Coarse 16-step march to bracket
// the edge, then a short bisection so long fades don't band into visible
// steps. Returns max_d when no edge is found in range.
float _spec_edge_dist(vec2 uv, vec2 dir, float d0, float max_d, vec2 texel) {
    float lo = 0.0;
    float hi = -1.0;
    for (int i = 1; i <= 16; i++) {
        float t = max_d * float(i) / 16.0;
        float d_s = texture(depth_map, uv + dir * t * texel).r * depth_scale;
        // Slope-relative threshold: marks interpolate depth across their
        // quad, so a surface can tilt — only a DROP steeper than the
        // per-px allowance counts as a silhouette edge.
        if (d_s < d0 - specular_depth_eps - specular_slope_tol * t) {
            hi = t; break;
        }
        lo = t;
    }
    if (hi < 0.0) return max_d;
    for (int b = 0; b < 4; b++) {
        float mid = 0.5 * (lo + hi);
        float d_m = texture(depth_map, uv + dir * mid * texel).r * depth_scale;
        if (d_m < d0 - specular_depth_eps - specular_slope_tol * mid) {
            hi = mid;
        } else {
            lo = mid;
        }
    }
    return hi;
}

// Extended sRGB (mirrored for negatives, unbounded above) — hdr_color.py.
vec3 melty_encode(vec3 v) {
    vec3 a = abs(v);
    vec3 e = mix(a * 12.92, 1.055 * pow(a, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, a));
    return sign(v) * e;
}
vec3 melty_decode(vec3 c) {
    vec3 a = abs(c);
    vec3 l = mix(a / 12.92, pow((a + 0.055) / 1.055, vec3(2.4)), step(0.04045, a));
    return sign(c) * l;
}

void main() {
    vec2 uv = v_texcoord;

    vec4 color = texture(u_texture, uv);
    float depth = texture(depth_map, uv).r * depth_scale;

    // Joint bilateral upsample of the (possibly low-res) shadow map. A plain
    // bilinear fetch smears the shadow silhouette across the crisp rounded-rect
    // edges, producing visible fringing. Instead we gather the four nearest
    // low-res taps and weight each by (a) the usual bilinear weight and (b) how
    // closely its receiver depth (stored in shadow.a by ShadowCast) matches the
    // full-res depth at this pixel. Taps belonging to a different surface are
    // rejected, so the shadow snaps back to the high-res geometry edge.
    vec2 texel = 1.0 / shadow_size;
    vec2 sample_pos = uv * shadow_size - 0.5;
    vec2 base = floor(sample_pos);
    vec2 frac = sample_pos - base;

    float shadow_sum = 0.0;
    float weight_sum = 0.0;

    for (int x = 0; x <= 1; x++) {
        for (int y = 0; y <= 1; y++) {
            vec2 tap = (base + vec2(float(x), float(y)) + 0.5) * texel;
            vec4 s = texture(shadow_map, tap);

            float wx = (x == 0) ? (1.0 - frac.x) : frac.x;
            float wy = (y == 0) ? (1.0 - frac.y) : frac.y;
            float spatial = wx * wy;

            float depth_weight = exp(-abs(s.a - depth) * depth_sharpness);

            float w = spatial * depth_weight;
            shadow_sum += s.r * w;
            weight_sum += w;
        }
    }

    // Fall back to a straight bilinear fetch if every tap was rejected (e.g. a
    // thin feature with no depth-matching neighbor) so we never punch a hole.
    float shadow_intensity = (weight_sum > 0.0001)
        ? (shadow_sum / weight_sum)
        : texture(shadow_map, uv).r;

    // Glow rects as light sources: sample the low-res light buffer (bilinear
    // sampler upscales the smooth falloff for free; depth gating already
    // happened per emitter at stamp time). Light eats shadow first, then
    // adds its own emission below.
    vec3 glow_light = vec3(0.0);
    if (glow_strength > 0.0) {
        glow_light = texture(glow_map, uv).rgb;
        float glum = dot(glow_light, vec3(0.299, 0.587, 0.114));
        shadow_intensity *= max(0.0, 1.0 - glum * glow_shadow_cut);
    }

    // The scene is LINEAR scRGB (hdr_color.py) but every knob here —
    // shadow_color, the opacities, the glow and specular strengths — was
    // tuned against the sRGB-ENCODED frame, so the mix runs in that domain
    // and decodes at the end: the same look, and a shadow_color of
    // (0, 0.02, 0.05) stays the blue-black it reads as (taken as linear
    // light it is sRGB (0, 0.15, 0.25): the blue shadows of 09-07).
    // Extended curves: values above 1 and below 0 pass through.
    vec3 color_enc = melty_encode(color.rgb);
    vec3 shadowed = mix(color_enc, shadow_color, shadow_intensity * shadow_opacity);
    shadowed += melty_encode(glow_light) * glow_strength;

    // Specular bevel highlight on the lit edge. March toward the light in
    // the full-res depth mask: the first sample that drops below this
    // pixel's surface is the silhouette edge, so this pixel sits on the
    // rim that faces the light (shadows are displaced the OPPOSITE way,
    // toward -light_dir, so highlight and shadow stay consistent). Convex
    // rounded corners fall out for free — the march follows the mask.
    if (specular_bevel > 0.0) {
        vec2 texel_full = 1.0 / texture_size;
        vec2 light_n = normalize(light_dir);
        float edge_t = _spec_edge_dist(uv, light_n, depth,
                                       specular_bevel, texel_full);
        if (edge_t < specular_bevel) {
            // Quarter-round bevel of radius specular_bevel: at the edge
            // the normal tilts fully toward the light, flattening to
            // straight-up one bevel radius in.
            float f = edge_t / specular_bevel;          // 0 edge .. 1 flat
            float sin_t = 1.0 - f;
            float cos_t = sqrt(max(0.0, 1.0 - sin_t * sin_t));
            vec3 n = vec3(light_n * sin_t, cos_t);
            // Light at 45 deg elevation from the light_dir side, viewer
            // straight down; Blinn half-vector with a roughness-driven
            // exponent (rough = broad+dim, smooth = tight+bright).
            vec3 L = normalize(vec3(light_n, 1.0));
            vec3 H = normalize(L + vec3(0.0, 0.0, 1.0));
            float rough = clamp(specular_roughness, 0.02, 1.0);
            float shininess = max(1.0, 0.5 / (rough * rough));
            float spec = pow(max(dot(n, H), 0.0), shininess);
            spec *= 1.0 - 0.5 * rough;

            // Fade along the edge: brightness peaks at the owning
            // window's LIT corner and dies out scanning away from it
            // along either edge. The corner comes from CPU-side window
            // geometry: the win_mask rank at this fragment identifies the
            // topmost window, win_rects holds its fb-space rect, and the
            // fade is an analytic distance field from the rect's corner
            // toward +light_dir. No depth-mask walks — the field is
            // perfectly smooth and translates with the window, so it
            // cannot dash, stair-step, or wobble as views move. Fragments
            // no window owns (floor-level marks) keep fade = 1.
            float fade = 1.0;
            if (specular_fade > 0.0) {
                float zn = texture(win_mask, uv).r;
                if (zn > 0.0) {
                    int idx = clamp(int(round(zn * 1024.0)) - 1, 0, 255);
                    vec4 r = texelFetch(win_rects, ivec2(idx, 0), 0);
                    vec2 frag_px = uv / texel_full;
                    vec2 corner = vec2(
                        light_n.x > 0.0 ? r.z : r.x,
                        light_n.y > 0.0 ? r.w : r.y);
                    vec2 d = abs(frag_px - corner);
                    vec2 ext = max(r.zw - r.xy, vec2(1.0));
                    float u_max = 0.0;
                    if (abs(light_n.x) > 1e-3) {
                        float fl = (specular_fade_rel > 0.0)
                            ? min(specular_fade, specular_fade_rel * ext.x)
                            : specular_fade;
                        u_max = max(u_max, d.x / max(fl, 1.0));
                    }
                    if (abs(light_n.y) > 1e-3) {
                        float fl = (specular_fade_rel > 0.0)
                            ? min(specular_fade, specular_fade_rel * ext.y)
                            : specular_fade;
                        u_max = max(u_max, d.y / max(fl, 1.0));
                    }
                    fade = 1.0 - smoothstep(0.0, 1.0, u_max);
                }
            }
            // Depth falloff: low surfaces (near the floor) keep the full
            // highlight, high-stacked ones fade out exponentially.
            if (specular_depth_falloff > 0.0) {
                fade *= exp(-max(depth, 0.0) * specular_depth_falloff);
            }
            shadowed += vec3(spec * fade * specular_strength);
        }
    }

    // Frameless-window frame: coverage of the content's rounded rect at this
    // pixel (gl_FragCoord: pixel centres, origin bottom-left — the rect is
    // symmetric so no flip). Outside it only the shadow exists: emit it as
    // premultiplied alpha so the compositor blends the desktop through.
    float frame_cov = 1.0;
    if (frame_size.x > 0.0 && frame_size.y > 0.0) {
        vec2 half_size = frame_size * 0.5;
        vec2 fd = abs(gl_FragCoord.xy - frame_origin - half_size) - (half_size - vec2(frame_radius));
        float fdist = length(max(fd, vec2(0.0))) + min(max(fd.x, fd.y), 0.0) - frame_radius;
        frame_cov = 1.0 - smoothstep(-0.5, 0.5, fdist);
    }
    float bg_alpha = shadow_intensity * shadow_opacity;
    vec3 bg_rgb = melty_decode(shadow_color) * bg_alpha;
    fragColor = vec4(mix(bg_rgb, melty_decode(shadowed), frame_cov), mix(bg_alpha, color.a, frame_cov));
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
    // No ceiling and no floor: the working space is linear scRGB
    // (hdr_color.py) -- values above 1.0 are real highlights and NEGATIVE
    // components are colours outside sRGB (a BT.2020 green is
    // (-1.9, 3.6, -0.3)). Flooring them here collapsed every wide-gamut
    // pixel to the sRGB gamut before the present pass could convert
    // primaries, so HDR screenshots showed muted (09-10). The present
    // pass clamps AFTER its primaries matrix, where it is safe.
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

// sRGB <-> BT.2020 primaries (linear, D65; column-major, same numbers as
// hdr_color.MELTY_SRGB_TO_BT2020). HSL is done in BT.2020 because it
// contains everything a panel shows (P3 included): a wide colour is scRGB
// with NEGATIVE components (hdr_color.py) and HSL cannot take those. The
// old max(rgb, 0) collapsed such pixels to the sRGB gamut, so HDR
// screenshots showed muted even at saturation 1 (09-10).
const mat3 SRGB_TO_BT2020 = mat3(
    0.6274039, 0.0690973, 0.0163914,
    0.3292830, 0.9195404, 0.0880133,
    0.0433131, 0.0113623, 0.8955953);
const mat3 BT2020_TO_SRGB = mat3(
    1.6604910, -0.1245505, -0.0181508,
    -0.5876411, 1.1328999, -0.1005789,
    -0.0728499, -0.0083494, 1.1187297);

void main() {
    vec4 color = texture(u_texture, v_texcoord);
    if (hue_shift == 0.0 && saturation == 1.0 && lightness == 0.0) {
        fragColor = color;  // identity: bit-exact passthrough
        return;
    }
    vec3 wide = max(SRGB_TO_BT2020 * color.rgb, 0.0);
    // HSL is defined on [0, 1]; an HDR pixel (a component above 1.0) is
    // normalised by its brightest component first and scaled back after,
    // so highlights keep their level through the edit.
    float scale = max(max(max(wide.r, wide.g), wide.b), 1.0);
    vec3 hsl = rgb2hsl(wide / scale);
    
    // Apply adjustments
    hsl.x = fract(hsl.x + hue_shift * 0.5);  // Hue shift
    hsl.y = clamp(hsl.y * saturation, 0.0, 1.0);  // Saturation
    hsl.z = clamp(hsl.z + lightness, 0.0, 1.0);   // Lightness
    
    fragColor = vec4(BT2020_TO_SRGB * (hsl2rgb(hsl) * scale), color.a);
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
        # (min, max) live in the .r/.g of a 1x1 texture stored on the GPU by
        # texture_min_max - sampled here so the range is read straight from the
        # GPU with no CPU readback.
        'min_max_tex': (GLType.SAMPLER2D, None),
    }
    fragment_code = """
void main() {
    vec4 color = texture(u_texture, v_texcoord);

    vec2 mm = texelFetch(min_max_tex, ivec2(0, 0), 0).rg;
    float min_value = mm.x;
    float max_value = mm.y;

    // Avoid division by zero
    float range = max_value - min_value;
    if (abs(range) < 1e-20) range = 1.0;

    // Remap from [min, max] to [0, 1]
    color.rgb = (color.rgb - min_value) / range;

    fragColor = color;
}
"""


@register_shader
class Multiply:
    """Scale every colour channel by `factor` (alpha untouched): a plain
    exposure change, the dim behind draw_texture's crop selection."""
    shader_type = 'standard'
    uniforms = {
        'factor': (GLType.FLOAT, 1.0),
    }
    fragment_code = """
void main() {
    vec4 color = texture(u_texture, v_texcoord);
    fragColor = vec4(color.rgb * factor, color.a);
}
"""
