"""Build isolated bare and cached Rust decorators without replacing the installed extension."""
import argparse
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / 'native/gui_prototype'
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    for name in ('Cargo.toml', 'Cargo.lock'):
        shutil.copy2(source / name, destination / name)
    shutil.copytree(source / 'src', destination / 'src')
    shutil.copy2(Path(__file__).with_name('native_bare_gui.rs'), destination / 'src/bare.rs')
    shutil.copy2(Path(__file__).with_name('native_cached_bare_gui.rs'), destination / 'src/cached_bare.rs')
    path = destination / 'src/lib.rs'
    text = path.read_text()
    # Preserve the current renderer's identity, ImGui grouping, geometry and
    # cleanup code. Replace only dependency resolution with caller forwarding.
    start = text.index('#[pyclass(unsendable, module = "meltygui.core.rendering._gui_native")]\npub(crate) struct Renderer')
    end = text.index('#[pymodule]', start)
    renderer = text[start:end].replace('Renderer', 'CacheRenderer')
    # The module-level helper after the class stays with the production module.
    renderer = renderer[:renderer.index('/// Whether two keyword dictionaries hold the same keys')]
    start_matching = renderer.index('        if wants(WANT_INPUT) { args.set(intern!(py, "input_value"), &input_value)?; }')
    end_matching = renderer.index('        let fused = capture.as_ref().is_some_and(|capture| capture.fused);',
                                  renderer.index('        for parameter in &self.parameters {', start_matching))
    forwarding = '''        let clean = PyDict::new(py);
        if let Some(keys) = &explicit_keys {
            for key in keys.downcast::<PyTuple>()?.iter() {
                if let Some(value) = args.get(&key)? {
                    clean.set_item(key, value)?;
                }
            }
        }
'''
    renderer = renderer[:start_matching] + forwarding + renderer[end_matching:]
    old_call = 'self.func.bind(py).call((), Some(&clean))'
    assert renderer.count(old_call) == 1
    renderer = renderer.replace(old_call, 'self.func.bind(py).call((&input_value,), Some(&clean))')
    (destination / 'src/cache_renderer.rs').write_text('use super::*;\n' + renderer)
    needle = '    m.add_class::<Renderer>()?;'
    assert text.count(needle) == 1
    text = text.replace('mod geometry;', 'mod bare;\nmod cached_bare;\nmod cache_renderer;\nmod geometry;', 1)
    path.write_text(text.replace(needle, needle + '\n    m.add_class::<bare::BareGui>()?;'
                                '\n    m.add_class::<cached_bare::CachedBareGui>()?;'
                                '\n    m.add_class::<cache_renderer::CacheRenderer>()?;'
                                '\n    m.add_function(wrap_pyfunction!(bare::fixed_body, m)?)?;'))
    cargo = shutil.which('cargo')
    assert cargo
    subprocess.run([cargo, 'build', '--release', '--locked', '--manifest-path',
                    str(destination / 'Cargo.toml')], check=True, close_fds=False)


if __name__ == '__main__':
    main()
