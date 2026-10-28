"""Opt in to the retired Python oracle, including isolated subprocess tests."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / 'tests/legacy_reference'
sys.path[:0] = [str(REFERENCE), str(ROOT)]
# Child interpreters used by optimization/locking tests need the same oracle.
_paths = [str(REFERENCE), str(ROOT)]
if os.environ.get('PYTHONPATH'):
    _paths.append(os.environ['PYTHONPATH'])
os.environ['PYTHONPATH'] = os.pathsep.join(_paths)
