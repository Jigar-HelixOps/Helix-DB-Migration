import logging
import os
import sys
from datetime import datetime
from pathlib import Path


def _resolve_log_dir(log_dir):
    candidates = [
        Path(log_dir),
        Path("./logs"),
        Path("/tmp/oracle_pg_migrate_logs") / os.getenv("USER", "migration"),
    ]

    last_error = None
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            test_file = candidate / ".write_test"
            with test_file.open("w", encoding="utf-8") as handle:
                handle.write("ok")
            test_file.unlink()
            if candidate != Path(log_dir):
                print(
                    "Warning: cannot write to '{}'; using '{}' for logs instead.".format(
                        log_dir, candidate
                    ),
                    file=sys.stderr,
                )
            return candidate
        except (OSError, IOError) as exc:
            last_error = exc
            continue

    raise PermissionError(
        "Cannot write logs to any directory. Tried: {}. Last error: {}".format(
            ", ".join(str(path) for path in candidates),
            last_error,
        )
    )


def setup_logging(log_dir, verbosity):
    log_path = _resolve_log_dir(log_dir)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_path / "migrate_{}.log".format(timestamp)
    summary_file = log_path / "migrate_{}.summary.txt".format(timestamp)

    file_level = logging.DEBUG if verbosity >= 2 else logging.INFO
    console_level = logging.WARNING
    if verbosity == 1:
        console_level = logging.INFO
    elif verbosity >= 2:
        console_level = logging.DEBUG

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers = []

    file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    logging.getLogger(__name__).info("Log file: %s", log_file)
    logging.getLogger(__name__).info("Summary file: %s", summary_file)

    return log_file, summary_file
