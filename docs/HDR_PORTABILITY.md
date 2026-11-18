# HDR and the colour picker: what is assumed about the display

What Melty's HDR path and the `@gui` colour picker
([color_picker_view.py](../meltygui/view/color_picker_view.py)) take for
granted about Lukas's desktop, a patched Hyprland 0.56.2
(`~/Desktop/hyprland-hdr`), and what another system would need. Written
2026-11-18 by Claude from the code and the compositor's patches. The Hyprland
facts were read from its source and its live state; the macOS, iOS and other
compositor facts were **not run** and are marked where they are from memory.

The decisions behind the picker are in
[GUI_REQUIREMENTS.md](GUI_REQUIREMENTS.md), section 17.

## 1. The model everything rests on

- A Melty colour is extended sRGB: 1.0 is the desktop's SDR white, more is
  brighter, a negative channel is outside sRGB ([hdr_color.py](../meltygui/hdr_color.py)).
  It is a multiple of white, never nits.
- Nits appear in two places only: the picker's caption, and the
  `DYNAMIC_PERCEPTUAL` selector, which draws from the light the panel emits.
- Three of the five selectors (`HDR`, `SRGB_PLUS`, `SRGB`) and `PERCEPTUAL`
  need nothing from the display except that 1.0 is shown as white and that
  values above it are shown brighter in proportion. They are portable as
  they are, wherever Melty can present extended range at all.

## 2. What is specific to this desktop

### Melty's output (before the picker existed)

| Assumption | Where | On another system |
| --- | --- | --- |
| The compositor speaks `wp_color_management_v1`. Melty tags its surface extended linear (sRGB primaries) where it can present float buffers, else BT.2020 PQ | [wayland_color.py](../meltygui/core/graphics/wayland_color.py), `Toggles.HDR.output` ("auto", "linear", "pq", "srgb") | Without the protocol Melty presents sRGB and everything above white clips |
| The preferred image description's reference luminance is the user's SDR white | `wayland_color.query_preferred`, `desired_reference` | True on this fork, which patches it to `sdr_max_luminance`; stock Hyprland reports a fixed default there. Other compositors: see section 4 |
| That reference does not include the brightness slider | the fork's `clientImageDescription` | Melty's nits are therefore "nits at 100% brightness" |
| The EGL window is 8 bit on this driver (no 10 bit or float window config), so Melty presents half-float dmabufs of its own instead | [scene_target.py](../meltygui/core/graphics/scene_target.py) `_float_target`, native_wayland `float_buffer` (`EGL_MESA_image_dma_buf_export`, `zwp_linux_dmabuf_v1`, format `AB4H`) | Needs the driver to export a float texture and the compositor to take that format and modifier. Else the 8 bit PQ path: a step of one code is 3.7% of light near 300 nits, and no dither |
| An extended linear surface's 1.0 is the desktop's SDR white, with no ceiling | the fork's `linearSDRWhite` (Renderer.cpp); upstream Hyprland treats 1.0 as 80 nits | On a compositor that does not map it so, Melty's white would not be the desktop's: use `Toggles.HDR.output = "pq"` there |
| A window that presents float buffers has no EGL window | native_wayland `wants_float`, `float_abandon` | NVIDIA's EGL window puts explicit sync on the surface, after which a buffer of Melty's own is a protocol error. So it is decided when the window is made; going back to PQ gives it an EGL window for good |
| Through PQ, Melty's output ends at 10,000 nits, before the compositor's brightness | `melty_pq_encode`, `Room.carry` in the picker | The most that can be emitted is `10,000 x brightness` nits. Only when the float path is not available |
| Without a reading, white is 250 nits | `Toggles.HDR.pq_reference_nits` | A guess; `sdr_white_max=` on the picker overrides it |
| A vertex colour carries at most 64 times white | `Toggles.HDR.vertex_range` | Chips and swatches clip there; the picker's field is a texture and does not |

### The `Display` model ([display_model.py](../meltygui/model/display_model.py))

Used only by `DYNAMIC_PERCEPTUAL`. Every row is this fork's.

| Assumption | Detail |
| --- | --- |
| Hyprland's request socket | `$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket.sock`, found by `geometry_feed.hyprland_socket_path`; the request is `j/monitors all` |
| The JSON keys | `sdrMaxLuminance`, `sdrBrightness` and `colorManagementPreset` exist upstream; `exposure`, `shadowGamma` and `blackOffset` are the fork's |
| Brightness is `exposure` | One linear gain on every source before tone mapping: `emitted white = sdrMaxLuminance x exposure` |
| No event for a brightness change | So a thread asks: every 100 ms, every 8 ms for a second after a change (`PAUSE`, `QUICK`, `LINGER`) |
| The shadow stages, copied from the shader | `emit` / `ask` restate `shadowToneMap` of the fork's `cm_helpers.glsl`: below `sdrMaxLuminance x sdrBrightness x exposure` each channel is sRGB encoded and raised to `shadow_gamma`; then `c - b (1 - exp(-c / b))` with `b = black_offset`. A change to that shader must be repeated here |
| They run per channel in the panel's primaries | Undone in the same channels (`sent` / `seen` in the picker) |
| The panel is the output with an EDID | `/sys/class/drm/card*-<name>/edid`; headless desktops have none. The window is on a headless desktop that the panel mirrors |
| The panel's primaries | Under the preset `hdredid`: the EDID's base block chromaticities (bytes 25 to 34). Under `hdr`: BT.2020. Anything else: not known, and the description the window was given is used, else BT.2020 |
| One panel | The first output with an EDID. Which output a window is really shown on is not looked up |

### What the picker does not model

- The fork's highlight desaturation, roll-off and chroma boost. They were
  off. With any of them on, colours near the panel's peak are not what the
  dynamic field says.
- The fork's HDR gain (`hdrbrightness`). It multiplies every PQ surface,
  Melty's included, so it must be 1 or Melty's white is not the desktop's.
- The compositor's tone map. Melty declares the output's own maximum, so it
  stays off.
- What the monitor does with the signal: its own tone curve, brightness
  limiting, local dimming.
- **The black offset is treated as part of what the panel emits and is
  undone.** If it is there to cancel light the backlight leaks, the light
  that leaves the panel is already what was asked for, and undoing it makes
  the darkest colours about 0.3 nits too bright. Not decided; one branch in
  `ask` and `emit`.

### Fixed numbers in the picker

| Name | Value | Meaning |
| --- | --- | --- |
| `PEAK` | 32 | The most a picker shows, in multiples of white |
| `CEILING` | 10,000 | Nits at which Jzazbz, a PQ space, ends |
| `NOMINAL` | 203 | Nits of white at which the dynamic hue bar's colours are found |
| the gamut | Display P3 | "What the display can show", and the channels `hardware_max` is measured in. Not the panel's real primaries |
| `REACH`, `DYNAMIC_REACH` | 0.4, 0.36 | The chroma at the right edge of the perceptual fields |

The picker's texture is OpenGL (`Picture`: `GL_RGBA16F`). The `@gui` retained
host has no Metal path at all.

## 3. macOS and iOS

Nothing below is built. The iOS host
([MeltyHost.mm](../meltygui/platforms/ios/Host/MeltyHost.mm)) already
presents an extended linear sRGB layer with extended range on; there is no
macOS extended range path in the repository.

| Need | What to do |
| --- | --- |
| Present extended range | A `CAMetalLayer` with an extended linear colour space and `wantsExtendedDynamicRangeContent`, as iOS has. 1.0 is then SDR white by the system's definition, which is Melty's convention: no reference to read, no PQ encode, no 8 bit step |
| The picker's texture | A Metal `rgba16Float` texture behind `Picture.show` / `close`. The rest of the picker is draw list calls |
| `HDR`, `SRGB_PLUS`, `SRGB`, `PERCEPTUAL` | Nothing else. They work in multiples of white |
| Nits of white (`white()`) | Apple reports headroom, a multiple of white (`NSScreen.maximumExtendedDynamicRangeColorComponentValue`, `UIScreen.currentEDRHeadroom`), and, as far as known, no nits. Estimate: `panel peak / headroom` from a per-model table while the panel is the limit, else the brightness fraction times the model's SDR maximum. Or pass `sdr_white_max=` |
| `hardware_max` | Not needed for drawing: the system clips above the current headroom while the layer carries no tone-mapping metadata, so the field shows its own edge. Known in multiples of white, exactly |
| `Display` for `DYNAMIC_PERCEPTUAL` | Another reader behind the same fields: `white` from the estimate, `gain` 1, no `tone()` (there are no shadow stages), `primaries` from the screen's colour space. Screen changes come as notifications, so no thread and no polling |
| What changes with the brightness | On Apple the slider moves SDR white and the headroom. The dynamic field's shape would follow the headroom rather than a gain |

## 4. Another Linux desktop with HDR

From memory, to be checked on the system itself: KWin (Plasma 6) and Mutter
(GNOME 48 on) implement `wp_color_management_v1`, as stock Hyprland does.

| Piece | Elsewhere |
| --- | --- |
| Melty's PQ tag | Works wherever the protocol offers BT.2020 and PQ |
| SDR white | Whatever reference luminance that compositor puts in the preferred description. If it is the user's SDR brightness setting, Melty follows it as here, and a change arrives as `preferred_changed`: an event, no polling |
| Brightness | There is no `exposure`. A brightness setting is the SDR white itself, so `gain` is 1 and `white` is the reference |
| Shadow stages | None: `tone()` is None, and `sent` is a division by white |
| Panel primaries | The preferred description's (`wayland_color.preferred_primaries`, already read) |
| `Display.read` | Returns False without a Hyprland socket. The dynamic picker then uses the caption's white at a brightness of 1 and is right wherever the compositor shows what it is sent. To follow changes there, `Display` needs a second source: the preferred description and its event |
| `hardware_max` | The preferred description's maximum luminance, if the compositor reports the panel's. Not read today |

So on such a desktop every selector works today; only "the dynamic field
follows a brightness change while open" needs the second source for `Display`.

## 5. The patched Hyprland: what could be more standard

It is fine as it is for one desktop. Three changes would remove most of the
code in section 2 that is about this fork, in the order of what they buy.

1. **Shadow gamma on SDR sources, not on the output.** The stage runs in the
   output pass on the whole frame, so it also reshapes surfaces that declare
   absolute light (Melty, any PQ video), which then do not emit what their
   tag says. That is why the picker has to copy the shader and undo it. The
   usual way to get "sRGB values shown through a 2.2 power curve" is to
   decode SDR sources that way as they enter the work buffer (upstream has
   `render:cm_sdr_eotf` and a monitor `sdr_eotf` for it) and leave
   colour-managed HDR surfaces alone. With that, `ask` / `emit`, the EDID
   read and the panel matrices are not needed.
2. **Tell a window on a mirrored desktop about the panel.** A headless
   desktop under `hdredid` has no EDID and describes itself as BT.2020, so a
   client on it is told the wrong primaries (and the desktop's luminances)
   for the panel it is shown on. Giving the mirrored desktop the panel's
   image description makes the protocol's answer the right one.
3. **An event for a monitor rule change.** One line on the event socket when
   `exposure` or a tuning field changes replaces the polling thread. Still
   private to Hyprland, but cheap.

Smaller, and a matter of taste:

- `exposure` itself has no standard counterpart, and that is acceptable: it
  is the one way to make clients that ignore the reported white dim with the
  rest (section 17 of the requirements has the reasoning). Reporting the
  reference without it is what keeps well behaved clients from being dimmed
  twice.
- Reporting `sdr_max_luminance` as the reference luminance is the right use
  of the protocol.
- `hdrbrightness` breaks the one promise the protocol makes, that a declared
  light is the light shown. Keep it at 1, or limit it to surfaces that did
  not set a reference of their own.
- The black offset is a calibration of the panel and belongs at the output.
  Whether a client should see through it is the open point in section 2.
