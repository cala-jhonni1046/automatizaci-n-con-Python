"""El cerebro: habla con la IA (Gemini, Claude u otra) y decide qué herramientas usar.

Con JARVIS_GEMINI_WEB=true (por defecto) el razonamiento, las dudas técnicas, las explicaciones de
código y el análisis de repositorios van a tu cuenta de Gemini Pro en Edge (gemini_web.py). Las
acciones sobre la computadora (abrir, ejecutar, escribir, mandar...) siguen por la API con
herramientas (JARVIS_MODELO), que además es el respaldo si Gemini web falla.
"""
import json
import logging
import os
import re

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
import litellm

from . import config, gemini_web, herramientas
from .herramientas import DiagnosticoVerificado, TEMPERATURA_AUDITOR

log = logging.getLogger("jarvis")
litellm.suppress_debug_info = True

TEMPERATURA = 0.1

REGLA_VISION_TIEMPO_REAL = """REGLA DE VISIÓN EN TIEMPO REAL (PROHIBIDO HABLAR A CIEGAS):
1. TIENES PROHIBIDO decir 'debería aparecerte', 'fijate si ves', 'probablemente te salga' o 'imagino que estás en'. Vos ESTÁS VIENDO la pantalla del usuario en vivo.
2. Antes de dar cualquier indicación sobre un programa abierto, ejecutá inspeccionar_pantalla_hd o leé el último frame de video recibido y describí exactamente lo que está en pantalla: 'Estoy viendo tu ventana de [Programa], tenés abierto el cartel que dice [Texto exacto], hacé clic en el botón [Nombre exacto]'.
3. Si después de que el usuario hace clic algo no cambia o sale un cartel nuevo, leé el nuevo cartel en pantalla y decile exactamente qué dice sin adivinar."""

PROTOCOLO_CERTEZA_100 = """PROTOCOLO DE CERTEZA 100% (PROHIBIDO IMPROVISAR EN VIVO):
1. Ante CUALQUIER pregunta técnica, error o duda de configuración, TIENES PROHIBIDO responder con lo primero que se te ocurra en el canal de voz.
2. Tu única respuesta inmediata debe ser: 'Esperame unos segundos que lo investigo a fondo y verifico tu sistema para darte el dato exacto.' e inmediatamente ejecutá diagnosticar_y_resolver_con_certeza(consulta_usuario) o investigar_a_fondo().
3. Solo cuando la herramienta termine de verificar la realidad, leéle al usuario los pasos exactos comprobados (1, 2, 3). Jamás uses al usuario como conejillo de indias haciéndole probar cosas a ver si funcionan."""

INSTRUCCIONES = f"""{REGLA_VISION_TIEMPO_REAL}
{PROTOCOLO_CERTEZA_100}
Sos {config.NOMBRE.capitalize()}, un asistente e ingeniera de software que controla la computadora del usuario con precisión absoluta.
LEY DE PRECISIÓN ABSOLUTA:
1. PROHIBIDO ADIVINAR O DAR RODEOS: Nunca respondas a medias ni des explicaciones genéricas de memoria. Antes de contestar cualquier duda técnica, verificá primero el entorno real ejecutando leer_documento_local, ejecutar_comando, ejecutar_wsl, ejecutar_ssh_vm, inspeccionar_pantalla_hd o investigar_a_fondo.
2. FORMATO DE RESPUESTA OBLIGATORIO (1, 2, 3): Toda guía o solución debe ser directa y al grano en pasos numerados cortos: Paso 1 qué pestaña o archivo tocar, Paso 2 qué valor o código exacto escribir, Paso 3 qué botón apretar o qué comando ejecutar.
3. EJECUCIÓN DIRECTA: Si la consulta se resuelve corriendo un comando en Windows, WSL o la VM por SSH, ejecutalo vos misma con tus herramientas o entregá la línea exacta lista para copiar. Interpretá y corregí siempre errores fonéticos del micrófono según el contexto de ingeniería de software.
- Respondé en español rioplatense, breve y directo.
- Para actuar sobre la computadora usá SIEMPRE las herramientas. Nunca digas que hiciste algo
  si la herramienta no lo confirmó, y si una herramienta devuelve un error, contalo.
- Si una tarea tiene varios pasos (abrir un programa y después escribir), hacelos en orden.
- Si el usuario no autoriza una acción, no insistas ni la intentes de otra forma.
- Si no tenés una herramienta para lo que te piden, decilo y sugerí la más cercana.
- Cuando el usuario pida investigar cómo configurar, conectar o ejecutar una herramienta técnica, NUNCA respondas de memoria si hay dudas ni abras el navegador visual. Ejecutá primero investigar_a_fondo para leer la documentación oficial completa y los bloques de código reales antes de responder o de usar ejecutar_comando.
- Si el pedido es ambiguo y equivocarse sería costoso, preguntá antes de actuar."""

# ---------------------------------------------------------------- enrutamiento a Gemini web

PATRON_EXPLICAR_CODIGO = re.compile(
    r"expl[ií]ca(?:me)?\s+(?:este|el|ese)\s+c[oó]digo|mir[aá]\s+(?:este|el|ese)\s+c[oó]digo|"
    r"revis[aá]\s+lo\s+que\s+tengo\s+en\s+(?:la\s+)?pantalla",
    re.IGNORECASE,
)
PATRON_REPOSITORIO = re.compile(r"\brepo(?:sitorio)?s?\b|github\.com/", re.IGNORECASE)
# Pedidos que son acciones sobre la computadora: van al bucle de herramientas de la API.
PATRON_ACCION = re.compile(
    r"\b(abr[ií](?:me|lo|la)?|abre|cerr[aá]|ejecut[aá]|corr[eé]|instal[aá]|escrib[ií]|tipe[aá]|"
    r"mand[aá]|envi[aá]|whatsapp|carpeta|archivos?\s+(?:que|con|llamad)|busc[aá]\s+(?:el|los|un|mis)\s+archivo|"
    r"qu[eé]\s+hora|fecha|comando|terminal|wsl|ssh|vm|m[aá]quina\s+virtual|captur[aá]|"
    r"inspeccion[aá]|diagnostic[aá]|verific[aá]\s+(?:mi|el)\s+(?:sistema|entorno)|leé\s+el\s+archivo|"
    r"le[eé]\s+(?:el|este)\s+(?:documento|archivo|pdf)|issues?|pull\s+requests?|commits?)\b",
    re.IGNORECASE,
)
_ENCABEZADO_GEMINI = re.compile(r"^\[Gemini [^\]]*\]\n?")


def _extraer_repo(pedido: str) -> str:
    for token in re.findall(r"\S+", pedido):
        limpio = herramientas._limpiar_repo(token.strip(".,;:()\"'"))
        if limpio and ("/" in token):
            return limpio
    return ""


class Cerebro:
    def __init__(self, modelo: str = config.MODELO, usar_gemini_web: bool | None = None):
        self.modelo = modelo
        self.mensajes: list = [{"role": "system", "content": INSTRUCCIONES}]
        self.usar_gemini_web = config.GEMINI_WEB_ACTIVO if usar_gemini_web is None else usar_gemini_web
        self.ultima_respuesta_completa = ""  # markdown original de Gemini (con código) para mostrar

    # -- Gemini web
    def _consulta_gemini_web(self, pedido: str):
        """Devuelve una función que resuelve el pedido con Gemini web, o None si es una acción."""
        if PATRON_EXPLICAR_CODIGO.search(pedido):
            return lambda: herramientas.explicar_codigo_en_pantalla(pedido)
        repo = _extraer_repo(pedido) if PATRON_REPOSITORIO.search(pedido) else ""
        if repo and not re.search(r"\b(issues?|pull\s+requests?|commits?)\b", pedido, re.IGNORECASE):
            return lambda: herramientas.analizar_repositorio_con_gemini(repo, pedido)
        if PATRON_ACCION.search(pedido):
            return None
        return lambda: herramientas.consultar_gemini_pro(pedido)

    def _para_voz(self, resultado: str) -> str:
        completo = _ENCABEZADO_GEMINI.sub("", resultado).strip()
        self.ultima_respuesta_completa = completo
        if "```" in completo:
            # El código no se puede leer en voz alta: se muestra completo en la terminal.
            print(f"\n📋 Respuesta completa de Gemini:\n{completo}\n")
        return gemini_web.limpiar_para_voz(completo) or "Listo."

    def pensar(self, pedido: str) -> str:
        """Recibe lo que dijo el usuario y devuelve la respuesta final en texto."""
        if self.usar_gemini_web:
            consulta = self._consulta_gemini_web(pedido)
            if consulta is not None:
                resultado = consulta()
                if not resultado.startswith("No pude usar Gemini web"):
                    # Queda en el historial para que el bucle de herramientas tenga el contexto.
                    self.mensajes.append({"role": "user", "content": pedido})
                    self.mensajes.append({"role": "assistant", "content": _ENCABEZADO_GEMINI.sub("", resultado)})
                    self._recortar_historial()
                    return self._para_voz(resultado)
                log.warning("Gemini web no disponible, uso la API: %s", resultado)
        return self._pensar_con_herramientas(pedido)

    # -- API con herramientas

    def _recortar_historial(self, maximo: int = 40) -> None:
        """Mantiene la memoria de la charla acotada sin cortar una llamada a herramienta por la mitad."""
        if len(self.mensajes) <= maximo:
            return
        resto = self.mensajes[-(maximo - 1):]
        while resto and self._rol(resto[0]) != "user":
            resto.pop(0)
        self.mensajes = [self.mensajes[0]] + resto

    @staticmethod
    def _rol(mensaje) -> str:
        return mensaje["role"] if isinstance(mensaje, dict) else getattr(mensaje, "role", "")

    def _pensar_con_herramientas(self, pedido: str) -> str:
        """Bucle clásico: la API decide qué herramientas usar hasta tener la respuesta final."""
        self.mensajes.append({"role": "user", "content": pedido})
        self._recortar_historial()

        for _ in range(config.MAX_PASOS):
            respuesta = litellm.completion(
                model=self.modelo,
                messages=self.mensajes,
                tools=herramientas.esquemas(),
                temperature=TEMPERATURA,
            )
            mensaje = respuesta.choices[0].message
            # Se guarda el mensaje tal cual: algunos modelos (como Gemini) necesitan
            # datos internos de la llamada a herramienta para el siguiente paso.
            self.mensajes.append(mensaje)

            llamadas = getattr(mensaje, "tool_calls", None)
            if not llamadas:
                return (mensaje.content or "").strip() or "Listo."

            for llamada in llamadas:
                try:
                    argumentos = json.loads(llamada.function.arguments or "{}")
                except json.JSONDecodeError:
                    argumentos = {}
                resultado = herramientas.ejecutar(llamada.function.name, argumentos)
                log.info("Resultado %s: %s", llamada.function.name, resultado[:300])
                self.mensajes.append({
                    "role": "tool",
                    "tool_call_id": llamada.id,
                    "name": llamada.function.name,
                    "content": resultado,
                })

        return "Hice varios pasos pero no terminé. ¿Querés que siga?"

    def olvidar(self) -> None:
        """Borra la memoria de la conversación actual."""
        self.mensajes = self.mensajes[:1]
