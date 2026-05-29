import base64
from py_vapid import Vapid01
from cryptography.hazmat.primitives import serialization


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


vapid = Vapid01()
vapid.generate_keys()

private_key = vapid.private_key
public_key = private_key.public_key()
public_numbers = public_key.public_numbers()
x = public_numbers.x.to_bytes(32, "big")
y = public_numbers.y.to_bytes(32, "big")
public_key_b64 = b64url(b"\x04" + x + y)

private_key_pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")

print("")
print("COPY THIS INTO RENDER:")
print("")
print("Key:")
print("VAPID_PUBLIC_KEY")
print("")
print("Value:")
print(public_key_b64)
print("")
print("Key:")
print("VAPID_PRIVATE_KEY")
print("")
print("Value:")
print(private_key_pem)
print("")
print("IMPORTANT: Put these only in Render Environment.")
