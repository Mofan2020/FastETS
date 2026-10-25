"""Download the libspeex source archive (fallback for the build script).

Some machines have a curl/wget without a usable certificate chain; Python
usually ships its own CA bundle, so this is used as a last resort.
"""

from __future__ import annotations

import shutil
import ssl
import sys
import urllib.request


def main() -> int:
    if len(sys.argv) < 3:
        return 2
    url, target = sys.argv[1], sys.argv[2]
    try:
        context = ssl.create_default_context()
        with urllib.request.urlopen(url, timeout=120, context=context) as response:
            with open(target, "wb") as handle:
                shutil.copyfileobj(response, handle)
    except Exception as exc:  # noqa: BLE001
        print(f"[speex] download failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
