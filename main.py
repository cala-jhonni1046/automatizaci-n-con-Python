"""Jarvis: asistente por voz que controla tu computadora.

Uso:
    python main.py            # modo texto (ideal para probar)
    python main.py --voz      # modo voz: decí "Jarvis" y tu pedido
"""
import argparse
import logging
import sys
import time

from jarvis import config, seguridad
from jarvis.cerebro import Cerebro
from jarvis.voz import es_afirmativo

PALABRAS_FIN_CHARLA = ("gracias", "chau", "eso es todo", "nada más", "nada mas")
PALABRAS_APAGAR = ("apagate", "apágate", "desactivate", "cerrate", "salir")


def configurar_log() -> None:
    logging.basicConfig(
        filename=config.ARCHIVO_LOG, level=logging.INFO, encoding="utf-8",
        format="%(asctime)s %(levelname)s %(message)s",
    )


def pensar_seguro(cerebro: Cerebro, pedido: str) -> str:
    try:
        return cerebro.pensar(pedido)
    except Exception as e:
        logging.exception("Error de la IA")
        return f"Tuve un problema hablando con la IA: {e}"


# ---------------------------------------------------------------- modo texto

def modo_texto() -> None:
    def confirmar_por_texto(pregunta: str) -> bool:
        return es_afirmativo(input(f"\n🔐 {pregunta} (sí/no): "))

    seguridad.registrar_confirmador(confirmar_por_texto)
    cerebro = Cerebro()
    print(f"Jarvis listo con {config.MODELO}. Escribí tu pedido ('salir' para terminar, 'olvidar' para borrar la charla).\n")
    while True:
        try:
            pedido = input("🧑 Vos: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not pedido:
            continue
        if pedido.lower() in ("salir", "exit", "quit"):
            break
        if pedido.lower() == "olvidar":
            cerebro.olvidar()
            print("🤖 Listo, empecé una charla nueva.\n")
            continue
        print(f"🤖 Jarvis: {pensar_seguro(cerebro, pedido)}\n")
    print("Hasta luego.")


# ---------------------------------------------------------------- modo voz

def modo_voz() -> None:
    from jarvis.voz import Boca, Oido

    print("Preparando micrófono y voz...")
    oido, boca = Oido(), Boca()

    def confirmar_por_voz(pregunta: str) -> bool:
        boca.hablar(pregunta + " Decime sí o no.")
        respuesta = oido.escuchar(limite_segundos=5)
        print(f"🧑 Vos: {respuesta}")
        return es_afirmativo(respuesta)

    seguridad.registrar_confirmador(confirmar_por_voz)
    cerebro = Cerebro()
    nombre = config.NOMBRE
    activo_hasta = 0.0
    boca.hablar("Jarvis en línea.")
    print(f'Decí "{nombre.capitalize()}" y tu pedido. Ctrl+C para salir.\n')

    while True:
        texto = oido.escuchar()
        if not texto:
            continue
        en_charla = time.time() < activo_hasta

        if nombre in texto:
            pedido = texto.split(nombre, 1)[1].strip(" ,.")
        elif en_charla:
            pedido = texto
        else:
            continue  # no le hablaban a Jarvis

        print(f"🧑 Vos: {texto}")
        if any(p in pedido for p in PALABRAS_APAGAR):
            boca.hablar("Me apago. Hasta luego.")
            return
        if any(pedido.startswith(p) for p in PALABRAS_FIN_CHARLA):
            boca.hablar("De nada, acá estoy si me necesitás.")
            activo_hasta = 0.0
            continue
        if not pedido:
            boca.hablar("¿Sí? Te escucho.")
            activo_hasta = time.time() + config.SEGUNDOS_CONVERSACION
            continue

        boca.hablar(pensar_seguro(cerebro, pedido))
        activo_hasta = time.time() + config.SEGUNDOS_CONVERSACION


def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis, tu asistente por voz.")
    parser.add_argument("--voz", action="store_true", help="usar micrófono y parlantes")
    args = parser.parse_args()

    configurar_log()
    if not config.clave_configurada():
        sys.exit("Falta la clave de la IA. Copiá .env.example como .env y completá tu clave (mirá el README).")
    try:
        modo_voz() if args.voz else modo_texto()
    except KeyboardInterrupt:
        print("\nHasta luego.")


if __name__ == "__main__":
    main()
