from py_vapid import Vapid01

vapid = Vapid01()
vapid.generate_keys()

print("")
print("COPY THESE INTO RENDER ENVIRONMENT VARIABLES:")
print("")
print("VAPID_PUBLIC_KEY=" + vapid.public_key_b64)
print("")
print("VAPID_PRIVATE_KEY=" + vapid.private_key_b64)
print("")
