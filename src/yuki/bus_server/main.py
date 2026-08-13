from yuki.bus_server.agent import BusServerAgent
from yuki.config import Config


def main() -> None:
    BusServerAgent(Config.from_env()).run()


if __name__ == "__main__":
    main()
