"""
Runtime hook for PyInstaller — executed BEFORE main.py.

Sets environment variables that numba, numpy, and OpenBLAS read at
import time.  Once a library has been imported, changing these vars
has no effect, so this hook must run first.
"""
import os
import sys

# ── 1. Threading: ensure numpy/OpenBLAS/MKL use all cores ────────────
_cpu = str(os.cpu_count() or 4)
for var in ('MKL_NUM_THREADS', 'OMP_NUM_THREADS',
            'OPENBLAS_NUM_THREADS', 'NUMBA_NUM_THREADS'):
    os.environ.setdefault(var, _cpu)

# ── 2. Numba: writable cache + frozen-app fixes ─────────────────────
if getattr(sys, 'frozen', False):
    _cache = os.path.join(os.path.expanduser('~'), '.naksha', 'numba_cache')
    os.makedirs(_cache, exist_ok=True)
    os.environ.setdefault('NUMBA_CACHE_DIR', _cache)

    # Tell numba not to look for .py source files (they don't exist
    # inside a frozen exe — only .pyc bytecode is bundled).
    os.environ.setdefault('NUMBA_DISABLE_JIT', '0')