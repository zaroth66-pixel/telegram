# decode_session.py
import struct

with open('stolen_+251716932777_1783416171.session', 'rb') as f:
    data = f.read()
    
# First 4 bytes = DC ID
dc_id = struct.unpack('<I', data[:4])[0]
print(f"DC ID: {dc_id}")

# Next 8 bytes = Auth key (encrypted)
auth_key = data[4:12]
print(f"Auth Key (first 8 bytes): {auth_key.hex()[:16]}...")

# It's encrypted — you'd need the password to decrypt it.
# Decoding is useless — the session itself IS the credential.