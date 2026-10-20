# Jarvis 🤖

Asistente por voz en español que controla tu computadora. Le hablás ("Jarvis, abrí Excel"), la IA entiende lo que querés y Jarvis lo hace. Funciona con **Gemini o Claude**, y cambiar de una a otra es cambiar una línea.

## Qué puede hacer hoy

| Acción | Ejemplo |
|---|---|
| Abrir programas | "Jarvis, abrí Excel" · "abrí Antigravity" |
| Abrir carpetas | "abrí mis descargas" |
| Buscar y abrir archivos | "buscá el archivo presupuesto y abrilo" |
| Navegar | "buscá en internet cómo instalar FastAPI" |
| Terminal (Windows o Linux/WSL) | "en la terminal de Linux ejecutá `pip list`" |
| Escribir texto | "abrí el bloc de notas y escribí la lista del súper" |
| WhatsApp | "mandale a Juan que llego en diez minutos" |
| Conversar | "¿qué conviene usar, Django o FastAPI?" … "¿y por qué?" |

No hace falta decir frases exactas: la IA decide qué acción usar y puede encadenar varias.

## Seguridad incluida

- Los comandos que solo leen (`dir`, `git status`, `pip list`) corren solos. **Todo lo demás te pide confirmación**, y lo peligroso (borrar, formatear, `git push --force`) se marca con advertencia.
- **WhatsApp siempre confirma** a quién y qué antes de enviar.
- Las acciones son una **lista cerrada** (`jarvis/herramientas.py`): la IA no puede inventar acciones nuevas.
- Todo lo que hace queda registrado en `jarvis.log`.
- Tu clave va en `.env`, que **nunca se sube a GitHub** (`.gitignore`).

## Instalación (Windows)

1. Instalá **Python 3.11 o más nuevo** desde python.org (marcá "Add Python to PATH").
2. En una terminal, dentro de esta carpeta:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Sacá una clave gratuita de Gemini en https://aistudio.google.com/apikey
4. Copiá `.env.example` como `.env` y pegá la clave en `GEMINI_API_KEY=`.
5. (Opcional) Copiá `contactos.ejemplo.json` como `contactos.json` y poné tus contactos de WhatsApp.

## Uso

```bash
python main.py          # modo texto: escribís los pedidos (probá esto primero)
python main.py --voz    # modo voz: decí "Jarvis" y tu pedido
```

En modo voz, después de cada respuesta Jarvis sigue escuchando 30 segundos sin que repitas su nombre. Decí "gracias" para terminar la charla o "apagate" para cerrarlo. `Ctrl+C` lo corta siempre.

### Cambiar a Claude

En `.env`:
```
JARVIS_MODELO=anthropic/claude-sonnet-5-5
ANTHROPIC_API_KEY=tu-clave
```

### Voz más natural

En `.env` poné `JARVIS_MOTOR_VOZ=edge` (necesita internet).

## Estructura

```
main.py                  arranque, modo texto y modo voz
jarvis/config.py         configuración (lee .env)
jarvis/cerebro.py        habla con la IA y decide qué herramientas usar
jarvis/herramientas.py   las acciones sobre la computadora
jarvis/seguridad.py      qué se confirma y qué no
jarvis/voz.py            micrófono y voz
tests/                   pruebas: python -m pytest
```

## Agregar una acción nueva

En `jarvis/herramientas.py`:

```python
@herramienta
def bajar_volumen(porcentaje: int) -> str:
    """Baja el volumen de la computadora.
    porcentaje: cuánto bajarlo, de 0 a 100.
    """
    ...
    return "Bajé el volumen."
```

Con eso la IA ya la conoce y la usa sola cuando corresponde.

## Tené en cuenta

- El reconocimiento de voz usa el servicio gratuito de Google: **tu audio se envía a Google**. Para hacerlo sin internet, el próximo paso es cambiarlo por `faster-whisper`.
- En modo voz, la palabra "Jarvis" se detecta reconociendo todo lo que escucha, lo que usa internet continuamente. Un detector local como `openWakeWord` lo haría más liviano.

## Hoja de ruta

- [x] Núcleo: voz en español, IA intercambiable, acciones con confirmación
- [ ] Detector local de "Jarvis" (openWakeWord) y voz sin internet (faster-whisper)
- [ ] Reconocimiento de tu voz (Resemblyzer) y de tu cara (face_recognition) para acciones delicadas
- [ ] Contraseñas guardadas en el Administrador de credenciales de Windows (`keyring`)
- [ ] Aula virtual (API de Moodle o Playwright)
- [ ] Modo "mirar la pantalla" para lo que no está programado (Microsoft UFO)
- [ ] Tareas de programación: issues de GitHub con `gh` + Gemini CLI o Claude Code, siempre en una rama aparte
- [ ] Memoria a largo plazo (SQLite + ChromaDB)
- [ ] Herramientas como servidor MCP
- [ ] n8n para mails, calendario y Drive
- [ ] Ventana para ver qué escuchó y qué está haciendo
