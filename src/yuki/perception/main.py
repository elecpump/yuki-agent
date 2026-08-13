from yuki.config import Config
from yuki.perception.agent import PerceptionAgent


def main() -> None:
    PerceptionAgent(Config.from_env()).run()


if __name__ == "__main__":
    main()
