"""El cerebro: habla con la IA (Gemini, Claude u otra) y decide qué herramientas usar."""
import json
import logging

import litellm

from . import config, herramientas

log = logging.getLogger("jarvis")
litellm.suppress_debug_info = True

INSTRUCCIONES = f"""Sos {config.NOMBRE.capitalize()}, un asistente que controla la computadora del usuario por voz.
- Respondé en español rioplatense, breve y natural: tus respuestas se leen en voz alta.
  Nada de listas largas, markdown ni emojis, salvo que el usuario lo pida.
- Para actuar sobre la computadora usá SIEMPRE las herramientas. Nunca digas que hiciste algo
  si la herramienta no lo confirmó, y si una herramienta devuelve un error, contalo.
- Si una tarea tiene varios pasos (abrir un programa y después escribir), hacelos en orden.
- Si el usuario no autoriza una acción, no insistas ni la intentes de otra forma.
- Si no tenés una herramienta para lo que te piden, decilo y sugerí la más cercana.
- Si el pedido es ambiguo y equivocarse sería costoso, preguntá antes de actuar.
- Podés conversar y explicar cosas sin usar herramientas cuando no hace falta actuar."""


class Cerebro:
    def __init__(self, modelo: str = config.MODELO):
        self.modelo = modelo
        self.mensajes: list = [{"role": "system", "content": INSTRUCCIONES}]

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

    def pensar(self, pedido: str) -> str:
        """Recibe lo que dijo el usuario y devuelve la respuesta final en texto."""
        self.mensajes.append({"role": "user", "content": pedido})
        self._recortar_historial()

        for _ in range(config.MAX_PASOS):
            respuesta = litellm.completion(
                model=self.modelo,
                messages=self.mensajes,
                tools=herramientas.esquemas(),
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
