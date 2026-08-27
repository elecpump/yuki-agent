from yuki.config import Config
from yuki.model_worker.agent import ModelWorkerAgent
from yuki.model_worker.assembly import assemble_model_worker


class FakeBus:
    def __init__(self):
        self.services = {}

    def respond(self, service, handler, *, lane="work"):
        self.services[service] = (handler, lane)


def test_worker_assembly_registers_catalog_and_services_without_loading_models():
    config = Config(
        vlm={"enabled": False},
        stt={"enabled": False},
        local_brain={"enabled": False},
        tts={"enabled": False},
        memory={"vector_enabled": False},
        models={
            "policies": {
                "local_chat": {"warmup": False},
                "stt": {"warmup": False},
                "tts": {"warmup": False},
            }
        },
    )
    bus = FakeBus()

    runtime = assemble_model_worker(config, bus)
    try:
        assert runtime.manager.names() == ["vlm", "stt", "local_chat", "tts"]
        assert "model/vlm_understand" in bus.services
        assert "models/operations/submit" in bus.services
        assert bus.services["models/health"][1] == "control"
        assert runtime.manager.get_model_health("vlm")["runtime_state"] == "disabled"
        preflight = runtime.manager.preflight("vlm")
        assert preflight["models"]["vlm"]["checks"][0]["name"] == "model_interface"
    finally:
        runtime.close()


def test_worker_health_declares_manager_gpu_and_each_model_component():
    agent = ModelWorkerAgent(Config(), bus=FakeBus())

    components = agent.health_components()

    assert {
        "manager",
        "manager_loop",
        "scheduler",
        "operations",
        "gpu_runtime",
        "model.vlm",
        "model.stt",
        "model.local_chat",
        "model.tts",
        "model.embedding",
    } <= components.keys()
