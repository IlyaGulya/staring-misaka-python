from dataclasses import dataclass

from qrcode.image.base import BaseImage
from telethon.tl.custom import QRLogin


@dataclass
class QrCode:
    qr_login: QRLogin
    image: BaseImage


class LoginState:
    @dataclass
    class Idle:
        pass

    @dataclass
    class AlreadyAuthorized:
        pass

    @dataclass
    class PhoneEnter:
        current_phone: str = ""

    @dataclass
    class PhoneConfirm:
        current_phone: str
        code: str = ""

    @dataclass
    class QrGenerate:
        pass

    @dataclass
    class QrWaiting:
        qr_code: QrCode

    @dataclass
    class QrExpired:
        qr_code: QrCode


type PhoneLogin = LoginState.PhoneEnter | LoginState.PhoneConfirm
type QrLogin = LoginState.QrGenerate | LoginState.QrWaiting | LoginState.QrExpired
type LoginStateUnion = LoginState.Idle | LoginState.AlreadyAuthorized | PhoneLogin | QrLogin
