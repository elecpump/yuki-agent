import argparse
import io
from pathlib import Path

from PIL import ImageGrab

from yuki.config import Config
from yuki.recorder.agent import RecorderAgent
from yuki.recorder.session import Session


def grab_frame() -> bytes:
    image = ImageGrab.grab()
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description="Record a browsing session: frames + events, no audio.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between frame grabs")
    parser.add_argument("--no-frames", action="store_true", help="record events only")
    args = parser.parse_args()

    config = Config.from_env()
    session = Session(Path(args.output_dir))
    grabber = None if args.no_frames else grab_frame
    RecorderAgent(config, session=session, grabber=grabber, interval_sec=args.interval).run()


if __name__ == "__main__":
    main()
