# The vision worker on COOL: OpenCV's Cloud-Optimized OpenCV Library (an OpenCV 5 build tuned for
# Graviton3/4 with KleidiCV), from OpenCV's own image on AWS Marketplace, running the same handler
# as the stock-OpenCV worker. Design: docs/enhancements/opencv-agentic-vision-enhancement.md §4.
#
# Build from the repository root, on (or for) arm64, with the COOL image's URI pinned by digest:
#
#   docker build --platform linux/arm64 -f docker/vision-cool/Dockerfile \
#     --build-arg COOL_IMAGE=<registry>/<repository>@sha256:<digest> -t bloggerbear-vision-cool .
#
# The URI comes from the Marketplace subscription (docs/deployment-runsheet.md, "The COOL
# backend"); it is not written here because it belongs to that subscription.
#
# Then, on a Graviton3/4 host, print the build's fingerprint (what COOL_BUILD_SHA256 is set to) and
# run the benchmark against the stock build's numbers:
#
#   docker run --rm --entrypoint python3 bloggerbear-vision-cool -c \
#     "from vision import build; print(build.build_info())"
#   docker run --rm --entrypoint python3 bloggerbear-vision-cool bench/vision_benchmark.py kernels
#
# As a container-image Lambda the entrypoint below is the runtime interface client; whether the
# COOL build runs on Lambda's arm64 CPUs is the open question in §8 of the design.
ARG COOL_IMAGE
FROM ${COOL_IMAGE}

WORKDIR /var/task

COPY lambdas/requirements-vision-cool.txt /tmp/requirements-vision-cool.txt
RUN python3 -m pip install --no-cache-dir -r /tmp/requirements-vision-cool.txt

COPY lambdas/vision/ /var/task/vision/
COPY lambdas/vision_worker_handler.py /var/task/
COPY lambdas/common/__init__.py lambdas/common/vision_contract.py /var/task/common/
COPY scripts/vision_benchmark.py scripts/vision_benchmark_scenes.json /var/task/bench/

ENV PYTHONPATH=/var/task \
    VISION_BACKEND=cool

# Fail the build, not the first request, if the image's cv2 is not OpenCV 5.
RUN python3 -c "import cv2, sys; sys.exit(0 if cv2.__version__.startswith('5.') else 'cv2 ' + cv2.__version__ + ' is not OpenCV 5')"

ENTRYPOINT ["python3", "-m", "awslambdaric"]
CMD ["vision_worker_handler.lambda_handler"]
