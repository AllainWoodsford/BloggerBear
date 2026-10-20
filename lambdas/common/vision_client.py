"""The pipeline's side of the vision worker: invoke it, wherever it runs, and check what comes back.

docs/enhancements/archive/opencv-agentic-vision-enhancement.md §2-§3. The worker may run in another region
(next to the imagery), so the client is made for the region in the worker's ARN, not for the
Lambda's own. Configuration, from the environment (set by Terraform when `vision_enabled`):

    VISION_WORKER_ARN       the "opencv" backend's function ARN
    VISION_COOL_WORKER_ARN  the "cool" backend's, once it exists (docs §4); optional

A reply is checked against common/vision_contract.py before anything uses it. Every failure, from
the invoke, the worker or a malformed reply, raises `VisionError`; the adapter treats that site as
not measured this time, never as "nothing there".

`measure` names the task (`ships` by default; `rail_access` sends the OpenStreetMap network with
the request) and may shorten the read timeout: the triage agent's `look_again` runs inside the
research tick's own deadline, so it waits less for a re-measurement than the adapter does for the
first one. One boto3 client is kept per (region, read timeout).
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field

import boto3
from botocore.config import Config

from common import vision_contract as contract

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# A reply's figure is a map at most 1024 px on its longer side, plus, on the rail figures, the
# strip of caption and legend under it (vision/rail_annotate.py, which keeps the rail map within a
# 2 MiB budget and the second figure, the transit-reach map under `figures_png_b64["modes"]`,
# within 512 KiB); 3 MB is far above any such PNG, each figure is held to it on its own, and the
# worker's own reply-size guard keeps the two together under Lambda's 6 MB synchronous response
# limit.
MAX_IMAGE_BYTES = 3 * 1024 * 1024

_ENV_BY_BACKEND = {"opencv": "VISION_WORKER_ARN", "cool": "VISION_COOL_WORKER_ARN"}

# A worker call reads a few tiles and runs for seconds. No automatic retries: the next tick tries
# again, and a retry here would double a slow call's cost inside the research tick's own timeout.
# One attempt in all: botocore reads `max_attempts` as the number of retries, so it is
# `total_max_attempts` that says one. A caller with a deadline (common/vision_triage.py) passes a
# shorter read timeout, and counts the connect allowance on top of it: a call may take both before
# it gives up.
DEFAULT_READ_TIMEOUT_SECONDS = 90
CONNECT_TIMEOUT_SECONDS = 5
_CLIENT_CONFIG = Config(
    connect_timeout=CONNECT_TIMEOUT_SECONDS,
    read_timeout=DEFAULT_READ_TIMEOUT_SECONDS,
    retries={"total_max_attempts": 1, "mode": "standard"},
)
_clients: dict[tuple[str, float | None], object] = {}


class VisionError(RuntimeError):
    """The site could not be measured. `code` is the worker's error code, or "invoke" / "reply".
    `request_id` is the worker invocation's Lambda request id when the invoke answered (a
    refusal, a malformed or mismatched reply, a function error), so the failure still joins the
    worker's own log line; None before an answer (a bad request, a read timeout)."""

    def __init__(self, code: str, detail: str = "", request_id: str | None = None):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail, self.request_id = code, detail, request_id


@dataclass
class VisionResult:
    reply: dict
    image_png: bytes | None
    # The worker invocation's Lambda request id (ResponseMetadata.RequestId), the key that joins
    # the measurement to the worker's log line and its REPORT line; None from a fake or a local run.
    request_id: str | None = None
    # The reply's further figures by name (contract.FIGURE_NAMES), decoded as the map is: "modes",
    # the rail task's transit-reach map, when the network had a stops layer and the worker's
    # reply-size guard kept it. Empty from a ships reply, an older worker or a fake, so an adapter
    # reads `figures.get(name)` and loses nothing else when one is missing.
    figures: dict[str, bytes] = field(default_factory=dict)

    @property
    def metrics(self) -> dict:
        return self.reply["metrics"]


def worker_arn(backend: str = "opencv") -> str | None:
    if backend not in _ENV_BY_BACKEND:
        raise ValueError(f"unknown backend {backend!r}")
    return os.environ.get(_ENV_BY_BACKEND[backend]) or None


def is_configured(backend: str = "opencv") -> bool:
    return worker_arn(backend) is not None


def region_of(arn: str) -> str:
    """The region in a Lambda function ARN (arn:aws:lambda:<region>:<account>:function:<name>)."""
    parts = arn.split(":")
    if len(parts) < 7 or parts[2] != "lambda" or not parts[3]:
        raise ValueError(f"not a Lambda function ARN: {arn!r}")
    return parts[3]


def _client(region: str, read_timeout: float | None = None):
    """The Lambda client for `region`, with `_CLIENT_CONFIG`'s read timeout or `read_timeout`
    seconds instead; one per (region, read timeout), made on first use."""
    key = (region, read_timeout)
    if key not in _clients:
        config = _CLIENT_CONFIG
        if read_timeout is not None:
            config = config.merge(Config(read_timeout=read_timeout))
        _clients[key] = boto3.client("lambda", region_name=region, config=config)
    return _clients[key]


def measure(
    site: dict,
    scene: dict,
    backend: str = "opencv",
    params: dict | None = None,
    coverage_floor: float = 0.7,
    image: bool = True,
    client=None,
    task: str = contract.DEFAULT_TASK,
    network: dict | None = None,
    read_timeout: float | None = None,
) -> VisionResult:
    """Ask the worker for `backend` to measure `site` ({"id", "polygon"}) in `scene` ({"id",
    "captured_at", "assets"}) for `task`, sending `network` along when the task takes one. The
    request is validated here first, so a bad site config or network fails in the home region
    without a cross-region call. `read_timeout` (seconds) shortens how long the invoke waits."""
    arn = worker_arn(backend)
    if not arn:
        raise VisionError("invoke", f"no worker is configured for backend {backend!r}")
    request = {
        "version": contract.VERSION,
        "task": task,
        "backend": backend,
        "site": site,
        "scene": scene,
        "params": params or {},
        "coverage_floor": coverage_floor,
        "image": image,
    }
    if network is not None:
        request["network"] = network
    try:
        request = contract.validate_request(request, allowed_prefixes=_allowed_prefixes())
    except contract.ContractError as exc:
        raise VisionError("bad_request", str(exc)) from exc

    lambda_client = client or _client(region_of(arn), read_timeout)
    try:
        # UTF-8, not \u escapes: a network's non-ASCII names cost 2-3 bytes a character instead of
        # 6-12, against Lambda's 6 MB synchronous payload.
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
        response = lambda_client.invoke(FunctionName=arn, InvocationType="RequestResponse", Payload=payload)
        body = response["Payload"].read()
    except Exception as exc:  # botocore's errors, timeouts and throttles alike
        raise VisionError("invoke", str(exc)) from exc
    # Read once, right after the invoke answered: every failure from here on carries it, so a
    # refused or mismatched request still joins the worker's log line (the id keys its REPORT line).
    request_id = _request_id(response)
    if response.get("FunctionError"):
        raise VisionError("invoke", f"worker raised: {body[:300]!r}", request_id)

    try:
        reply = contract.validate_reply(json.loads(body))
    except (ValueError, contract.ContractError) as exc:
        raise VisionError("reply", str(exc), request_id) from exc
    if not reply["ok"]:
        raise VisionError(reply["error"], reply.get("detail", ""), request_id)
    answered = (reply["backend"], reply.get("site_id"), reply.get("scene_id"))
    if answered != (backend, site.get("id"), scene.get("id")):
        raise VisionError("reply", "the reply is for a different backend, site or scene", request_id)
    if reply.get("task", contract.DEFAULT_TASK) != task:
        raise VisionError("reply", "the reply is for a different task", request_id)
    try:
        image = _decode_image(reply.get("image_png_b64"))
        # Each figure is held to MAX_IMAGE_BYTES and the PNG signature on its own, like the map.
        figures = {
            name: _decode_image(b64, f"figure {name!r}")
            for name, b64 in (reply.get("figures_png_b64") or {}).items()
        }
    except VisionError as exc:
        exc.request_id = request_id
        raise
    return VisionResult(reply=reply, image_png=image, request_id=request_id, figures=figures)


def _request_id(response) -> str | None:
    """The Lambda request id of an invoke's response (ResponseMetadata.RequestId), or None when
    the response (a fake's, say) has none."""
    try:
        request_id = (response.get("ResponseMetadata") or {}).get("RequestId")
    except AttributeError:
        return None
    return request_id if isinstance(request_id, str) and request_id else None


def _decode_image(b64: str | None, what: str = "image") -> bytes | None:
    """The PNG in `b64`, or None for None; VisionError("reply", ...) naming `what` (the map, or
    which figure) when it is over MAX_IMAGE_BYTES, not base64 or not a PNG."""
    if b64 is None:
        return None
    if len(b64) > MAX_IMAGE_BYTES * 4 // 3 + 4:
        raise VisionError("reply", f"{what} too large")
    try:
        png = base64.b64decode(b64, validate=True)
    except ValueError as exc:
        raise VisionError("reply", f"{what} is not base64") from exc
    if not png.startswith(PNG_SIGNATURE):
        raise VisionError("reply", f"{what} is not a PNG")
    return png


def _allowed_prefixes() -> tuple[str, ...]:
    """The worker's own rule (vision_contract.allowed_prefixes), so a request the client lets
    through is one the worker accepts."""
    return contract.allowed_prefixes(os.environ.get("VISION_ALLOWED_URL_PREFIXES"))
