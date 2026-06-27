# Fichero FADP — mazo de Anki incremental

App web que mantiene **un único mazo de Anki** alimentado desde Dropbox. Detecta solo
las clases nuevas (PDFs marcados `CLASE_`), genera tarjetas con Gemini únicamente para
lo nuevo, reconstruye el mazo maestro y lo deja en Dropbox. Un botón, progreso en vivo.

Pensado para el examen de admisión de la Academia Diplomática del Perú (FADP), pero
sirve para cualquier curso organizado en carpetas `CURSO/SESIÓN/`.

## Cómo funciona

- Fuente de verdad: `fadp_estado.json` en Dropbox (todas las tarjetas + archivos ya
  procesados, identificados por `content_hash`, estable aunque renombres el PDF).
- Clasificación por marca: `CLASE_` se procesa, `LECTURA_` se ignora, sílabos fuera,
  y lo no marcado se omite y se lista. Tolera erratas (`CLAES_`, `LETURA_`…).
- El `.apkg` se reconstruye desde el estado en cada corrida → tarjetas idénticas y, en
  Anki, tu progreso se conserva (GUID por pregunta).

## Estructura

```
app.py                   # servidor Flask + botón + progreso en vivo
pipeline.py              # motor: escaneo incremental, Gemini, construcción del mazo
templates/index.html     # interfaz
obtener_refresh_token.py # helper de un solo uso para el token durable de Dropbox
requirements.txt
render.yaml              # despliegue en Render
```

## Variables de entorno

| Variable             | Para qué                                              |
|----------------------|------------------------------------------------------|
| `DBX_APP_KEY`        | App key de tu app de Dropbox                          |
| `DBX_APP_SECRET`     | App secret                                            |
| `DBX_REFRESH_TOKEN`  | Token durable (no caduca) — **recomendado en Render** |
| `GEMINI_API_KEY`     | API key de Google AI Studio                          |
| `GEMINI_MODEL`       | Por defecto `gemini-2.5-flash-lite`                   |
| `DROPBOX_BASE`       | Raíz de cursos (def. `/Aplicaciones/Rakuten Kobo/CURSO`) |
| `SESSION_FILTER`     | Opcional: `S3` para forzar solo esa sesión           |

> En local puedes usar `DROPBOX_TOKEN` (caduca en ~4 h). En Render usa el **refresh token**,
> o la app dejará de autenticarse a las pocas horas.

## Despliegue en Render

1. **Token durable de Dropbox** (una vez, en tu máquina):
   ```bash
   pip install dropbox
   python obtener_refresh_token.py
   ```
   Guarda los tres valores que imprime.

2. **Sube el código a GitHub** (repo nuevo):
   ```bash
   git init && git add . && git commit -m "FADP Anki incremental"
   git branch -M main
   git remote add origin https://github.com/TU_USUARIO/fadp-anki.git
   git push -u origin main
   ```

3. **Render** → New → Web Service → conecta el repo. Detecta `render.yaml`.
   Carga las variables de entorno de la tabla. Deploy.

4. Abre la URL, pulsa **Procesar actualizaciones** y observa el progreso. Cuando termine,
   reimporta el `.apkg` en Anki (o descárgalo con el botón).

## Notas

- **Un solo worker** (ya configurado): el estado de la corrida vive en memoria del proceso.
- La **primera corrida** procesa todo y puede tardar / toparse con el límite diario de
  Gemini. No se pierde nada: el estado se guarda tras cada archivo, así que basta volver a
  pulsar el botón para retomar solo lo que faltó.
- En el plan gratuito la instancia se duerme por inactividad; abrir la página la despierta.
  Si cierras la pestaña a media corrida, puede pausarse; al volver, retoma desde el último
  archivo guardado.
- Depurar tarjetas flojas: edita `fadp_estado.json` en Dropbox y vuelve a procesar.
