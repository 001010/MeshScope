"""Wrap the built MSI in a setup .exe that carries the MeshScope icon.

An .msi file always shows the Windows Installer icon in Explorer and browsers, so the
installer people download is dist\\MeshScope-<version>-setup.exe: a small launcher (compiled
with the C# compiler that ships with Windows' .NET Framework 4) with the MSI embedded.
It unpacks the MSI to %TEMP%, runs it with msiexec (passing on any arguments, e.g. /quiet),
and deletes it afterwards. Upgrades behave exactly as with the MSI itself.

    .venv\\Scripts\\python tools\\make_setup_exe.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from meshscope import APP_NAME, __version__  # noqa: E402

CSC = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"

SOURCE = r'''
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("@APP@ @VER@ Setup")]
[assembly: AssemblyProduct("@APP@")]
[assembly: AssemblyDescription("Installs @APP@ @VER@, the Meshtastic USB node explorer")]
[assembly: AssemblyCompany("@APP@")]
[assembly: AssemblyCopyright("MeshScope contributors")]
[assembly: AssemblyVersion("@VER4@")]
[assembly: AssemblyFileVersion("@VER4@")]
[assembly: AssemblyInformationalVersion("@VER@")]

static class Setup {
    [STAThread]
    static int Main(string[] args) {
        string msi = Path.Combine(Path.GetTempPath(), "@APP@-@VER@-" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".msi");
        try {
            using (Stream src = Assembly.GetExecutingAssembly().GetManifestResourceStream("@MSI@"))
            using (FileStream dst = File.Create(msi)) {
                src.CopyTo(dst);
            }
            string extra = "";
            foreach (string a in args) extra += " " + (a.Contains(" ") ? "\"" + a + "\"" : a);
            ProcessStartInfo psi = new ProcessStartInfo("msiexec.exe", "/i \"" + msi + "\"" + extra);
            psi.UseShellExecute = false;
            using (Process p = Process.Start(psi)) {
                p.WaitForExit();
                return p.ExitCode;
            }
        } catch (Exception e) {
            MessageBox.Show("@APP@ setup could not start the installer:\n\n" + e.Message, "@APP@ Setup",
                            MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        } finally {
            try { if (File.Exists(msi)) File.Delete(msi); } catch { }
        }
    }
}
'''

MANIFEST = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity version="@VER4@" name="MeshScope.Setup" type="win32"/>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security><requestedPrivileges>
      <!-- msiexec asks for elevation itself when it installs -->
      <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
    </requestedPrivileges></security>
  </trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">
    <application><supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/></application>
  </compatibility>
</assembly>
'''


def main() -> int:
    msi = ROOT / "dist" / f"{APP_NAME}-{__version__}-win64.msi"
    out = ROOT / "dist" / f"{APP_NAME}-{__version__}-setup.exe"
    icon = ROOT / "assets" / "meshscope.ico"
    if not msi.exists():
        print(f"{msi} not found: build the MSI first")
        return 1
    if not CSC.exists():
        print(f"C# compiler not found at {CSC} (.NET Framework 4 is part of Windows 10/11)")
        return 1
    parts = (__version__.split("+")[0].split("-")[0].split(".") + ["0", "0", "0"])[:4]
    ver4 = ".".join(str(int(p)) if p.isdigit() else "0" for p in parts)
    fill = lambda s: (s.replace("@APP@", APP_NAME).replace("@VER@", __version__).replace("@VER4@", ver4)
                      .replace("@MSI@", "installer.msi"))
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "setup.cs").write_text(fill(SOURCE), encoding="utf-8")
        (td / "setup.manifest").write_text(fill(MANIFEST), encoding="utf-8")
        cmd = [str(CSC), "/nologo", "/target:winexe", "/optimize+", "/platform:anycpu",
               f"/out:{out}", f"/win32icon:{icon}", f"/win32manifest:{td / 'setup.manifest'}",
               f"/resource:{msi},installer.msi", "/reference:System.Windows.Forms.dll", str(td / "setup.cs")]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout, r.stderr)
            return r.returncode
    print(f"Built {out} ({out.stat().st_size / 2**20:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
