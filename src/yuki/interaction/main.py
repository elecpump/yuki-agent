from yuki.config import Config
from yuki.interaction.agent import InteractionAgent


def main() -> None:
    InteractionAgent(Config.from_env()).run()


if __name__ == "__main__":
    main()
