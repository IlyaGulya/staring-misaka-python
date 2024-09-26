# from typing import Callable

from db import create_session
from llm import create_llm
# from login_state import LoginStateUnion
from telegram import create_bot

# login_state_listener: Callable[[LoginStateUnion], None] = None


# def set_login_state_listener(listener: Callable[[LoginStateUnion], None]) -> None:
#     global login_state_listener
#     login_state_listener = listener
#     pass


def main():
    session = create_session()
    llm = create_llm()
    bot = create_bot(session, llm)

    bot.start()

    # async def on_startup():
    #     await bot.connect()
    #     print("Telegram client started")
    #     if await bot.is_user_authorized():
    #         login_state_listener(LoginState.AlreadyAuthorized())
    #
    # async def on_shutdown():
    #     await bot.disconnect()
    #     print("Telegram client stopped")
    #
    # app.on_startup(on_startup)
    # app.on_shutdown(on_shutdown)
    #
    # render_ui(bot, set_login_state_listener)


if __name__ == '__main__':
    main()
