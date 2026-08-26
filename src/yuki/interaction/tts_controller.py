import threading
import time
from dataclasses import dataclass

from yuki.logger import get_logger
from yuki.topics import Topics


logger = get_logger("yuki.interaction.tts_controller")


class EmotionMapper:
    _VECTORS = {
        "joy": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "anger": [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "sadness": [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "anxiety": [0.0, 0.0, 0.0, 0.6, 0.0, 0.4, 0.0, 0.0],
        "love": [0.6, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.4],
        "tired": [0.0, 0.0, 0.3, 0.0, 0.0, 0.7, 0.0, 0.0],
    }

    def map(self, emotion: object) -> list[float] | None:
        value = getattr(emotion, "value", emotion)
        if not isinstance(value, str):
            return None
        vector = self._VECTORS.get(value.lower())
        return list(vector) if vector is not None else None


@dataclass(frozen=True)
class _SpeechJob:
    generation: int
    text: str
    emotion: object


class _StaleJob(RuntimeError):
    pass


class TtsController:
    """Asynchronous latest-reply-wins synthesis and playback controller."""

    def __init__(self, model, player, bus, *, emotion_mapper=None) -> None:
        self._model = model
        self._player = player
        self._bus = bus
        self._emotion_mapper = emotion_mapper or EmotionMapper()
        self._condition = threading.Condition(threading.RLock())
        self._generation = 0
        self._pending: _SpeechJob | None = None
        self._active_job: _SpeechJob | None = None
        self._tts_is_active = False
        self._stopping = False
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="yuki-tts-controller",
        )
        self._thread.start()

    @property
    def is_active(self) -> bool:
        with self._condition:
            return self._tts_is_active

    def warmup(self) -> None:
        warmup = getattr(self._model, "warmup", None)
        if callable(warmup):
            warmup()

    def health(self) -> dict:
        check = getattr(self._model, "health", None)
        detail = check() if callable(check) else {}
        return {**detail, "active": self.is_active}

    def _is_current(self, job: _SpeechJob) -> bool:
        with self._condition:
            return not self._stopping and job.generation == self._generation

    def speak(self, text: str, emotion: object = "neutral") -> None:
        text = str(text).strip()
        if not text:
            return
        with self._condition:
            if self._stopping:
                return
            self._generation += 1
            job = _SpeechJob(self._generation, text, emotion)
            self._pending = job
            stop_active = self._tts_is_active
            self._condition.notify()
            if stop_active:
                self._player.stop()

    def stop(self) -> None:
        with self._condition:
            if self._stopping:
                return
            self._generation += 1
            self._pending = None
            self._player.stop()

    def _publish(self, topic: str, job: _SpeechJob) -> None:
        self._bus.publish(topic, {"text": job.text, "ts": time.time()})

    def _mark_speaking(self, job: _SpeechJob) -> None:
        with self._condition:
            if self._stopping or job.generation != self._generation:
                raise _StaleJob()
            self._tts_is_active = True
            self._active_job = job
        try:
            self._publish(Topics.TTS_SPEAKING, job)
        except Exception:
            with self._condition:
                if self._active_job == job:
                    self._tts_is_active = False
                    self._active_job = None
            raise

    def _finish(self, job: _SpeechJob) -> None:
        with self._condition:
            if not self._tts_is_active or self._active_job != job:
                return
            self._tts_is_active = False
            self._active_job = None
        try:
            self._publish(Topics.TTS_FINISHED, job)
        except Exception:
            logger.exception("failed to publish TTS finished")

    def _console_fallback(self, job: _SpeechJob) -> None:
        if self._is_current(job):
            print(f"[yuki] {job.text}", flush=True)

    def _guarded_chunks(self, job: _SpeechJob, chunks):
        iterator = iter(chunks)
        try:
            for chunk in iterator:
                if not self._is_current(job):
                    return
                yield chunk
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _prepend(first: bytes, iterator):
        try:
            yield first
            yield from iterator
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()

    def _process(self, job: _SpeechJob) -> None:
        if not self._is_current(job):
            return
        spoke = False

        def on_first_chunk() -> None:
            nonlocal spoke
            if not self._is_current(job):
                raise _StaleJob()
            self._mark_speaking(job)
            spoke = True

        try:
            emotion_vector = self._emotion_mapper.map(job.emotion)
            chunks = iter(self._model.synthesize_stream(job.text, emotion_vector=emotion_vector))
            if not self._is_current(job):
                close = getattr(chunks, "close", None)
                if callable(close):
                    close()
                return
            first = next(chunks, None)
            if first is None:
                self._console_fallback(job)
                return
            if not self._is_current(job):
                close = getattr(chunks, "close", None)
                if callable(close):
                    close()
                return
            completed = self._player.play_stream(
                self._guarded_chunks(job, self._prepend(first, chunks)),
                on_first_chunk=on_first_chunk,
            )
            if completed and not spoke:
                self._console_fallback(job)
        except _StaleJob:
            pass
        except Exception:
            if self._is_current(job):
                logger.exception("TTS synthesis or playback failed")
                self._console_fallback(job)
        finally:
            if spoke:
                self._finish(job)

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                job, self._pending = self._pending, None
            self._process(job)

    def shutdown(self) -> None:
        with self._condition:
            if self._stopping:
                return
            self._stopping = True
            self._generation += 1
            self._pending = None
            active_job = self._active_job if self._tts_is_active else None
            self._tts_is_active = False
            self._active_job = None
            self._condition.notify_all()
        self._player.stop()
        if active_job is not None:
            try:
                self._publish(Topics.TTS_FINISHED, active_job)
            except Exception:
                logger.exception("failed to publish TTS finished during shutdown")
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            logger.warning("TTS worker did not stop before shutdown timeout")
            return
        self._player.close()
