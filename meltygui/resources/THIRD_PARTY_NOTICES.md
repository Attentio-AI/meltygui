# Bundled resources

- JetBrains Mono: JetBrains s.r.o., SIL Open Font License 1.1.
  License: `jetbrains-weights/OFL.txt`. Source: https://github.com/JetBrains/JetBrainsMono
- DejaVu Sans: Bitstream copyright and DejaVu public-domain changes.
  License: `dejavu/LICENSE.txt`. Source: https://dejavu-fonts.github.io/
- Font Awesome 5.15.4 font (stored under the historical filename
  `fontawesome-webfont.ttf`): Fonticons, Inc., SIL Open Font License 1.1.
  License: `fontawesome-LICENSE.txt`.
  Source: https://github.com/FortAwesome/Font-Awesome/tree/5.15.4
- Studio Small 09 HDR environment: Poly Haven, CC0.
  Source: https://polyhaven.com/a/studio_small_09
  License: https://polyhaven.com/license

The adapted pyimgui window integration retains its license in
`meltygui/windows/backends/PYIMGUI_LICENSE`.

Native support distributions carry their own licenses. In particular, the
GL-enabled PyCUDA wheel includes NVIDIA's cuRAND runtime and its CUDA 12.1 EULA;
that runtime is not covered by MeltyGUI's project license. The NVIDIA driver
and CUDA compiler/toolkit are not bundled in MeltyGUI wheels.
