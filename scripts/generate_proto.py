"""用 grpc_tools.protoc 重新生成 yuki_pb2.py（无系统 protoc 依赖）。"""

import os
import sys

from grpc_tools import protoc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROTO_DIR = os.path.join(ROOT, "proto")
OUT_DIR = os.path.join(ROOT, "src", "yuki", "proto")
GRPC_PROTO = os.path.join(os.path.dirname(protoc.__file__), "_proto")


def generate_to(out_dir: str = OUT_DIR) -> None:
    os.makedirs(out_dir, exist_ok=True)
    rc = protoc.main(
        [
            "protoc",
            f"-I{PROTO_DIR}",
            f"-I{GRPC_PROTO}",
            f"--python_out={out_dir}",
            f"--pyi_out={out_dir}",
            os.path.join(PROTO_DIR, "yuki.proto"),
        ]
    )
    if rc != 0:
        raise SystemExit(f"protoc failed with code {rc}")


def generate() -> None:
    generate_to(OUT_DIR)
    print(f"generated yuki_pb2.py / yuki_pb2.pyi -> {OUT_DIR}")


if __name__ == "__main__":
    generate()
