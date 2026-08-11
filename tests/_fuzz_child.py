
from pathlib import Path
import os, sys

try:
    import resource
except ImportError:
    resource = None

if resource is not None:
    setrlimit = getattr(resource, "setrlimit", None)
    rlimit_as = getattr(resource, "RLIMIT_AS", None)
    if setrlimit is not None and rlimit_as is not None:
        setrlimit(rlimit_as, (536870912, 536870912))

sys.path.insert(0, str(Path('/sessions/wizardly-awesome-ritchie/mnt/Blob/security').resolve()))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from security.safe_decompress import safe_parquet_open, DecompressionLimits
data = open(sys.argv[1], "rb").read()
try:
    pf = safe_parquet_open(data, DecompressionLimits(max_output_bytes=64*1024*1024))
    pf.read()
    print("READ_OK")
except MemoryError:
    print("MEMORY_ERROR")
except Exception as e:
    print("REJECTED:" + type(e).__name__)
