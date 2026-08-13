from yuki.config import Config
from yuki.cognition.agent import CognitionAgent


def main() -> None:
    CognitionAgent(Config.from_env()).run()


if __name__ == "__main__":
    main()
