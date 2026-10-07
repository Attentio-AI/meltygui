import plistlib

import pytest


@pytest.fixture
def icon_project():
    """The generated target shape, without native SDK/runtime dependencies."""
    def create(build):
        path = build / 'MeltyIOS.xcodeproj/project.pbxproj'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps({'rootObject': 'project', 'objects': {
            'project': {'isa': 'PBXProject', 'mainGroup': 'group'},
            'group': {'isa': 'PBXGroup', 'children': []},
            'target': {'isa': 'PBXNativeTarget', 'name': 'Melty', 'buildPhases': [],
                       'buildConfigurationList': 'configurations'},
            'configurations': {'isa': 'XCConfigurationList', 'buildConfigurations': ['debug', 'release']},
            'debug': {'isa': 'XCBuildConfiguration', 'buildSettings': {'DEVELOPMENT_TEAM': 'KEEP'}},
            'release': {'isa': 'XCBuildConfiguration', 'buildSettings': {'DEVELOPMENT_TEAM': 'KEEP'}},
        }}))
        return path
    return create
