import logging

from db import create_session
from env import BOT_TOKEN
from llm import create_llm
from telegram import create_bot
from userbot import create_userbot


def main():
    session = create_session()
    llm = create_llm()
    userbot = create_userbot()
    bot = create_bot(session, llm, userbot)

    userbot.start()
    bot.start(bot_token=BOT_TOKEN)

    bot.loop.run_forever()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
    main()
