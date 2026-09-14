"""Residual backgrounds and the draw-list renderer's text colour policy.

Tuple colours remain absolute extended sRGB. Style opts into signed shifts;
the renderer works in linear scRGB, just like hdr_color.
"""
import math


class Style(tuple):
    def __new__(cls, rgb=(0, 0, 0), *, absolute=False):
        values = tuple(float(value) for value in rgb)
        if len(values) not in (3, 4) or not all(map(math.isfinite, values)):
            raise ValueError("Style requires three finite channels and optional alpha")
        style = super().__new__(cls, values)
        style.absolute = bool(absolute)
        return style

    def __repr__(self):
        return f"Style({tuple(self)!r}, absolute={self.absolute!r})"

    def __getnewargs_ex__(self):
        return (tuple(self),), {"absolute": self.absolute}

    def __eq__(self, other):
        if not isinstance(other, tuple):
            return NotImplemented
        # Plain tuples mean absolute. Metadata must participate in dirty
        # detection: switching black from residual to absolute is an edit.
        absolute = other.absolute if isinstance(other, Style) else True
        return self.absolute == absolute and tuple.__eq__(self, other)

    def __ne__(self, other):
        equal = self.__eq__(other)
        return NotImplemented if equal is NotImplemented else not equal

    __hash__ = tuple.__hash__


def draw_background(style, parent):
    """Resolve one deferred background to linear RGBA for the paint pass.

    Positive residuals move toward reference white, negative toward black.
    Absolute colours keep HDR headroom and wide gamut. Alpha composites in
    linear light against the enclosing background before descendants use it.
    """
    from src.lsd.gl_gui.hdr_color import srgb_to_linear, linear_to_srgb

    if style is None:
        return parent
    if isinstance(style, Style) and not style.absolute:
        base = tuple(linear_to_srgb(channel) for channel in parent[:3])
        rgb = tuple(channel + max(-1.0, min(1.0, delta)) *
                    (max(0.0, 1.0 - channel) if delta >= 0 else channel)
                    for channel, delta in zip(base, style))
    else:
        rgb = style[:3]
    alpha = max(0.0, min(1.0, style[3])) if len(style) == 4 else 1.0
    return tuple(srgb_to_linear(channel) * alpha + behind * (1 - alpha)
                 for channel, behind in zip(rgb, parent)) + (1.0,)


def adjust_text_color():
    """The single text-colour policy, executed per glyph pixel on the GPU.

    Edit this function to change text appearance. The suggestion and sampled
    background are linear scRGB. Preserve readable suggestions, otherwise
    move toward black or reference white by the smallest necessary amount.
    Coverage/opacity remain the draw-list renderer's responsibility.
    """
    return """
    uniform sampler2D StyleContext;
    uniform int DynamicStyles;
    uniform float TextContrast;

    vec3 adjust_text_color(vec3 suggestion) {
        vec3 background = texelFetch(StyleContext, ivec2(gl_FragCoord.xy), 0).rgb;
        vec3 weights = vec3(0.2126, 0.7152, 0.0722);
        float behind = max(0.0, dot(background, weights));
        float proposed = max(0.0, dot(suggestion, weights));
        float ratio = (max(behind, proposed) + 0.05) /
                      (min(behind, proposed) + 0.05);
        if (ratio >= TextContrast) return suggestion;

        float dark = (behind + 0.05) / TextContrast - 0.05;
        float light = (behind + 0.05) * TextContrast - 0.05;
        float toDark = dark >= 0.0 && proposed > 0.0
            ? clamp((proposed - dark) / proposed, 0.0, 1.0) : 2.0;
        float toLight = light <= 1.0 && proposed < 1.0
            ? clamp((light - proposed) / (1.0 - proposed), 0.0, 1.0) : 2.0;
        return toDark <= toLight ? mix(suggestion, vec3(0), toDark)
                                : mix(suggestion, vec3(1), toLight);
    }
    """
