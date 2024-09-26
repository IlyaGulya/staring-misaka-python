import asyncio
from typing import Callable

import segno
from nicegui import ui
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from login_state import LoginState, QrCode, LoginStateUnion

current_phone = ''
current_code = ''


# apply_login_state: typing.Callable[[LoginState], None] = None
#
# global set_login_state


@ui.refreshable
def create_ui(client: TelegramClient, set_login_state_listener: Callable[[Callable[[LoginStateUnion], None]], None]):
    login_state, set_login_state = ui.state(LoginState.Idle())
    status_message, set_status_message = ui.state('')

    def on_login_state_change(state: LoginStateUnion):
        set_login_state(state)

    set_login_state_listener(on_login_state_change)

    with ui.card().classes('w-96 m-auto p-4'):
        ui.label('Telegram Login').classes('text-2xl font-bold mb-4')

        @ui.refreshable
        def login_section():
            global current_phone, current_code, current_qr_code

            def login_another_way_button():
                ui.button(
                    'Login another way',
                    on_click=lambda: set_login_state(LoginState.Idle)
                ).classes('w-full mt-4')

            print("current state is", login_state)

            match login_state:
                case LoginState.Idle():
                    ui.label('Choose login method:').classes('text-lg font-semibold mb-2')
                    ui.button('QR CODE LOGIN', on_click=lambda: set_login_state(LoginState.QrGenerate)).classes(
                        'w-full mb-2')
                    ui.button('PHONE NUMBER LOGIN', on_click=lambda: set_login_state(LoginState.PhoneEnter)).classes(
                        'w-full')

                case LoginState.AlreadyAuthorized():
                    ui.label("Already authorized")

                case LoginState.PhoneEnter | LoginState.PhoneConfirm:
                    match login_state:
                        case LoginState.PhoneEnter:
                            ui.label('Login with Phone Number').classes('text-lg font-semibold mb-2')

                            def update_phone(new_phone):
                                global current_phone
                                current_phone = new_phone.value

                            ui.input('Phone Number', value=current_phone,
                                     on_change=update_phone).classes('w-full mb-2')

                            async def send_code():
                                try:
                                    await client.send_code_request(current_phone)
                                    set_login_state(LoginState.PhoneConfirm)
                                    set_status_message('Code sent! Please enter it below.')
                                except Exception as e:
                                    print(e)
                                    set_status_message(f'Error sending code: {str(e)}')

                            ui.button('SEND CODE', on_click=send_code).classes('w-full')
                        case LoginState.PhoneConfirm:
                            ui.input('Verification Code', value=current_code,
                                     on_change=lambda e: setattr(create_ui, 'current_code', e.value)).classes(
                                'w-full mb-2')

                            async def verify_code():
                                try:
                                    await client.sign_in(phone=current_phone, code=current_code)
                                    set_status_message('Login successful!')
                                except SessionPasswordNeededError:
                                    set_status_message(
                                        'Two-factor authentication is enabled. Please enter your password.')
                                except Exception as e:
                                    set_status_message(f'Login failed: {str(e)}')

                            ui.button('VERIFY CODE', on_click=verify_code).classes('w-full')

                    login_another_way_button()

                case LoginState.QrGenerate | LoginState.QrWaiting | LoginState.QrExpired:
                    match login_state:
                        case LoginState.QrWaiting:
                            if current_qr_code:
                                ui.html(login_state.image.get_image()).classes('w-full aspect-square')
                                ui.label('Scan the QR code with your Telegram app').classes('mt-2 text-sm')
                        case LoginState.QrExpired:
                            ui.label('QR code expired. Please generate a new one.').classes('mt-2 text-sm')
                            ui.button('GENERATE NEW QR CODE', on_click=generate_qr).classes('w-full')
                        case LoginState.QrGenerate:
                            ui.button('GENERATE QR CODE', on_click=generate_qr).classes('w-full')

                    login_another_way_button()


        async def generate_qr():
            try:
                qr_login = await client.qr_login()
                qr_image = segno.make(qr_login.url, micro=False).svg_data_uri()
                set_login_state(LoginState.QrWaiting(QrCode(qr_login=qr_login, image=qr_image)))
                set_status_message('QR code generated. Please scan with your Telegram app.')

                # Wait for QR login
                try:
                    user = await qr_login.wait()
                    set_status_message(f'Login successful! Welcome, {user.first_name}!')
                except asyncio.TimeoutError:
                    set_login_state(LoginState.QrExpired)
                    set_status_message('QR code expired. Please generate a new one.')

            except Exception as e:
                set_status_message(f'Error generating QR code: {str(e)}')

        login_section()

        ui.label(status_message).classes('mt-2 text-sm')


def render_ui(client: TelegramClient, set_login_state_listener: Callable[[Callable[[LoginStateUnion], None]], None]):
    create_ui(client, set_login_state_listener)
    ui.run(show=False)
