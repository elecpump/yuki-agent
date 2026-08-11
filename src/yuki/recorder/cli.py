import argparse
import io
import time
from pathlib import Path

from PIL import ImageGrab

from yuki.bus import MessageBus
from yuki.config import Config
from yuki.recorder.session import Session
from yuki.shutdown import ShutdownManager


def grab_frame() -> bytes:
    image = ImageGrab.grab()
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def run(session: Session, bus: MessageBus, grabber, interval_sec: float) -> None:
    def on_event(topic: str, payload: dict) -> None:
        session.record_event(topic, payload)

    bus.subscribe("event/", on_event)
    shutdown = ShutdownManager()
    shutdown.register_signal_handlers()
    next_grab = time.time()
    while not shutdown.shutdown_requested:
        now = time.time()
        if now >= next_grab and grabber is not None:
            session.save_frame(grabber())
            next_grab = now + interval_sec
        shutdown.wait(timeout=0.05)
    session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Record a browsing session: frames + events, no audio.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between frame grabs")
    parser.add_argument("--no-frames", action="store_true", help="record events only")
    args = parser.parse_args()

    config = Config.from_env()
    bus = MessageBus(base_port=config.base_port, role="node", hwm=config.hwm)
    session = Session(Path(args.output_dir))
    grabber = None if args.no_frames else grab_frame
    try:
        run(session, bus, grabber, args.interval)
    finally:
        session.close()
        bus.close()


if __name__ == "__main__":
    main()
