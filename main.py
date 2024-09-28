import logging

from db import create_session
from llm import create_llm
from telegram import create_bot


def main():
    session = create_session()
    llm = create_llm()
    bot = create_bot(session, llm)

    bot.start()

    bot.run_until_disconnected()


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
    main()
