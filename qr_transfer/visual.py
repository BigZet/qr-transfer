"""Optional QR adapter shared by AQR1 and AQR2."""
import io


def encode_qr(raw: bytes, box_size: int = 6) -> bytes:
    import qrcode
    from qrcode.util import QRData, MODE_8BIT_BYTE

    if box_size < 1:
        raise ValueError("Positive QR module size required")
    qr = qrcode.QRCode(version=40, error_correction=qrcode.constants.ERROR_CORRECT_L,
                       box_size=box_size, border=4)
    qr.add_data(QRData(raw, mode=MODE_8BIT_BYTE), optimize=0)
    qr.make(fit=False)
    buffer = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def decode_qr(png: bytes) -> list[bytes]:
    from PIL import Image
    import zxingcpp

    with Image.open(io.BytesIO(png)) as image:
        return [bytes(code.bytes) for code in zxingcpp.read_barcodes(image)]
