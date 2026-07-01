import pathlib

f = pathlib.Path('/usr/local/lib/python3.11/site-packages/SpotipyFree/Formatter.py')
content = f.read_text()

# Fix 1: ownerV2 key missing (already patched - idempotent check)
old1 = 'playlist["owner"] = playlist["ownerV2"]["data"]'
new1 = 'owner_v2 = playlist.get("ownerV2") or {}; playlist["owner"] = owner_v2.get("data", playlist.get("owner", {}))'
content = content.replace(old1, new1)

# Fix 2: owner["name"] key missing -> use get() with fallbacks
old2 = '        playlist["owner"]["display_name"] = playlist["owner"]["name"]'
new2 = '        playlist["owner"]["display_name"] = playlist["owner"].get("name", playlist["owner"].get("displayName", "Unknown"))'
content = content.replace(old2, new2)

# Fix 3: owner["uri"] might be missing
old3 = '        playlist["external_urls"]["spotify"] = playlist["owner"]["uri"]'
new3 = '        playlist["external_urls"]["spotify"] = playlist["owner"].get("uri", "")'
content = content.replace(old3, new3)

f.write_text(content)

# Verify all three patches applied
checks = [
    ('ownerV2 patch', 'owner_v2 = playlist.get("ownerV2")'),
    ('name patch', 'playlist["owner"].get("name"'),
    ('uri patch', 'playlist["owner"].get("uri"'),
]
for label, needle in checks:
    status = "OK" if needle in content else "MISSING"
    print(f"{label}: {status}")
