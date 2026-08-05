"""Codex runtime worker entrypoint."""

from typing import NoReturn

from apps.processes import ProcessName, unavailable_process


def main() -> NoReturn:
    unavailable_process(ProcessName.RUNTIME_WORKER_CODEX)


if __name__ == "__main__":
    main()
