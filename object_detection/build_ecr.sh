#!/bin/sh
#
# Build the cloud processor as a container image and push it to ECR.
#
# This is the counterpart to the usual processor `build.sh` (which produces a
# package.zip), and exists because doover's processor pipeline cannot deploy this app:
#
#   1. `doover-control` hardcodes `PackageType="Zip"` with an inline
#      `Code={"ZipFile": ...}` (applications/views.py) -- there is no image path.
#   2. `lambda_config` is whitelisted server-side to
#      (Environment, Runtime, Timeout, Handler, MemorySize, Architectures), so
#      `PackageType` / `Code.ImageUri` / `ImageConfig` are rejected outright
#      (applications/serializers_a.py).
#   3. The inline ZipFile upload caps around 50MB. This app's dependencies are 288MB
#      unzipped / 113MB zipped before the 22MB of weights, so the zip route can't
#      carry it either -- see Dockerfile.processor for the measurements.
#
# So the image goes to ECR and the function is created/updated directly against AWS.
# Requires the caller's own AWS credentials with ECR push + (for --deploy) Lambda write.
#
# Usage:
#   ./build_processor.sh                          # build + push :latest
#   ./build_processor.sh --tag v3 --deploy        # push :v3 and update the function
#   ECR_REPO=my-repo AWS_REGION=us-east-1 ./build_processor.sh
#
# One thing NOT to copy from doover's pipeline: its `MemorySize` <= 1024 validation is
# doover's own limit, not AWS's. Deploying out-of-band we aren't bound by it, which
# matters because Lambda scales vCPU with memory (~1 vCPU per 1769MB). At 1024MB you get
# roughly half a core -- comparable to the single CM4 core the on-device app already
# uses, so the cloud variant would gain little. MEMORY_SIZE below defaults higher on
# purpose.

set -eu

AWS_REGION="${AWS_REGION:-ap-southeast-2}"
ECR_REPO="${ECR_REPO:-doover/object-detection-processor}"
FUNCTION_NAME="${FUNCTION_NAME:-doover-object-detection-processor}"
# arm64 = Graviton: cheaper per ms, and the same architecture the device image is built
# for, so `common` runs on wheels we've already exercised.
ARCH="${ARCH:-arm64}"
MEMORY_SIZE="${MEMORY_SIZE:-3072}"
# Generous but bounded. Cold start is ~200ms of model load plus image init; a frame is
# ~1-2s of inference. Anything approaching this means something is wrong, not slow.
TIMEOUT="${TIMEOUT:-120}"

TAG="latest"
DEPLOY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --tag) TAG="$2"; shift 2 ;;
        --deploy) DEPLOY=1; shift ;;
        --region) AWS_REGION="$2"; shift 2 ;;
        --repo) ECR_REPO="$2"; shift 2 ;;
        --function) FUNCTION_NAME="$2"; shift 2 ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

case "$ARCH" in
    arm64) DOCKER_PLATFORM="linux/arm64"; LAMBDA_ARCH="arm64" ;;
    amd64|x86_64) DOCKER_PLATFORM="linux/amd64"; LAMBDA_ARCH="x86_64" ;;
    *) echo "unsupported ARCH: $ARCH (use arm64 or amd64)" >&2; exit 2 ;;
esac

for tool in aws docker; do
    command -v "$tool" >/dev/null || { echo "$tool is required" >&2; exit 1; }
done

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE_URI="${REGISTRY}/${ECR_REPO}:${TAG}"

echo "account   : ${ACCOUNT_ID}"
echo "region    : ${AWS_REGION}"
echo "image     : ${IMAGE_URI}"
echo "platform  : ${DOCKER_PLATFORM}"

# Idempotent: describe first so a re-run doesn't fail on an existing repo.
if ! aws ecr describe-repositories --region "$AWS_REGION" \
        --repository-names "$ECR_REPO" >/dev/null 2>&1; then
    echo "creating ECR repository ${ECR_REPO}..."
    # Lambda reads the image on every cold start, so scanning on push is worth having;
    # AES256 encryption is the default and costs nothing.
    aws ecr create-repository --region "$AWS_REGION" \
        --repository-name "$ECR_REPO" \
        --image-scanning-configuration scanOnPush=true \
        --encryption-configuration encryptionType=AES256 >/dev/null
fi

echo "logging docker into ECR..."
aws ecr get-login-password --region "$AWS_REGION" \
    | docker login --username AWS --password-stdin "$REGISTRY"

echo "building..."
# --provenance=false: Lambda rejects an image whose manifest is an OCI index carrying
# attestations, which is what buildx produces by default. It wants a single manifest.
docker build \
    --platform "$DOCKER_PLATFORM" \
    --provenance=false \
    -f Dockerfile.processor \
    -t "$IMAGE_URI" \
    .

echo "pushing..."
docker push "$IMAGE_URI"

DIGEST="$(aws ecr describe-images --region "$AWS_REGION" \
    --repository-name "$ECR_REPO" \
    --image-ids "imageTag=${TAG}" \
    --query 'imageDetails[0].imageDigest' --output text)"
echo "pushed ${IMAGE_URI}"
echo "digest  ${DIGEST}"

if [ "$DEPLOY" -eq 0 ]; then
    cat <<EOF

Not deploying (pass --deploy to do it). To wire it up by hand:

  # First time -- note Runtime/Handler are omitted: they are invalid for an image
  # package, and the handler comes from the Dockerfile's CMD.
  aws lambda create-function --region ${AWS_REGION} \\
    --function-name ${FUNCTION_NAME} \\
    --package-type Image \\
    --code ImageUri=${IMAGE_URI} \\
    --architectures ${LAMBDA_ARCH} \\
    --memory-size ${MEMORY_SIZE} --timeout ${TIMEOUT} \\
    --role arn:aws:iam::${ACCOUNT_ID}:role/<lambda-execution-role> \\
    --environment "Variables={APPLICATION_ID=<app-id>,DOOVER_DATA_ENDPOINT=<url>}"

  # Subsequent pushes. Prefer the digest: a moving tag will not redeploy on its own,
  # since Lambda resolves the tag once at update time.
  aws lambda update-function-code --region ${AWS_REGION} \\
    --function-name ${FUNCTION_NAME} \\
    --image-uri ${REGISTRY}/${ECR_REPO}@${DIGEST}

APPLICATION_ID and DOOVER_DATA_ENDPOINT are what doover injects for a normal processor
(see build_lambda_config in doover-control applications/views.py); an out-of-band
function has to be given them explicitly or pydoover will not know which app it is.
EOF
    exit 0
fi

if aws lambda get-function --region "$AWS_REGION" \
        --function-name "$FUNCTION_NAME" >/dev/null 2>&1; then
    echo "updating ${FUNCTION_NAME}..."
    aws lambda update-function-code --region "$AWS_REGION" \
        --function-name "$FUNCTION_NAME" \
        --image-uri "${REGISTRY}/${ECR_REPO}@${DIGEST}" >/dev/null
    aws lambda wait function-updated --region "$AWS_REGION" \
        --function-name "$FUNCTION_NAME"
    echo "updated."
else
    echo "Function ${FUNCTION_NAME} does not exist." >&2
    echo "Create it once with the command printed above (it needs an execution role" >&2
    echo "and the doover environment variables), then re-run with --deploy." >&2
    exit 1
fi
