# Build FastETS for macOS (arm64 and x86_64).
#
#   ./packaging/build_macos.sh            # native arch
#   ARCH=x86_64 ./packaging/build_macos.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
ARCH="${ARCH:-arm64}"

PY="${PYTHON:-python3}"
VENV="${ROOT}/.venv-build"

if [ ! -d "${VENV}" ]; then
  "${PY}" -m venv "${VENV}"
fi
# shellcheck disable=SC1091
source "${VENV}/bin/activate"

python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller

pyinstaller --noconfirm --clean packaging/FastETS.spec

cd "${ROOT}/dist"
if [ -d "FastETS.app" ]; then
  zip -r "../FastETS-macOS-${ARCH}.zip" "FastETS.app" -x '*.DS_Store'
else
  zip -r "../FastETS-macOS-${ARCH}.zip" "FastETS"
fi

echo "built: dist/FastETS-macOS-${ARCH}.zip"
