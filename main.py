from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--ocr-worker":
        from drawing_renamer.app import configure_runtime_environment
        from drawing_renamer.ocr_worker import main as worker_main

        configure_runtime_environment()
        return worker_main(sys.argv[2:])

    from drawing_renamer.app import main as application_main

    update_success_marker: Path | None = None
    if "--update-success-marker" in sys.argv:
        marker_index = sys.argv.index("--update-success-marker")
        try:
            update_success_marker = Path(sys.argv[marker_index + 1])
        except IndexError:
            pass
        else:
            del sys.argv[marker_index : marker_index + 2]
    return application_main(update_success_marker=update_success_marker)


if __name__ == "__main__":
    raise SystemExit(main())
