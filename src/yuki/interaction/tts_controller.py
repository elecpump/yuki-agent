import threading
import time
from dataclasses import dataclass, field
from typing import ClassVar

from yuki.logger import get_logger
from yuki.topics import Topics

logger = get_logger("yuki.interaction.tts_controller")


class EmotionMapper:
    _VECTORS: ClassVar[dict[str, list[float]]] = {
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
    kind: str = "final"
    reply_id: str | None = None
    cancelled: threading.Event = field(default_factory=threading.Event, compare=False)
    done: threading.Event = field(default_factory=threading.Event, compare=False)


class _StaleJob(RuntimeError):
    pass


class TtsController:
    """Asynchronous, kind-aware speech synthesis and playback controller."""

    def __init__(
        self,
        model,
        player,
        bus,
        *,
        emotion_mapper=None,
        transition_grace_s: float = 0.8,
    ) -> None:
        self._model = model
        self._player = player
        self._bus = bus
        self._emotion_mapper = emotion_mapper or EmotionMapper()
        self._transition_grace_s = max(0.0, float(transition_grace_s))
        self._condition = threading.Condition(threading.RLock())
        self._generation = 0
        self._pending: _SpeechJob | None = None
        self._processing_job: _SpeechJob | None = None
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
            return not self._stopping and not job.cancelled.is_set()

    @staticmethod
    def _same_identified_reply(job: _SpeechJob, reply_id: str | None) -> bool:
        return bool(reply_id) and job.reply_id == reply_id

    def _has_final_locked(self) -> bool:
        return any(
            job is not None and job.kind == "final" and not job.cancelled.is_set()
            for job in (self._pending, self._processing_job)
        )

    def speak(
        self,
        text: str,
        emotion: object = "neutral",
        *,
        kind: str = "final",
        reply_id: str | None = None,
    ) -> None:
        text = str(text).strip()
        if not text:
            return
        kind = "transition" if kind == "transition" else "final"
        stop_active = False
        grace_job: _SpeechJob | None = None
        with self._condition:
            if self._stopping:
                return
            if kind == "transition":
                if self._has_final_locked():
                    return
                if self._processing_job is not None and not self._processing_job.cancelled.is_set():
                    return
                if self._pending is not None:
                    self._pending.cancelled.set()
            else:
                if self._pending is not None:
                    self._pending.cancelled.set()
                    self._pending = None
                processing = self._processing_job
                if processing is not None and not processing.cancelled.is_set():
                    matching_transition = (
                        processing.kind == "transition"
                        and self._same_identified_reply(processing, reply_id)
                    )
                    if matching_transition and self._active_job is processing:
                        grace_job = processing
                    else:
                        processing.cancelled.set()
                        stop_active = self._active_job is processing

            self._generation += 1
            self._pending = _SpeechJob(
                self._generation,
                text,
                emotion,
                kind=kind,
                reply_id=reply_id,
            )
            self._condition.notify()

        if stop_active:
            self._player.stop()
        if grace_job is not None:
            self._stop_transition_after_grace(grace_job)

    def _stop_transition_after_grace(self, job: _SpeechJob) -> None:
        def expire() -> None:
            should_stop = False
            with self._condition:
                if (
                    not self._stopping
                    and self._active_job is job
                    and not job.cancelled.is_set()
                ):
                    job.cancelled.set()
                    should_stop = True
            if should_stop:
                self._player.stop()

        timer = threading.Timer(self._transition_grace_s, expire)
        timer.daemon = True
        timer.start()

    def cancel(self, reply_id: str | None) -> None:
        if not reply_id:
            return
        stop_active = False
        with self._condition:
            pending = self._pending
            if (
                pending is not None
                and pending.kind == "transition"
                and pending.reply_id == reply_id
            ):
                pending.cancelled.set()
                self._pending = None
            processing = self._processing_job
            if (
                processing is not None
                and processing.kind == "transition"
                and processing.reply_id == reply_id
                and not processing.cancelled.is_set()
            ):
                processing.cancelled.set()
                stop_active = self._active_job is processing
            self._condition.notify()
        if stop_active:
            self._player.stop()

    def stop(self) -> None:
        with self._condition:
            if self._stopping:
                return
            if self._pending is not None:
                self._pending.cancelled.set()
            self._pending = None
            if self._processing_job is not None:
                self._processing_job.cancelled.set()
            self._player.stop()

    def _publish(self, topic: str, job: _SpeechJob) -> None:
        self._bus.publish(
            topic,
            {
                "text": job.text,
                "ts": time.time(),
                "kind": job.kind,
                "reply_id": job.reply_id,
            },
        )

    def _mark_speaking(self, job: _SpeechJob) -> None:
        with self._condition:
            if self._stopping or job.cancelled.is_set():
                raise _StaleJob()
            self._tts_is_active = True
            self._active_job = job
        try:
            self._publish(Topics.TTS_SPEAKING, job)
        except Exception:
            with self._condition:
                if self._active_job is job:
                    self._tts_is_active = False
                    self._active_job = None
            raise

    def _finish(self, job: _SpeechJob) -> None:
        with self._condition:
            if not self._tts_is_active or self._active_job is not job:
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
                self._processing_job = job
            try:
                self._process(job)
            finally:
                with self._condition:
                    if self._processing_job is job:
                        self._processing_job = None
                    job.done.set()
                    self._condition.notify_all()

    def shutdown(self) -> None:
        with self._condition:
            if self._stopping:
                return
            self._stopping = True
            if self._pending is not None:
                self._pending.cancelled.set()
            self._pending = None
            if self._processing_job is not None:
                self._processing_job.cancelled.set()
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
