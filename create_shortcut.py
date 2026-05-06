"""
PitchBot first-run setup.
Run this ONCE from a terminal/PowerShell inside the PitchBot folder:

    python create_shortcut.py

It will:
  1. Create a Python virtual environment (venv)
  2. Install all dependencies
  3. Initialise the SQLite database
  4. Generate assets/icon.ico
  5. Create a PitchBot shortcut on your Desktop
     (Windows: .lnk  |  Linux/Mac: .desktop)

After setup you still need to fetch MLB data once:
    venv\\Scripts\\python update.py --full-season   (Windows)
    venv/bin/python  update.py --full-season        (Mac/Linux)

Then just double-click the Desktop icon any time to launch PitchBot.
"""

import ctypes
import os
import platform
import subprocess
import sys


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IS_WIN = platform.system() == "Windows"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _venv_paths():
    scripts = "Scripts" if IS_WIN else "bin"
    venv = os.path.join(SCRIPT_DIR, "venv")
    py   = os.path.join(venv, scripts, "python" + (".exe" if IS_WIN else ""))
    pyw  = os.path.join(venv, scripts, "pythonw.exe") if IS_WIN else py
    pip  = os.path.join(venv, scripts, "pip" + (".exe" if IS_WIN else ""))
    return venv, py, pyw, pip


def _run(cmd, **kwargs):
    """Run a command, printing it first."""
    print("  >", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True, **kwargs)


# ---------------------------------------------------------------------------
# Step 1 — virtual environment
# ---------------------------------------------------------------------------

def setup_venv():
    venv, py, pyw, pip = _venv_paths()
    if os.path.exists(py):
        print("[1/4] Virtual environment already exists — skipping.")
    else:
        print("[1/4] Creating virtual environment…")
        _run([sys.executable, "-m", "venv", venv])

    print("[2/4] Installing dependencies (first time may take a few minutes)…")
    _run([pip, "install", "--upgrade", "pip", "-q"])
    _run([pip, "install", "-r", os.path.join(SCRIPT_DIR, "requirements.txt"), "-q"])
    print("      Dependencies installed.\n")

    print("[3/4] Initialising database…")
    _run(
        [py, "-c",
         "import sys; sys.path.insert(0,'.'); from database import init_db; init_db(); print('      DB ready.')"],
        cwd=SCRIPT_DIR,
    )
    print()


# ---------------------------------------------------------------------------
# Step 2 — icon.ico
# ---------------------------------------------------------------------------

def make_ico():
    _, py, *_ = _venv_paths()
    ico_path = os.path.join(SCRIPT_DIR, "assets", "icon.ico")

    code = r"""
import os
from PIL import Image, ImageDraw

ico = r"{ico}"
images = []
for size in (16, 32, 48, 64, 128, 256):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = max(1, size // 16)
    # Dark navy background circle
    d.ellipse([m, m, size - m, size - m], fill=(26, 26, 46), outline=(15, 52, 96), width=m)
    # Green strike zone rectangle
    zl, zr = int(size * 0.28), int(size * 0.72)
    zt, zb = int(size * 0.23), int(size * 0.77)
    bw = max(2, size // 28)
    d.rectangle([zl, zt, zr, zb], outline=(46, 204, 113), width=bw)
    # Zone grid lines
    step_h = (zb - zt) // 3
    step_v = (zr - zl) // 3
    for i in (1, 2):
        d.line([(zl, zt + i * step_h), (zr, zt + i * step_h)], fill=(46, 204, 113, 100), width=max(1, bw - 1))
        d.line([(zl + i * step_v, zt), (zl + i * step_v, zb)], fill=(46, 204, 113, 100), width=max(1, bw - 1))
    # Red stitching dots
    for xf, yf in ((0.35, 0.38), (0.65, 0.38), (0.35, 0.62), (0.65, 0.62)):
        cx, cy = int(size * xf), int(size * yf)
        r = max(1, size // 22)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(231, 76, 60))
    images.append(img)

images[0].save(ico, format="ICO", sizes=[(s, s) for s in (16, 32, 48, 64, 128, 256)],
               append_images=images[1:])
print("      icon.ico created:", ico)
""".format(ico=ico_path.replace("\\", "\\\\"))

    try:
        _run([py, "-c", code], cwd=SCRIPT_DIR)
    except Exception as e:
        print(f"      Warning: could not create icon.ico — {e}")
        print("      The shortcut will use the default Python icon instead.")


# ---------------------------------------------------------------------------
# Step 3 — Desktop shortcut
# ---------------------------------------------------------------------------

def _windows_desktop():
    """Return the user's Desktop path (handles OneDrive redirection)."""
    CSIDL_DESKTOPDIRECTORY = 0x0010
    buf = ctypes.create_unicode_buffer(512)
    try:
        ctypes.windll.shell32.SHGetFolderPathW(0, CSIDL_DESKTOPDIRECTORY, 0, 0, buf)
        path = buf.value
    except Exception:
        path = ""
    return path or os.path.join(os.path.expanduser("~"), "Desktop")


def create_windows_shortcut():
    import tempfile
    _, _, pyw, _ = _venv_paths()
    launcher = os.path.join(SCRIPT_DIR, "launcher.pyw")
    ico      = os.path.join(SCRIPT_DIR, "assets", "icon.ico")
    desktop  = _windows_desktop()
    shortcut = os.path.join(desktop, "PitchBot.lnk")

    # Write a temp .ps1 file — avoids all command-line quoting complexity.
    # In PowerShell single-quoted strings backslashes are literal, so
    # Windows paths work as-is.
    ps_lines = [
        "$ws = New-Object -ComObject WScript.Shell",
        f"$s = $ws.CreateShortcut('{shortcut}')",
        f"$s.TargetPath = '{pyw}'",
        # Quote the argument so spaces in the path are handled correctly.
        f'$s.Arguments = \'"{launcher}"\'',
        f"$s.WorkingDirectory = '{SCRIPT_DIR}'",
        f"$s.IconLocation = '{ico}'",
        "$s.Description = 'PitchBot - MLB Ball/Strike Accuracy Tracker'",
        "$s.WindowStyle = 1",
        "$s.Save()",
    ]

    fd, ps1 = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(ps_lines))
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1],
            capture_output=True, text=True,
        )
    finally:
        try:
            os.unlink(ps1)
        except OSError:
            pass

    if result.returncode == 0:
        print(f"      Desktop shortcut: {shortcut}")
    else:
        print("      Could not create shortcut automatically.")
        print("      stderr:", result.stderr.strip())
        print()
        print("  Manual setup — right-click Desktop → New → Shortcut")
        print(f"  Target:    {pyw}")
        print(f'  Arguments: "{launcher}"')
        print(f"  Icon:      {ico}")


def create_linux_shortcut():
    apps_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "applications")
    os.makedirs(apps_dir, exist_ok=True)
    src = os.path.join(SCRIPT_DIR, "PitchBot.desktop")
    dst = os.path.join(apps_dir, "PitchBot.desktop")
    with open(src) as f:
        content = f.read().replace("/home/user/PitchBot", SCRIPT_DIR)
    with open(dst, "w") as f:
        f.write(content)
    # Make it trusted / executable
    os.chmod(dst, 0o755)
    print(f"      .desktop launcher installed: {dst}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    os.chdir(SCRIPT_DIR)

    print("=" * 54)
    print("  PitchBot Setup")
    print("=" * 54)
    print()

    setup_venv()

    print("[4/4] Creating icon & Desktop shortcut…")
    make_ico()
    if IS_WIN:
        create_windows_shortcut()
    else:
        create_linux_shortcut()

    _, py, _, _ = _venv_paths()
    venv_rel = os.path.relpath(py, SCRIPT_DIR)

    print()
    print("=" * 54)
    print("  Setup complete!")
    print("=" * 54)
    print()
    print("  Fetch 2025 season data (first time, ~15 min):")
    print()
    if IS_WIN:
        print(f"    {os.path.join('venv', 'Scripts', 'python.exe')} update.py --full-season")
    else:
        print("    venv/bin/python update.py --full-season")
    print()
    print("  Then double-click the PitchBot icon on your Desktop!")
    print()


if __name__ == "__main__":
    main()
