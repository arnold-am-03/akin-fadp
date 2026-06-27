"""
Obtén UNA SOLA VEZ tu refresh token de Dropbox (no caduca; ideal para Render).

Requisitos en https://www.dropbox.com/developers/apps  (tu app, Scoped, Full Dropbox):
  - Permissions: files.content.read y files.content.write  -> Submit
  - Settings: copia App key y App secret

Uso:
  pip install dropbox
  python obtener_refresh_token.py
Sigue el enlace, autoriza, pega el código y guarda el refresh token que imprime.
"""
from dropbox import DropboxOAuth2FlowNoRedirect

APP_KEY    = input("App key: ").strip()
APP_SECRET = input("App secret: ").strip()

flow = DropboxOAuth2FlowNoRedirect(APP_KEY, APP_SECRET, token_access_type="offline")
print("\n1) Abre este enlace y autoriza:\n   " + flow.start())
code = input("\n2) Pega aquí el código de autorización: ").strip()
res = flow.finish(code)

print("\n=== Guarda esto como variables de entorno en Render ===")
print("DBX_APP_KEY      =", APP_KEY)
print("DBX_APP_SECRET   =", APP_SECRET)
print("DBX_REFRESH_TOKEN=", res.refresh_token)
