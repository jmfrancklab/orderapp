"""Validated, atomically saved expenditure rules shared by all web workers."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import tempfile

import yaml


STATUSES = ("not ready", "awaiting order", "in cart", "ordered", "received")
PAIRS = tuple((source, target) for source in STATUSES for target in STATUSES
              if source != target)


class WorkflowError(ValueError):
    pass


def default_rules():
    return {source: {target: target != "not ready" for target in STATUSES
                     if target != source} for source in STATUSES}


def validate(document):
    if (not isinstance(document, dict) or set(document) != {"version", "transitions"}
            or type(document["version"]) is not int or document["version"] != 1):
        raise WorkflowError("Expected workflow version 1 and a transitions mapping.")
    rules = document["transitions"]
    if not isinstance(rules, dict) or set(rules) != set(STATUSES):
        raise WorkflowError("Workflow must contain all five source statuses.")
    for source in STATUSES:
        targets = rules[source]
        if (not isinstance(targets, dict)
                or set(targets) != set(STATUSES) - {source}
                or any(type(value) is not bool for value in targets.values())):
            raise WorkflowError("Each source must map all four other statuses to true or false.")
    return rules


@contextmanager
def locked(path):
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write(path, rules):
    document = {"version": 1, "transitions": rules}
    validate(document)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=Path(path).parent, delete=False) as output:
            temporary = output.name
            yaml.safe_dump(document, output, sort_keys=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def initialize(path):
    """Only seed absent files; never replace an existing configuration."""
    with locked(path):
        if not os.path.lexists(path):
            _write(path, default_rules())


def read(path):
    try:
        with open(path, encoding="utf-8") as source:
            return validate(yaml.safe_load(source))
    except (OSError, yaml.YAMLError, UnicodeError) as exc:
        raise WorkflowError("Cannot read expenditure workflow configuration.") from exc


def save(path, rules):
    """Serialize writers and return the prior rules for auditing."""
    validate({"version": 1, "transitions": rules})
    with locked(path):
        previous = read(path)
        _write(path, rules)
    return previous
