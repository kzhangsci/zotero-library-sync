# Library Sync standalone export; LicensePending. See release/BUILD.md.
from __future__ import annotations
from dataclasses import dataclass
import datetime as dt
import json
import os
from pathlib import Path
from typing import Mapping
REPO = Path(__file__).resolve().parents[1]
LEGACY_DEFAULTS = {}

@dataclass(frozen=True)
class LibraryPaths:
    repo: Path
    data: Path
    library: Path
    reports: Path
    cache: Path
    secrets: Path
    output: Path
    mineru_out: Path
    glossary: Path
    zotero_data: Path
    baseline_date: str | None
    config_file: Path | None
    portable: bool
    git_path: Path | None = None

def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate Library Sync configuration field')
        result[key] = value
    return result

def _path(value, base: Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip() or '\x00' in str(value):
        raise ValueError('Invalid Library Sync path')
    p = Path(value).expanduser()
    return (p if p.is_absolute() else base / p).resolve()

def get_paths(*, repo: Path | None=None, environ: Mapping[str, str] | None=None) -> LibraryPaths:
    env = os.environ if environ is None else environ
    root = Path(repo or REPO).resolve()
    home = Path.home()
    portable = (root / 'RELEASE.json').is_file() or not LEGACY_DEFAULTS
    configured_home = env.get('LIBRARY_SYNC_HOME', '').strip()
    default_data = _path(configured_home, root) if configured_home else Path(env.get('LOCALAPPDATA') or home / '.local' / 'share') / 'LibrarySync' / 'data' if portable else root
    explicit = env.get('LIBRARY_SYNC_CONFIG', '').strip()
    config = _path(explicit, root) if explicit else root / 'library-sync.json'
    if not explicit and (not config.is_file()):
        config = default_data / 'config.json'
    values = {}
    if explicit or config.is_file():
        if not config.is_file() or config.stat().st_size > 16384:
            raise ValueError('Library Sync configuration is missing or too large')
        try:
            values = json.loads(config.read_text(encoding='utf-8-sig'), object_pairs_hook=_unique)
        except (OSError, ValueError) as exc:
            raise ValueError('Library Sync configuration cannot be read') from exc
        allowed = {'schema_version', 'data_dir', 'library_dir', 'baseline_date', 'zotero_data_dir', 'mineru_out_dir', 'git_path'}
        if not isinstance(values, dict) or set(values) - allowed or values.get('schema_version') != 1:
            raise ValueError('Unsupported Library Sync configuration schema')
    else:
        config = None
    base = config.parent if config else root
    data = _path(configured_home or values.get('data_dir', default_data), base)
    library = _path(env.get('LIBRARY_SYNC_LIBRARY') or values.get('library_dir') or (LEGACY_DEFAULTS.get('library') if not portable else None) or home / 'Documents' / 'Library', base)
    baseline = values.get('baseline_date')
    if baseline is not None:
        if not isinstance(baseline, str) or len(baseline) != 10:
            raise ValueError('baseline_date must be an explicit YYYY-MM-DD date')
        try:
            if dt.date.fromisoformat(baseline).isoformat() != baseline:
                raise ValueError
        except ValueError as exc:
            raise ValueError('baseline_date must be an explicit YYYY-MM-DD date') from exc
    reports = data / 'reports'
    mineru = values.get('mineru_out_dir') or (LEGACY_DEFAULTS.get('mineru_out') if not portable and config is None and (not configured_home) else None) or data / 'mineru_out'
    return LibraryPaths(root, data, library, reports, reports / '_cache', data / '.secrets', data / 'output', _path(mineru, base), library / 'resources' / 'glossary.md', _path(values.get('zotero_data_dir', home / 'Zotero'), base), baseline, config, portable, _path(env.get('LIBRARY_SYNC_GIT') or values['git_path'], base) if env.get('LIBRARY_SYNC_GIT') or values.get('git_path') else None)
PATHS = get_paths()
DATA = PATHS.data
LIBRARY = PATHS.library
REPORTS = PATHS.reports
CACHE = PATHS.cache
SECRETS = PATHS.secrets
OUTPUT = PATHS.output
MINERU_OUT = PATHS.mineru_out
GLOSSARY = PATHS.glossary
ZOTERO_DATA = PATHS.zotero_data
BASELINE_DATE = PATHS.baseline_date
GIT = PATHS.git_path
