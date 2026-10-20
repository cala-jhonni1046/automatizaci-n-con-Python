"""Oído y voz de Jarvis."""
import asyncio
import logging
import os
import tempfile

from . import config

log = logging.getLogger("jarvis")

PALABRAS_SI = ("si", "sí", "dale", "confirmo", "hacelo", "adelante", "ok", "okay", "obvio", "claro")


# ---------------------------------------------------------------- escuchar

class Oido:
    def __init__(self):
        import speech_recognition as sr

        self.sr = sr
        self.reconocedor = sr.Recognizer()
        self.reconocedor.pause_threshold = 0.8
        self.microfono = sr.Microphone()
        with self.microfono as fuente:
            self.reconocedor.adjust_for_ambient_noise(fuente, duration=1)

    def escuchar(self, limite_segundos: int = 15) -> str:
        """Graba una frase y la devuelve como texto en minúsculas ('' si no entendió)."""
        with self.microfono as fuente:
            try:
                audio = self.reconocedor.listen(fuente, timeout=5, phrase_time_limit=limite_segundos)
            except self.sr.WaitTimeoutError:
                return ""
        try:
            # Usa el reconocimiento gratuito de Google: el audio se envía a Google.
            return self.reconocedor.recognize_google(audio, language=config.IDIOMA_VOZ).lower()
        except self.sr.UnknownValueError:
            return ""
        except self.sr.RequestError as e:
            log.error("Sin conexión con el reconocimiento de voz: %s", e)
            return ""


# ---------------------------------------------------------------- hablar

class Boca:
    def __init__(self):
        self.motor = config.MOTOR_VOZ
        if self.motor == "edge":
            import edge_tts  # noqa: F401  (verifica que esté instalado)
            import pygame

            pygame.mixer.init()
            self.pygame = pygame
        else:
            import pyttsx3

            self.tts = pyttsx3.init()
            self.tts.setProperty("rate", 180)
            for voz in self.tts.getProperty("voices"):
                descripcion = f"{voz.name} {voz.id} {getattr(voz, 'languages', '')}".lower()
                if "spanish" in descripcion or "español" in descripcion or "es-" in descripcion or "es_" in descripcion:
                    self.tts.setProperty("voice", voz.id)
                    break

    def hablar(self, texto: str) -> None:
        print(f"🤖 {config.NOMBRE.capitalize()}: {texto}")
        if not texto:
            return
        if self.motor == "edge":
            self._hablar_edge(texto)
        else:
            self.tts.say(texto)
            self.tts.runAndWait()

    def _hablar_edge(self, texto: str) -> None:
        import edge_tts

        fd, ruta = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            asyncio.run(edge_tts.Communicate(texto, config.VOZ_EDGE).save(ruta))
            self.pygame.mixer.music.load(ruta)
            self.pygame.mixer.music.play()
            while self.pygame.mixer.music.get_busy():
                self.pygame.time.wait(100)
            self.pygame.mixer.music.unload()
        finally:
            os.remove(ruta)


def es_afirmativo(respuesta: str) -> bool:
    palabras = respuesta.lower().replace(",", " ").replace(".", " ").split()
    return any(p in PALABRAS_SI for p in palabras) and "no" not in palabras
