"""The pipeline's side of the vision worker: invoke it, wherever it runs, and check what comes back.

docs/enhancements/opencv-agentic-vision-enhancement.md §2-§3. The worker may run in another region
(next to the imagery), so the client is made for the region in the worker's ARN, not for the
Lambda's own. Configuration, from the environment (set by Terraform when `vision_enabled`):

    VISION_WORKER_ARN       the "opencv" backend's function ARN
    VISION_COOL_WORKER_ARN  the "cool" backend's, once it exists (docs §4); optional

A reply is checked against common/vision_contract.py before anything uses it. Every failure, from
the invoke, the worker or a malformed reply, raises `VisionError`; the adapter treats that site as
not measured this time, never as "nothing there".
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

import boto3
from botocore.config import Config

from common import vision_contract as contract

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# A reply's figure is at most 1024 px on its longer side; 3 MB is far above any such PNG and well
# under Lambda's 6 MB synchronous response limit.
MAX_IMAGE_BYTES = 3 * 1024 * 1024

_ENV_BY_BACKEND = {"opencv": "VISION_WORKER_ARN", "cool": "VISION_COOL_WORKER_ARN"}

# A worker call reads a few tiles and runs for seconds. No automatic retries: the next tick tries
# again, and a retry here would double a slow call's cost inside the research tick's own timeout.
_CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=90, retries={"max_attempts": 1, "mode": "standard"})
_clients: dict[str, object] = {}


class VisionError(RuntimeError):
    """The site could not be measured. `code` is the worker's error code, or "invoke" / "reply"."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


@dataclass
class VisionResult:
    reply: dict
    image_png: bytes | None

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


def _client(region: str):
    if region not in _clients:
        _clients[region] = boto3.client("lambda", region_name=region, config=_CLIENT_CONFIG)
    return _clients[region]


def measure(
    site: dict,
    scene: dict,
    backend: str = "opencv",
    params: dict | None = None,
    coverage_floor: float = 0.7,
    image: bool = True,
    client=None,
) -> VisionResult:
    """Ask the worker for `backend` to measure `site` ({"id", "polygon"}) in `scene` ({"id",
    "captured_at", "assets"}). The request is validated here first, so a bad site config fails in
    the home region without a cross-region call."""
    arn = worker_arn(backend)
    if not arn:
        raise VisionError("invoke", f"no worker is configured for backend {backend!r}")
    request = {
        "version": contract.VERSION,
        "backend": backend,
        "site": site,
        "scene": scene,
        "params": params or {},
        "coverage_floor": coverage_floor,
        "image": image,
    }
    try:
        request = contract.validate_request(request, allowed_prefixes=_allowed_prefixes())
    except contract.ContractError as exc:
        raise VisionError("bad_request", str(exc)) from exc

    lambda_client = client or _client(region_of(arn))
    try:
        response = lambda_client.invoke(
            FunctionName=arn, InvocationType="RequestResponse", Payload=json.dumps(request).encode("utf-8")
        )
        body = response["Payload"].read()
    except Exception as exc:  # botocore's errors, timeouts and throttles alike
        raise VisionError("invoke", str(exc)) from exc
    if response.get("FunctionError"):
        raise VisionError("invoke", f"worker raised: {body[:300]!r}")

    try:
        reply = contract.validate_reply(json.loads(body))
    except (ValueError, contract.ContractError) as exc:
        raise VisionError("reply", str(exc)) from exc
    if not reply["ok"]:
        raise VisionError(reply["error"], reply.get("detail", ""))
    answered = (reply["backend"], reply.get("site_id"), reply.get("scene_id"))
    if answered != (backend, site.get("id"), scene.get("id")):
        raise VisionError("reply", "the reply is for a different backend, site or scene")
    return VisionResult(reply=reply, image_png=_decode_image(reply.get("image_png_b64")))


def _decode_image(b64: str | None) -> bytes | None:
    if b64 is None:
        return None
    if len(b64) > MAX_IMAGE_BYTES * 4 // 3 + 4:
        raise VisionError("reply", "image too large")
    try:
        png = base64.b64decode(b64, validate=True)
    except ValueError as exc:
        raise VisionError("reply", "image is not base64") from exc
    if not png.startswith(PNG_SIGNATURE):
        raise VisionError("reply", "image is not a PNG")
    return png


def _allowed_prefixes() -> tuple[str, ...]:
    raw = os.environ.get("VISION_ALLOWED_URL_PREFIXES", "")
    return tuple(p.strip() for p in raw.split(",") if p.strip()) or contract.DEFAULT_ALLOWED_URL_PREFIXES
