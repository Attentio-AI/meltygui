#!/bin/bash
set -euo pipefail
kind=$1
mkdir -p "$HOME"
export PATH=/opt/python/cp312-cp312/bin:$PATH
python -m venv /tmp/build-env
export PATH=/tmp/build-env/bin:$PATH
python -m pip install -q uv==0.11.19 auditwheel==6.4.2 patchelf==0.17.2.4
if [ "$kind" = pycuda ]; then
  export CUDA_ROOT=/usr/local/cuda-12.1
  export PATH=$CUDA_ROOT/bin:$PATH
  export LD_LIBRARY_PATH=$CUDA_ROOT/lib64
fi
uv build --python /opt/python/cp312-cp312/bin/python /source --out-dir /output/raw
for wheel in /output/raw/*.whl; do
  auditwheel repair --plat manylinux_2_28_x86_64 --exclude libcuda.so.1 --wheel-dir /output "$wheel"
done
cp /output/raw/*.tar.gz /output/
for wheel in /output/*.whl; do
  auditwheel show "$wheel" > /output/auditwheel.txt
done
