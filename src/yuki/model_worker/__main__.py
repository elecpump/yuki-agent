from yuki.config import Config
from yuki.model_worker.agent import ModelWorkerAgent


def main() -> None:
    ModelWorkerAgent(Config.from_env()).run()


if __name__ == "__main__":
    main()
