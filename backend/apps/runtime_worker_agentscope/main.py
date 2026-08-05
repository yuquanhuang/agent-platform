"""AgentScope runtime worker entrypoint."""

from typing import NoReturn

from apps.processes import ProcessName, unavailable_process


def main() -> NoReturn:
    unavailable_process(ProcessName.RUNTIME_WORKER_AGENTSCOPE)


if __name__ == "__main__":
    main()
