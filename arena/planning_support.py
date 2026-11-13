# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic fixtures and instructions for independent generated tests."""
from pathlib import Path

OBSERVER_FILENAME = "_mchc_observe.py"

CONSTRUCTOR_GUIDANCE = """
For constructor-observation scenarios the application supplies the read-only helper
/acceptance/_mchc_observe.py. Do not generate or replace this reserved file.
Use `from _mchc_observe import observe_constructors`. The context manager
records real Python constructor calls via sys.setprofile; it never patches SDK
classes, __init__, or validation. It observes the CURRENT THREAD, not subprocesses.
The returned mapping uses the actual classes as keys. Each entry is a list of
argument dicts (defaults included, **kwargs flattened, self omitted). Example:

    import boto3
    from strands.models import BedrockModel
    from _mchc_observe import observe_constructors
    with observe_constructors(boto3.Session, BedrockModel) as calls:
        runpy.run_path("/workspace/weather_agent.py", run_name="__main__")
    assert calls[BedrockModel], "The public entry point must construct its model"
    assert any(c.get("profile_name") == required_profile for c in calls[boto3.Session])
    assert any(c.get("model_id") == required_model for c in calls[BedrockModel])

Adapt filenames and assertions ONLY to the approved task. Set controlled sys.argv
and exercise the public entry point inside this context; importing alone may not
initialize the agent. To verify session identity, capture the session's service-client
boundary (a real method with self) and compare it with calls[BedrockModel][...]["boto_session"].
Use unittest.mock.patch.object(boto3.session.Session, "client", new=fixture_client)
if a service-client fixture is needed; leave boto3.Session and BedrockModel intact.
Service-client fixtures must implement the SDK's accessed contract, including
client.meta.region_name, and realistic responses for the exercised methods.
The real Session(profile_name=...) requires an existing profile even offline:
provide temporary AWS_CONFIG_FILE and AWS_SHARED_CREDENTIALS_FILE containing that
profile, a region and explicitly synthetic test credentials, and disable EC2 metadata.
Stub service-client requests/responses and HTTP transports before running the CLI,
including streaming model responses if the approved path invokes the agent. Never
pretend offline fixtures establish live provider access. Never patch BedrockModel.__init__,
even with wraps: a recording side_effect can suppress the wrapped constructor.
The observer does not suppress exceptions or certify success; assert behavior and
configuration after the real constructor executes.
"""


def with_constructor_observer(files):
    """Freeze the helper beside the test code; future reruns need only the plan."""
    source = Path(__file__).with_name("constructor_observation.py").read_text()
    if OBSERVER_FILENAME in files and files[OBSERVER_FILENAME] != source:
        raise ValueError(f"{OBSERVER_FILENAME} is reserved for the application's constructor observer")
    return {**files, OBSERVER_FILENAME: source}
