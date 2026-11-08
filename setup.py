"""Build MeshScope into a Windows .msi with cx_Freeze.

    python setup.py bdist_msi        (or just run build.ps1)
"""

import sys

from cx_Freeze import Executable, setup

from meshscope import APP_NAME, __version__

# Never change this: it lets newer MSIs upgrade older installs in place.
UPGRADE_CODE = "{045C21F7-8BB4-4D15-8CFF-A82DB9F505D6}"

build_exe_options = {
    "packages": ["meshscope", "meshtastic", "serial", "pubsub", "google.protobuf", "tabulate", "yaml"],
    "excludes": [
        "tkinter", "unittest", "pydoc_data", "test",
        # BLE / optional analysis extras of the meshtastic package - not used over USB
        "bleak", "winrt", "dash", "dash_bootstrap_components", "pandas", "pyarrow", "numpy",
        "matplotlib", "riden", "ppk2_api", "parse", "platformdirs", "print_color", "argcomplete",
    ],
    "include_files": [("meshscope/static", "static"), ("assets/meshscope.ico", "meshscope.ico")],
    "include_msvcr": True,
    "optimize": 1,
}

# Desktop shortcut (the Start-menu shortcut comes from the Executable below).
msi_data = {
    "Shortcut": [
        ("DesktopShortcut", "DesktopFolder", APP_NAME, "TARGETDIR",
         "[TARGETDIR]MeshScope.exe", None, "Meshtastic USB node explorer", None,
         None, None, None, "TARGETDIR"),
    ],
}

bdist_msi_options = {
    "upgrade_code": UPGRADE_CODE,
    "add_to_path": False,
    "all_users": True,
    "initial_target_dir": rf"[ProgramFiles64Folder]{APP_NAME}",
    "install_icon": "assets/meshscope.ico",
    "output_name": f"{APP_NAME}-{__version__}-win64.msi",
    "data": msi_data,
    "summary_data": {
        "author": "MeshScope",
        "comments": "Meshtastic USB node explorer: node list, traceroute, ping, maps, topology and reach analysis",
    },
}

setup(
    name=APP_NAME,
    version=__version__,
    description="MeshScope - Meshtastic USB node explorer",
    author="MeshScope",
    options={"build_exe": build_exe_options, "bdist_msi": bdist_msi_options},
    executables=[
        Executable(
            "run_meshscope.py",
            base="gui" if sys.platform == "win32" else None,
            target_name="MeshScope.exe",
            icon="assets/meshscope.ico",
            shortcut_name=APP_NAME,
            shortcut_dir="ProgramMenuFolder",
            copyright="MeshScope contributors",
        )
    ],
)
