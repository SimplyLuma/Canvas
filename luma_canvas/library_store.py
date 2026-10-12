# SPDX-License-Identifier: MPL-2.0
"""A local index of known designs; organization never moves document files."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
from .persistence import DOCUMENT_SUFFIX, LEGACY_DOCUMENT_SUFFIXES, data_home, documents_folder


@dataclass(frozen=True)
class DesignFile:
    path: Path
    modified: float
    size: int


def saved_documents(recent=()):
    candidates = list(recent)
    for suffix in (DOCUMENT_SUFFIX, *LEGACY_DOCUMENT_SUFFIXES):
        candidates.extend(documents_folder().glob(f'*{suffix}'))
    found = {}
    for candidate in candidates:
        try:
            path = Path(candidate).resolve()
            if path.suffix.lower() not in (DOCUMENT_SUFFIX, *LEGACY_DOCUMENT_SUFFIXES) or not path.is_file():
                continue
            stat = path.stat()
            found[path] = DesignFile(path, stat.st_mtime, stat.st_size)
        except OSError:
            continue
    return sorted(found.values(), key=lambda design: (-design.modified, design.path.name.casefold()))


class LibraryOrganization:
    def __init__(self):
        self.path = data_home() / 'library.json'
        try:
            saved = json.loads(self.path.read_text())
        except (OSError, ValueError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        self.starred = set(saved.get('starred', []))
        self.projects = dict(saved.get('projects', {}))
        self.assignments = dict(saved.get('assignments', {}))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.json.tmp')
        try:
            with temporary.open('w') as output:
                json.dump({'starred': sorted(self.starred), 'projects': self.projects,
                           'assignments': self.assignments}, output, indent=2)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
