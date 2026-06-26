"""Shared pytest setup.

Pin ``MCPE_DATA_DIR`` to a throwaway directory BEFORE any ``app.*`` import caches
``get_settings()`` / ``get_engine()``, so a bare ``pytest`` run never reads or
writes a real data directory. An explicit ``MCPE_DATA_DIR`` still wins (setdefault).
"""

import os
import tempfile

os.environ.setdefault("MCPE_DATA_DIR", tempfile.mkdtemp(prefix="mcpe-tests-"))
