"""Testing.py."""
import os
from os import environ

TESTING = True
SPIFFWORKFLOW_BACKEND_LOG_TO_FILE = environ.get("SPIFFWORKFLOW_BACKEND_LOG_TO_FILE", default="true") == "true"

SPIFFWORKFLOW_BACKEND_PERMISSIONS_FILE_NAME = environ.get(
    "SPIFFWORKFLOW_BACKEND_PERMISSIONS_FILE_NAME", default="unit_testing.yml"
)

SPIFFWORKFLOW_BACKEND_LOG_LEVEL = environ.get("SPIFFWORKFLOW_BACKEND_LOG_LEVEL", default="debug")
SPIFFWORKFLOW_BACKEND_GIT_COMMIT_ON_SAVE = False

# NOTE: set this here since nox shoves tests and src code to
# different places and this allows us to know exactly where we are at the start
worker_id = environ.get("PYTEST_XDIST_WORKER")
parallel_test_suffix = ""
if worker_id is not None:
    parallel_test_suffix = f"_{worker_id}"
SPIFFWORKFLOW_BACKEND_BPMN_SPEC_ABSOLUTE_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "tests",
    "spiffworkflow_backend",
    "files",
    f"bpmn_specs{parallel_test_suffix}",
)
