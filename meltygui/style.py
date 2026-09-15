"""Residual backgrounds and the draw-list renderer's text colour policy.

Tuple colours remain absolute extended sRGB. Style opts into signed shifts;
the renderer works in linear scRGB, just like hdr_color.
"""
import math


class Style(tuple):
    def __new__(cls, rgb=(0, 0, 0), *, absolute=False, shadow_offset=None,
                font_size=None, font_weight=None, tint_fn=None,
                font_size_fn=None, font_weight_fn=None, shadow_fn=None):
        values = tuple(float(value) for value in rgb)
        if len(values) not in (3, 4) or not all(map(math.isfinite, values)):
            raise ValueError("Style requires three finite channels and optional alpha")
        style = super().__new__(cls, values)
        style.absolute = bool(absolute)
        for name, fn in (("tint_fn", tint_fn), ("font_size_fn", font_size_fn),
                         ("font_weight_fn", font_weight_fn), ("shadow_fn", shadow_fn)):
            if fn is not None and not callable(fn):
                raise TypeError(f"{name} must be callable or None")
            setattr(style, name, fn)
        for name, value in (("shadow_offset", shadow_offset), ("font_size", font_size),
                            ("font_weight", font_weight)):
            if value is not None:
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError(f"{name} must be finite")
            setattr(style, name, value)
        return style

    def __repr__(self):
        return (f"Style({tuple(self)!r}, absolute={self.absolute!r}, "
                f"shadow_offset={self.shadow_offset!r}, font_size={self.font_size!r}, "
                f"font_weight={self.font_weight!r}, tint_fn={self.tint_fn!r}, "
                f"font_size_fn={self.font_size_fn!r}, font_weight_fn={self.font_weight_fn!r}, "
                f"shadow_fn={self.shadow_fn!r})")

    def __getnewargs_ex__(self):
        return (tuple(self),), {"absolute": self.absolute, "shadow_offset": self.shadow_offset,
                               "font_size": self.font_size, "font_weight": self.font_weight,
                               "tint_fn": self.tint_fn, "font_size_fn": self.font_size_fn,
                               "font_weight_fn": self.font_weight_fn, "shadow_fn": self.shadow_fn}

    def __eq__(self, other):
        if not isinstance(other, tuple):
            return NotImplemented
        # Plain tuples mean absolute. Metadata must participate in dirty
        # detection: switching black from residual to absolute is an edit.
        absolute = other.absolute if isinstance(other, Style) else True
        return (self.absolute == absolute
                and all(getattr(self, name, None) == getattr(other, name, None)
                        for name in ("shadow_offset", "font_size", "font_weight", "tint_fn",
                                     "font_size_fn", "font_weight_fn", "shadow_fn"))
                and tuple.__eq__(self, other))

    def __ne__(self, other):
        equal = self.__eq__(other)
        return NotImplemented if equal is NotImplemented else not equal

    __hash__ = tuple.__hash__


def default_scalar_accumulation(context, residual):
    """Default size, weight, and shadow policy; also an explicit subtree reset."""
    return context + residual


def _scalar(fn, context, residual):
    value = float(fn(context, residual))
    if not math.isfinite(value):
        raise ValueError("Style accumulation callback must return a finite scalar")
    return value


def _font_value(value, absolute, base):
    return value(base) if callable(value) else (value if absolute else base + value)


def _font_step(previous, absolute, fn, residual):
    # Evaluate against the selected font's real base size/weight, not a delta
    # from zero. An explicit base font inside a font view keeps its own context.
    return lambda base: _scalar(fn, _font_value(previous, absolute, base), residual)


def resolve_font_style(style, parent=(0.0, 0.0, False, False)):
    """Compose inherited policies; evaluate nonlinear steps at font selection.

    The first four fields keep the existing delta/absolute representation.
    Custom functions append two inherited policies and may turn a field into
    a deferred scalar calculation. This preserves native font selection and
    avoids guessing the font family/base size before the view requests it.
    """
    values = list(parent[:4])
    functions = list(parent[4:] or (default_scalar_accumulation,) * 2)
    for i, name in enumerate(('font_size', 'font_weight')):
        override = getattr(style, name + '_fn', None)
        if override is not None:
            functions[i] = override
        value = getattr(style, name, None)
        if value is not None:
            if style.absolute:
                values[i], values[i + 2] = value, True
            elif functions[i] is default_scalar_accumulation and not callable(values[i]):
                values[i] += value
            else:
                values[i] = _font_step(values[i], values[i + 2], functions[i], value)
                values[i + 2] = True
    if any(fn is not default_scalar_accumulation for fn in functions):
        values.extend(functions)
    return tuple(values)


def evaluate_font_style(context, size, weight):
    return (_font_value(context[0], context[2], size),
            _font_value(context[1], context[3], weight))


def resolve_shadow_offset(style, parent=0.0, shadow_fn=None):
    """Signed shadow depth; omitted values inherit, absolute values bypass policy."""
    offset = getattr(style, 'shadow_offset', None)
    if offset is None:
        return parent
    if style.absolute:
        return offset
    fn = getattr(style, 'shadow_fn', None)
    if fn is None:
        fn = shadow_fn if shadow_fn is not None else default_scalar_accumulation
    return _scalar(fn, parent, offset)


def default_tint_accumulation(context_tint, tint_res):
    """Current extended-sRGB policy: positive toward white, negative toward zero."""
    return tuple(channel + max(-1.0, min(1.0, delta)) *
                 (max(0.0, 1.0 - channel) if delta >= 0 else channel)
                 for channel, delta in zip(context_tint, tint_res))


def draw_background(style, parent, tint_fn=None):
    """Resolve one deferred background to linear RGBA for the paint pass.

    tint_fn receives extended-sRGB triples, before linear conversion/compositing.
    The default moves positive residuals toward white, negative toward black.
    Absolute colours keep HDR headroom and wide gamut. Alpha composites in
    linear light against the enclosing background before descendants use it.
    """
    from meltygui.hdr_color import srgb_to_linear
    from meltygui.hdr_color import linear_to_srgb

    if style is None:
        return parent
    if isinstance(style, Style) and not style.absolute:
        base = tuple(linear_to_srgb(channel) for channel in parent[:3])
        accumulate = getattr(style, 'tint_fn', None)
        if accumulate is None:
            accumulate = tint_fn if tint_fn is not None else default_tint_accumulation
        rgb = tuple(float(c) for c in accumulate(base, tuple(style[:3])))
        if len(rgb) != 3 or not all(map(math.isfinite, rgb)):
            raise ValueError("tint_fn must return three finite extended-sRGB channels")
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
