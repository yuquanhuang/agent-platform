"""Temporal run worker entrypoint."""

from typing import NoReturn

from apps.processes import ProcessName, unavailable_process


def main() -> NoReturn:
    unavailable_process(ProcessName.TEMPORAL_WORKER_RUN)


if __name__ == "__main__":
    main()
