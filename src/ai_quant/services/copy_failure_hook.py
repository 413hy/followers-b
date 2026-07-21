"""Wake the incident reporter on the first abnormal exit of a restartable service."""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404 -- exact systemctl path and allowlisted unit names

_ALLOWED_SOURCE_UNITS = frozenset(
    {
        "aiq-copy-poller.service",
        "aiq-copy-telegram.service",
        "aiq-testnet-user-stream.service",
    }
)
_SUCCESS_RESULTS = frozenset({"success", "", "none"})


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wake copy incident handling after service exit")
    parser.add_argument("--source-unit", choices=sorted(_ALLOWED_SOURCE_UNITS), required=True)
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    service_result = os.environ.get("SERVICE_RESULT", "").strip().casefold()
    if service_result in _SUCCESS_RESULTS:
        return 0
    reporter_unit = f"aiq-copy-incident-reporter@{arguments.source_unit}.service"
    result = subprocess.run(  # noqa: S603  # nosec B603
        ["/usr/bin/systemctl", "start", "--no-block", reporter_unit],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    print(
        json.dumps(
            {
                "event": "copy_failure_hook",
                "source_unit": arguments.source_unit,
                "service_result": service_result,
                "reporter_started": result.returncode == 0,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    return 0 if result.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
