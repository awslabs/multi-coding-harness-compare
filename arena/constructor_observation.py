# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Read-only observation of real Python constructors in the current thread.

This stdlib-only file is frozen into generated acceptance files as
_mchc_observe.py so tests also work after S3 restore and inside Docker.
"""
from contextlib import contextmanager
import inspect
import sys


@contextmanager
def observe_constructors(*classes):
    """Yield {class: [arguments]} without replacing classes or constructors.

    Argument dictionaries include defaults and flattened **kwargs, excluding
    self. Values retain identity (they are not deep copies). Records show calls,
    not successful initialization: real exceptions propagate normally.
    Only constructors with Python code are supported. Observe and execute the
    public entry point in this thread; subprocesses/other threads are not traced.
    """
    calls = {cls: [] for cls in classes}
    targets = {}
    for cls in calls:
        init = cls.__init__
        code = getattr(init, "__code__", None)
        if code is None:
            raise TypeError(f"{cls.__name__}.__init__ must be a Python function")
        parameters = list(inspect.signature(init, follow_wrapped=False).parameters.values())
        targets.setdefault(code, []).append((cls, parameters))
    previous = sys.getprofile()

    def record(frame, event, arg):
        if event == "call":
            for cls, parameters in targets.get(frame.f_code, ()):
                if not parameters or not isinstance(frame.f_locals.get(parameters[0].name), cls):
                    continue
                values = {}
                for parameter in parameters[1:]:
                    value = frame.f_locals[parameter.name]
                    if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                        values.update(value)
                    else:
                        values[parameter.name] = value
                calls[cls].append(values)
        if previous is not None:
            previous(frame, event, arg)

    sys.setprofile(record)
    try:
        yield calls
    finally:
        sys.setprofile(previous)
