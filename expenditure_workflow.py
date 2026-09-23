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


FIELDS = ("description", "link", "vendor_id", "project_id", "use_note", "cost", "quantity", "location")
GROUPS = ("authorized", "unauthorized")


def possible(source, target):
    return source != target and (target != "ordered" or source == "in cart")


def default_rules():
    return {
        **{group: {source: {target: possible(source, target) and
             (group == "authorized" or target == "not ready")
             for target in STATUSES if target != source} for source in STATUSES}
           for group in GROUPS},
        "locks": {status: {field: status in ("ordered", "received") and
                   field in ("description", "vendor_id") for field in FIELDS}
                  for status in STATUSES},
    }


def validate(document):
    if not isinstance(document, dict) or type(document.get("version")) is not int:
        raise WorkflowError("Expected a versioned workflow mapping.")
    if document["version"] == 1:
        if set(document) != {"version", "transitions"}:
            raise WorkflowError("Invalid legacy workflow.")
        legacy = document["transitions"]
        validate_matrix(legacy, False)
        rules = default_rules()
        for source, target in PAIRS:
            rules["unauthorized"][source][target] = possible(source, target) and not legacy[source][target]
        return rules
    if document["version"] != 2 or set(document) != {"version", *GROUPS, "locks"}:
        raise WorkflowError("Expected workflow version 2 with permissions and locks.")
    for group in GROUPS:
        validate_matrix(document[group], True)
    locks = document["locks"]
    if not isinstance(locks, dict) or set(locks) != set(STATUSES):
        raise WorkflowError("Locks must contain all statuses.")
    for row in locks.values():
        if not isinstance(row, dict) or set(row) != set(FIELDS) or any(type(v) is not bool for v in row.values()):
            raise WorkflowError("Locks must contain boolean values for every field.")
    return {key: document[key] for key in (*GROUPS, "locks")}


def validate_matrix(matrix, enforce_cart):
    if not isinstance(matrix, dict) or set(matrix) != set(STATUSES):
        raise WorkflowError("Workflow must contain all five source statuses.")
    for source in STATUSES:
        row = matrix[source]
        if not isinstance(row, dict) or set(row) != set(STATUSES) - {source} or any(type(v) is not bool for v in row.values()):
            raise WorkflowError("Each source must map all other statuses to booleans.")
        if enforce_cart and any(value and not possible(source, target) for target, value in row.items()):
            raise WorkflowError("Ordered items must pass through the cart.")


@contextmanager
def locked(path):
    with open(str(path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write(path, rules):
    document = {"version": 2, **rules}
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
            document = yaml.safe_load(source)
        rules = validate(document)
        if document["version"] == 1:
            with locked(path):
                with open(path, encoding="utf-8") as source:
                    original = source.read()
                document = yaml.safe_load(original)
                rules = validate(document)
                if document["version"] == 1:
                    backup = Path(str(path) + ".v1.bak")
                    if not backup.exists():
                        with backup.open("x", encoding="utf-8") as output:
                            output.write(original)
                    _write(path, rules)
        return rules
    except (OSError, yaml.YAMLError, UnicodeError) as exc:
        raise WorkflowError("Cannot read expenditure workflow configuration.") from exc


def save(path, rules):
    """Serialize writers and return the prior rules for auditing."""
    validate({"version": 2, **rules})
    read(path)  # Migrate before taking the writer lock.
    with locked(path):
        previous = read(path)
        _write(path, rules)
    return previous
