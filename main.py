from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--ocr-worker":
        from drawing_renamer.app import configure_runtime_environment
        from drawing_renamer.ocr_worker import main as worker_main

        configure_runtime_environment()
        return worker_main(sys.argv[2:])

    from drawing_renamer.app import main as application_main

    return application_main()


if __name__ == "__main__":
    raise SystemExit(main())
