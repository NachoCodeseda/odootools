import os
import glob
import shutil
import importlib.util
from pathlib import Path
from typing import List, Optional, Tuple


def find_conf_file(odoo_path: Optional[str] = None) -> Optional[str]:
    """Return the first odoo.conf found, checking ODOO_CONF env var, then near *odoo_path*, then system locations."""
    candidates: List[Path] = []
    env_conf = os.getenv('ODOO_CONF')
    if env_conf:
        candidates.append(Path(env_conf))
    if odoo_path:
        p = Path(odoo_path)
        candidates += [p / 'odoo.conf', p.parent / 'odoo.conf']
    candidates += [
        Path('/etc/odoo/odoo.conf'),
        Path('/etc/odoo.conf'),
        Path.home() / '.odoorc',
        Path.home() / '.openerp_serverrc',
    ]
    return next((str(c) for c in candidates if c.is_file()), None)


def discover_all_installations() -> List[str]:
    """
    Return a deduplicated list of Odoo installation paths (directories containing odoo-bin).

    Search order:
    1. ``ODOO_PATH`` environment variable.
    2. Odoo already importable in the current Python environment (pip install / venv).
    3. Walk parent directories of this file (covers installs like /opt/odoo18/…).
    4. ``odoo-bin`` found via ``shutil.which`` (Odoo in system PATH).
    5. Glob search under ``/opt``, ``/usr/local``, and the home directory.
    """
    found: List[str] = []
    seen: set = set()

    def register(path: Optional[str]) -> None:
        if not path:
            return
        real = os.path.realpath(path)
        if real not in seen and os.path.exists(os.path.join(path, 'odoo-bin')):
            found.append(path)
            seen.add(real)

    # 1. ODOO_PATH env var
    register(os.getenv('ODOO_PATH'))

    # 2. Odoo already importable (pip-installed or active venv)
    try:
        spec = importlib.util.find_spec('odoo')
        if spec and spec.origin:
            pkg_dir = Path(spec.origin).parent
            for candidate in (pkg_dir.parent, pkg_dir.parent.parent):
                register(str(candidate))
    except (ValueError, ModuleNotFoundError):
        pass

    # 3. Walk up from this file's location (e.g. installed inside /opt/odoo18/custom_addons/…)
    for parent in list(Path(__file__).resolve().parents)[:6]:
        register(str(parent))
        nested = parent / 'odoo'
        if nested.is_dir():
            register(str(nested))

    # 4. odoo-bin in system PATH
    bin_path = shutil.which('odoo-bin') or shutil.which('odoo')
    if bin_path:
        register(str(Path(bin_path).resolve().parent))

    # 5. Glob search in common roots
    patterns = [
        '/opt/*/odoo-bin',
        '/opt/*/odoo/odoo-bin',
        '/usr/local/*/odoo-bin',
        str(Path.home() / '*/odoo-bin'),
        str(Path.home() / '*/odoo/odoo-bin'),
    ]
    for pattern in patterns:
        for match in sorted(glob.glob(pattern)):
            register(str(Path(match).parent))

    return found


def discover_odoo() -> Tuple[Optional[str], Optional[str]]:
    """
    Return ``(odoo_path, conf_path)`` for the best available Odoo installation.

    If ``ODOO_CONF`` or ``ODOO_PATH`` env vars are set they take priority.
    Otherwise the first result from :func:`discover_all_installations` is used.
    """
    env_conf = os.getenv('ODOO_CONF')
    env_path = os.getenv('ODOO_PATH')
    if env_conf and Path(env_conf).is_file():
        return env_path, env_conf
    if env_path and Path(env_path, 'odoo-bin').exists():
        return env_path, find_conf_file(env_path)

    installations = discover_all_installations()
    if installations:
        path = installations[0]
        return path, find_conf_file(path)

    return None, None
