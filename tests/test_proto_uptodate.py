import filecmp
import importlib.util
import tempfile
from pathlib import Path

COMMITTED_DIR = Path("src/yuki/proto")


def _load_generate():
    spec = importlib.util.spec_from_file_location(
        "generate_proto", Path("scripts/generate_proto.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.generate_to


def test_generated_proto_is_uptodate():
    generate_to = _load_generate()
    with tempfile.TemporaryDirectory() as tmp:
        generate_to(tmp)
        assert filecmp.cmp(
            Path(tmp) / "yuki_pb2.py",
            COMMITTED_DIR / "yuki_pb2.py",
            shallow=False,
        ), "yuki_pb2.py is out of date — run `python scripts/generate_proto.py`"


def test_generated_pyi_is_uptodate():
    generate_to = _load_generate()
    with tempfile.TemporaryDirectory() as tmp:
        generate_to(tmp)
        assert filecmp.cmp(
            Path(tmp) / "yuki_pb2.pyi",
            COMMITTED_DIR / "yuki_pb2.pyi",
            shallow=False,
        ), "yuki_pb2.pyi is out of date — run `python scripts/generate_proto.py`"
