import importlib
import threading
from collections.abc import Callable, Iterable

from yuki.logger import get_logger


TTS_SAMPLE_RATE = 22050
logger = get_logger("yuki.interaction.audio_output")


class AudioPlayer:
    """Thread-safe, lazily initialized mono PCM output."""

    def __init__(
        self,
        *,
        chunk_size: int = 1024,
        stream_factory: Callable[[], object] | None = None,
        module_loader: Callable[[str], object] = importlib.import_module,
    ) -> None:
        self._chunk_size = int(chunk_size)
        self._stream_factory = stream_factory
        self._module_loader = module_loader
        self._lock = threading.RLock()
        self._interrupted = threading.Event()
        self._stream = None
        self._audio = None
        self._closed = False

    @staticmethod
    def _stream_is_closed(stream) -> bool:
        return bool(getattr(stream, "closed", False) or getattr(stream, "_closed", False))

    @staticmethod
    def _stream_is_active(stream) -> bool:
        is_active = getattr(stream, "is_active", None)
        return bool(is_active()) if callable(is_active) else True

    def _discard_stream_locked(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

    def _ensure_stream(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("audio player is closed")
            if self._stream is not None and not self._stream_is_closed(self._stream):
                if not self._stream_is_active(self._stream):
                    try:
                        self._stream.start_stream()
                    except Exception:
                        self._discard_stream_locked()
                    else:
                        return self._stream
                else:
                    return self._stream
            elif self._stream is not None:
                self._discard_stream_locked()

            if self._stream_factory is not None:
                self._stream = self._stream_factory()
                return self._stream

            pyaudio = self._module_loader("pyaudio")
            if self._audio is None:
                self._audio = pyaudio.PyAudio()
            self._stream = self._audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=TTS_SAMPLE_RATE,
                output=True,
                frames_per_buffer=self._chunk_size,
            )
            return self._stream

    def play_stream(
        self,
        chunks: Iterable[bytes],
        on_first_chunk: Callable[[], None] | None = None,
    ) -> bool:
        """Play chunks and return False if another thread interrupted playback."""
        self._interrupted.clear()
        self._ensure_stream()
        first = True
        iterator = iter(chunks)
        try:
            for chunk in iterator:
                if not chunk:
                    continue
                frame_bytes = self._chunk_size * 2
                for offset in range(0, len(chunk), frame_bytes):
                    if self._closed or self._interrupted.is_set():
                        return False
                    piece = chunk[offset : offset + frame_bytes]
                    if first:
                        if on_first_chunk is not None:
                            on_first_chunk()
                        first = False
                    if self._interrupted.is_set():
                        return False
                    with self._lock:
                        if self._closed or self._interrupted.is_set():
                            return False
                        self._stream.write(piece)
            return not self._interrupted.is_set()
        except Exception:
            with self._lock:
                self._discard_stream_locked()
            raise
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()

    def stop(self) -> None:
        self._interrupted.set()
        with self._lock:
            stream = self._stream
            if stream is None or self._stream_is_closed(stream):
                return
            try:
                if self._stream_is_active(stream):
                    stream.stop_stream()
            except Exception as exc:
                logger.warning("audio output stop failed", error=str(exc))
                self._discard_stream_locked()

    def close(self) -> None:
        self._interrupted.set()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            stream, self._stream = self._stream, None
            audio, self._audio = self._audio, None
            if stream is not None:
                try:
                    if not self._stream_is_closed(stream) and self._stream_is_active(stream):
                        stream.stop_stream()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            if audio is not None:
                try:
                    audio.terminate()
                except Exception:
                    pass
