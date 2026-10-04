#!/bin/bash
# What the Lambda runs in place of a Python handler. The Lambda Web Adapter layer's wrapper
# (/opt/bootstrap, named by AWS_LAMBDA_EXEC_WRAPPER) execs "$LAMBDA_TASK_ROOT/$_HANDLER", and the
# function's handler is this file's name. It starts the MCP SDK's own web app under uvicorn on
# the port the adapter forwards requests to, and the adapter turns each API Gateway event into a
# plain HTTP request for it.
#
# The shape is the adapter's own zip example for a Python web app:
# https://github.com/awslabs/aws-lambda-web-adapter/blob/main/examples/fastapi-zip/app/run.sh
#
# - `python -m uvicorn`, not `uvicorn`: `pip install -t` puts the package in the task root but
#   its console script under bin/, with a shebang pointing at the build machine's Python.
# - PYTHONPATH names the task root (ops_mcp/, common/ and everything pip installed) first, then
#   the runtime's own directory, which is where the python3.11 runtime keeps boto3. A managed
#   runtime adds both for a normal handler; nothing does for a process started here.
# - 127.0.0.1: only the adapter, in the same sandbox, ever connects.
# - --factory: ops_mcp.server:create_app is a function that builds the app (it reads
#   OPS_MCP_ALLOWED_HOSTS at that moment), not an app object.
# - exec: uvicorn replaces this shell, so it receives the sandbox's shutdown signal itself.
set -eu

cd "$LAMBDA_TASK_ROOT"
export PYTHONPATH="$LAMBDA_TASK_ROOT:${LAMBDA_RUNTIME_DIR:-/var/runtime}${PYTHONPATH:+:$PYTHONPATH}"

exec python -m uvicorn --factory ops_mcp.server:create_app --host 127.0.0.1 --port "${AWS_LWA_PORT:-8080}"
