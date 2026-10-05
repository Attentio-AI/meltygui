"""UIKit/Metal host and build recipes for ordinary MeltyGUI applications."""
from pathlib import Path

import os


def build_directory():
    """Build output belongs to the caller, never the installed toolkit."""
    return Path(os.environ.get('MELTY_IOS_BUILD_DIR', Path.cwd() / 'build/ios')).resolve()
