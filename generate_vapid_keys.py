from py_vapid import Vapid01

vapid = Vapid01()
vapid.generate_keys()

public_key = vapid.public_key
private_key = vapid.private_key

public_pem = public_key.public_bytes(
    encoding=__import__("cryptography.hazmat.primitives.serialization").hazmat.primitives.serialization.Encoding.PEM,
    format=__import__("cryptography.hazmat.primitives.serialization").hazmat.primitives.serialization.PublicFormat.SubjectPublicKeyInfo,
).decode("utf-8")

private_pem = private_key.private_bytes(
    encoding=__import__("cryptography.hazmat.primitives.serialization").hazmat.primitives.serialization.Encoding.PEM,
    format=__import__("cryptography.hazmat.primitives.serialization").hazmat.primitives.serialization.PrivateFormat.PKCS8,
    encryption_algorithm=__import__("cryptography.hazmat.primitives.serialization").hazmat.primitives.serialization.NoEncryption(),
).decode("utf-8")

print("VAPID_PUBLIC_KEY=")
print(public_pem)

print("VAPID_PRIVATE_KEY=")
print(private_pem)
